#!/usr/bin/env python3
"""Canonical external model-cache CLI for the Pharaon Asset Factory container.

Subcommands (all offline except explicitly confirmed acquisition):

    models plan    --manifest MANIFEST [--json]
    models status  --manifest MANIFEST [--json]
    models acquire --manifest MANIFEST --confirm-download --max-bytes N [--json]
    models verify  --manifest MANIFEST [--json]

The cache root is read from MODEL_CACHE_DIR and defaults to /models. Artifacts
are written below the cache root under the manifest's validated destination
namespace. Acquisition requires both --confirm-download and --max-bytes, never
opens a network connection before the complete manifest is validated, the cache
is inspected, and the byte allowance and policy limits are satisfied, streams
each artifact into a temporary .part file on the destination filesystem, and
promotes it to the final path only after exact size and SHA-256 verification.

Exit codes:
    0   operation succeeded
    2   policy refusal (missing confirmation or insufficient byte allowance)
    3   manifest validation or destination path-security failure
    4   integrity verification failure
    5   transport failure
    6   lock/concurrency conflict
    64  invalid CLI usage
    70  internal error

This module is standard-library only and performs no automatic or background
network access.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional, Sequence


SCHEMA_VERSION = 1
JSON_SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_POLICY_REFUSAL = 2
EXIT_MANIFEST_INVALID = 3
EXIT_INTEGRITY_FAILURE = 4
EXIT_TRANSPORT_FAILURE = 5
EXIT_LOCK_CONFLICT = 6
EXIT_INVALID_USAGE = 64
EXIT_INTERNAL_ERROR = 70

DEFAULT_CACHE_DIR = "/models"
CHUNK_SIZE = 64 * 1024
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 1.0
LOCK_WAIT_SECONDS = 10.0
LOCK_POLL_INTERVAL = 0.25
LOCK_HEARTBEAT_SECONDS = 30.0
STALE_LOCK_GRACE_SECONDS = 24 * 60 * 60
STALE_LOCK_NO_OWNER_GRACE_SECONDS = 60.0
MAX_MANIFEST_FILES = 1000
MAX_PATH_COMPONENT_LENGTH = 255

SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MUTABLE_REVISION_WORDS = {"main", "latest", "master", "head"}
MUTABLE_URL_FRAGMENTS = (
    "/resolve/main/",
    "/resolve/master/",
    "/resolve/head/",
    "/resolve/latest/",
    "/blob/main/",
    "/blob/master/",
    "/blob/head/",
    "/blob/latest/",
    "/archive/refs/heads/",
)
TEST_HOSTS = {"host.docker.internal"}
FILE_STATES = ("ABSENT", "PARTIAL", "CORRUPTED", "VERIFIED")
USER_AGENT = "pharaon-model-cache/" + str(SCHEMA_VERSION)


class ModelCacheError(Exception):
    """Base class for expected, machine-classifiable model-cache failures."""

    classification = "ERROR"
    exit_code = EXIT_INTERNAL_ERROR
    manifest: Any = None
    entries: Any = None
    stats: dict[str, int] | None = None


class ManifestValidationError(ModelCacheError):
    """Manifest is malformed or a destination violates path policy."""

    classification = "MANIFEST_INVALID"
    exit_code = EXIT_MANIFEST_INVALID


class PolicyRefusalError(ModelCacheError):
    """Acquisition was refused before any network or final-file activity."""

    classification = "POLICY_REFUSAL"
    exit_code = EXIT_POLICY_REFUSAL


class IntegrityError(ModelCacheError):
    """Downloaded or cached content failed size or SHA-256 verification."""

    classification = "INTEGRITY_FAILURE"
    exit_code = EXIT_INTEGRITY_FAILURE


class TransportError(ModelCacheError):
    """A network transport failure occurred after an authorized request."""

    classification = "TRANSPORT_FAILURE"
    exit_code = EXIT_TRANSPORT_FAILURE

    def __init__(self, message: str, *, attempts: int = 0, retries: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.retries = retries


class LockConflictError(ModelCacheError):
    """Another process holds the artifact-set lock and the wait is bounded."""

    classification = "LOCK_CONFLICT"
    exit_code = EXIT_LOCK_CONFLICT


class UsageError(ModelCacheError):
    """Invalid command-line usage."""

    classification = "INVALID_REQUEST"
    exit_code = EXIT_INVALID_USAGE


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


class _NonRetryableHttpError(TransportError):
    """Permanent HTTP failure that must not be retried."""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _cache_root(environment: dict[str, str] | None = None) -> Path:
    env = dict(os.environ if environment is None else environment)
    return _absolute(Path(env.get("MODEL_CACHE_DIR", DEFAULT_CACHE_DIR)))


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    return normalized in {"localhost", "127.0.0.1", "::1"} or normalized.startswith("127.")


def _validated_components(value: str, field: str) -> list[str]:
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(f"{field} must be a non-empty string")
    if value.startswith("/") or "\\" in value or value.startswith("~"):
        raise ManifestValidationError(f"{field} must be a relative path: {value!r}")
    components = value.split("/")
    for component in components:
        if component in ("", ".", ".."):
            raise ManifestValidationError(f"{field} contains an invalid component: {value!r}")
        if not SAFE_COMPONENT_RE.fullmatch(component):
            raise ManifestValidationError(f"{field} contains an unsafe component {component!r}")
        if len(component) > MAX_PATH_COMPONENT_LENGTH:
            raise ManifestValidationError(f"{field} component exceeds {MAX_PATH_COMPONENT_LENGTH} characters")
    return components


def _validated_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise ManifestValidationError("file url must be a non-empty string")
    if len(url) > 2048:
        raise ManifestValidationError("file url exceeds 2048 characters")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ManifestValidationError(f"unsupported URL scheme {parsed.scheme!r}; expected http or https")
    if not parsed.hostname:
        raise ManifestValidationError("file url is missing a host")
    if parsed.username is not None or parsed.password is not None:
        raise ManifestValidationError("file url must not contain embedded credentials")
    host = parsed.hostname.lower()
    lowered = url.lower()
    if parsed.scheme == "http" and not (_is_loopback_host(host) or host in TEST_HOSTS):
        raise ManifestValidationError(
            "http URLs are allowed only for loopback or host.docker.internal test fixtures; production sources require https"
        )
    if any(fragment in lowered for fragment in MUTABLE_URL_FRAGMENTS):
        raise ManifestValidationError("mutable source reference in URL is not allowed")
    mutable_segments = [
        segment for segment in parsed.path.split("/") if segment and segment.lower() in MUTABLE_REVISION_WORDS
    ]
    if mutable_segments:
        raise ManifestValidationError("mutable source reference in URL is not allowed")
    return url


def _parse_manifest_file(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ManifestValidationError(f"cannot read manifest {path}: {error}") from error
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ManifestValidationError(f"manifest is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ManifestValidationError("manifest root must be a JSON object")

    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        raise ManifestValidationError(
            f"unsupported or missing schema_version; expected {SCHEMA_VERSION}"
        )

    artifact_set = data.get("artifact_set")
    if not isinstance(artifact_set, str) or not SAFE_COMPONENT_RE.fullmatch(artifact_set):
        raise ManifestValidationError(
            "artifact_set must be a non-empty identifier using letters, digits, '.', '_', or '-'"
        )

    revision = data.get("revision")
    if not isinstance(revision, str) or not revision:
        raise ManifestValidationError("revision must be a non-empty string")
    if revision.lower() in MUTABLE_REVISION_WORDS:
        raise ManifestValidationError(
            f"mutable revision {revision!r} is not allowed; use an immutable revision"
        )
    if not re.fullmatch(r"[A-Za-z0-9._-]{4,128}", revision):
        raise ManifestValidationError(
            "revision must be an immutable identifier of 4-128 characters using letters, digits, '.', '_', or '-'"
        )

    namespace = data.get("namespace")
    if not isinstance(namespace, str):
        raise ManifestValidationError("namespace must be a non-empty string")
    _validated_components(namespace, "namespace")

    description = data.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > 512):
        raise ManifestValidationError("description must be a string of at most 512 characters")

    raw_files = data.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ManifestValidationError("files must be a non-empty list")
    if len(raw_files) > MAX_MANIFEST_FILES:
        raise ManifestValidationError(f"files exceeds the policy limit of {MAX_MANIFEST_FILES}")

    files: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, dict):
            raise ManifestValidationError(f"files[{index}] must be a JSON object")
        missing = {"path", "url", "size", "sha256"} - raw_file.keys()
        if missing:
            raise ManifestValidationError(f"files[{index}] is missing required fields: {', '.join(sorted(missing))}")
        rel_path = raw_file.get("path")
        if not isinstance(rel_path, str) or not rel_path:
            raise ManifestValidationError(f"files[{index}].path must be a non-empty string")
        _validated_components(rel_path, f"files[{index}].path")
        url = _validated_url(raw_file.get("url"))
        size = raw_file.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ManifestValidationError(f"files[{index}].size must be a positive integer")
        sha256 = raw_file.get("sha256")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise ManifestValidationError(
                f"files[{index}].sha256 must be a 64-character lowercase hexadecimal SHA-256"
            )
        role = raw_file.get("role")
        if role is not None and (not isinstance(role, str) or not role or len(role) > 128):
            raise ManifestValidationError(f"files[{index}].role must be a non-empty string of at most 128 characters")
        target = namespace + "/" + rel_path
        if target in seen_targets:
            raise ManifestValidationError(f"duplicate destination path: {target}")
        seen_targets.add(target)
        files.append(
            {
                "path": rel_path,
                "url": url,
                "size": size,
                "sha256": sha256,
                "role": role,
            }
        )

    total_size = sum(file["size"] for file in files)
    return {
        "schema_version": schema_version,
        "artifact_set": artifact_set,
        "revision": revision,
        "namespace": namespace,
        "description": description,
        "files": files,
        "total_size": total_size,
    }


def parse_manifest(path: Path) -> dict[str, Any]:
    """Parse and fully validate a model artifact manifest."""
    return _parse_manifest_file(Path(path))


def _canonical_plan_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    files = sorted(
        (
            {
                "path": file["path"],
                "url": file["url"],
                "size": file["size"],
                "sha256": file["sha256"],
                "role": file.get("role"),
            }
            for file in manifest["files"]
        ),
        key=lambda file: file["path"],
    )
    return {
        "schema_version": manifest["schema_version"],
        "artifact_set": manifest["artifact_set"],
        "revision": manifest["revision"],
        "namespace": manifest["namespace"],
        "files": files,
    }


def _plan_id(manifest: dict[str, Any]) -> str:
    canonical = json.dumps(_canonical_plan_payload(manifest), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _target_path(cache_root: Path, manifest: dict[str, Any], rel_path: str) -> Path:
    return _absolute(cache_root) / manifest["namespace"] / rel_path


def _file_state(target: Path, expected_size: int, expected_sha256: str) -> dict[str, Any]:
    part = Path(str(target) + ".part")
    if target.is_symlink():
        return {"state": "CORRUPTED", "detail": "final path is a symlink"}
    if target.exists():
        if not target.is_file():
            return {"state": "CORRUPTED", "detail": "final path is not a regular file"}
        actual_size = target.stat().st_size
        if actual_size != expected_size:
            return {
                "state": "CORRUPTED",
                "detail": f"size mismatch: expected {expected_size} bytes, found {actual_size}",
            }
        if _sha256_file(target) != expected_sha256:
            return {"state": "CORRUPTED", "detail": "SHA-256 mismatch"}
        return {"state": "VERIFIED", "detail": None}
    if part.exists():
        return {"state": "PARTIAL", "detail": "incomplete download file present"}
    return {"state": "ABSENT", "detail": None}


def _symlink_escape_detail(cache_root: Path, target: Path) -> str | None:
    """Return a description when *target* escapes *cache_root* via a symlink.

    Only ancestor directories are inspected here; the final path itself is
    handled by the caller so a final symlink can be reported distinctly.
    """
    root = _absolute(cache_root)
    try:
        relative = target.relative_to(root)
    except ValueError:
        return f"destination escapes the model cache: {target} is outside {root}"
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            real = Path(os.path.realpath(current))
            try:
                real.relative_to(root)
            except ValueError:
                return (
                    f"destination passes through a symlink that escapes the cache: "
                    f"{current} -> {real}"
                )
    return None


def _collect_states(cache_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for file in manifest["files"]:
        target = _target_path(cache_root, manifest, file["path"])
        issue = _symlink_escape_detail(cache_root, target)
        if issue is not None:
            state = {"state": "CORRUPTED", "detail": issue}
        else:
            state = _file_state(target, file["size"], file["sha256"])
        entries.append(
            {
                "path": file["path"],
                "url": file["url"],
                "expected_size": file["size"],
                "sha256": file["sha256"],
                "role": file.get("role"),
                "target": str(target),
                "state": state["state"],
                "detail": state["detail"],
            }
        )
    return entries


def _count_states(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {state: 0 for state in FILE_STATES}
    for entry in entries:
        counts[entry["state"]] += 1
    return counts


def _validate_destination(cache_root: Path, manifest: dict[str, Any], rel_path: str) -> Path:
    root = _absolute(cache_root)
    target = root / manifest["namespace"] / rel_path
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ManifestValidationError(
            f"destination escapes the model cache: {target} is outside {root}"
        ) from error
    issue = _symlink_escape_detail(root, target)
    if issue is not None:
        raise ManifestValidationError(issue)
    if target.is_symlink():
        raise ManifestValidationError(f"destination is an existing symlink: {target}")
    if target.exists() and not target.is_file():
        raise ManifestValidationError(f"destination exists and is not a regular file: {target}")
    return target


def _windows_pid_alive(pid: int) -> bool:
    """Conservative Windows liveness probe using only the standard library.

    Opens the process with limited query rights and checks whether its exit
    code is STILL_ACTIVE (259). A process that cannot be opened is treated as
    dead; an unexpected query failure is treated conservatively as alive.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class _ArtifactLock:
    """Per-artifact-set lock stored under the external cache root.

    Acquisition uses atomic directory creation. A bounded wait is attempted
    before failing with a clean LOCK_CONFLICT. Stale locks are broken only
    conservatively: the owner metadata must be older than a long grace period
    AND the recorded owner process must no longer be alive. An active lock is
    never broken.
    """

    def __init__(self, cache_root: Path, plan_id: str) -> None:
        self.lock_root = _absolute(cache_root) / ".locks"
        self.lock_dir = self.lock_root / plan_id
        self.owner_path = self.lock_dir / "owner.json"
        self.owner = {
            "plan_id": plan_id,
            "pid": os.getpid(),
            "start_epoch": time.time(),
        }

    @staticmethod
    def _owner_alive(pid: int) -> bool:
        """Return whether *pid* is still running.

        POSIX uses os.kill(pid, 0). On Windows os.kill does not act as a
        liveness probe (signal 0 maps to a console Ctrl+C event), so a
        conservative ctypes-based probe is used there instead.
        """
        if os.name == "nt":
            return _windows_pid_alive(pid)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    @classmethod
    def _stale(cls, lock_dir: Path) -> bool:
        owner_path = lock_dir / "owner.json"
        try:
            if owner_path.exists():
                age = time.time() - owner_path.stat().st_mtime
                if age < STALE_LOCK_GRACE_SECONDS:
                    return False
                try:
                    owner = json.loads(owner_path.read_text(encoding="utf-8"))
                    pid = int(owner.get("pid", -1))
                except (OSError, ValueError, json.JSONDecodeError):
                    pid = -1
                if pid > 0 and cls._owner_alive(pid):
                    return False
                return True
            return time.time() - lock_dir.stat().st_mtime > STALE_LOCK_NO_OWNER_GRACE_SECONDS
        except OSError:
            return False

    def _break_stale(self) -> None:
        if not self.lock_dir.is_dir():
            return
        try:
            if self.owner_path.exists():
                self.owner_path.unlink()
            self.lock_dir.rmdir()
        except OSError:
            pass

    def acquire(self) -> None:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                self.lock_root.mkdir(parents=True, exist_ok=True)
                os.mkdir(self.lock_dir)
                break
            except FileExistsError:
                if self._stale(self.lock_dir):
                    self._break_stale()
                    continue
                if time.monotonic() >= deadline:
                    raise LockConflictError(
                        f"another process holds the artifact-set lock {self.lock_dir.name}; "
                        f"waited {LOCK_WAIT_SECONDS:g}s without success"
                    )
                time.sleep(LOCK_POLL_INTERVAL)
            except OSError as error:
                raise LockConflictError(
                    f"cannot create artifact-set lock {self.lock_dir}: {error}"
                ) from error
        try:
            self.owner_path.write_text(
                json.dumps(self.owner, sort_keys=True), encoding="utf-8"
            )
        except OSError as error:
            self.release()
            raise LockConflictError(f"could not write lock owner metadata: {error}") from error

    def touch(self) -> None:
        """Refresh the owner heartbeat during long downloads."""
        try:
            if self.owner_path.exists():
                os.utime(self.owner_path, None)
        except OSError:
            pass

    def release(self) -> None:
        try:
            if self.owner_path.exists():
                self.owner_path.unlink()
            if self.lock_dir.is_dir():
                self.lock_dir.rmdir()
        except OSError:
            pass


class _TimedHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        super().connect()
        if self.sock is not None:
            self.sock.settimeout(READ_TIMEOUT)


class _TimedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        if self.sock is not None:
            self.sock.settimeout(READ_TIMEOUT)


class _TimedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request: urllib.request.Request) -> Any:
        return self.do_open(_TimedHTTPConnection, request)


class _TimedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request: urllib.request.Request) -> Any:
        return self.do_open(_TimedHTTPSConnection, request)


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when the target still obeys the manifest policy.

    Redirects to non-http(s) schemes and http redirects to non-loopback,
    non-test hosts are refused so a validated manifest cannot silently extend
    the network boundary through a server-controlled redirect.
    """

    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and not (_is_loopback_host(host) or host in TEST_HOSTS):
            return None
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    handlers: list[Any] = [urllib.request.ProxyHandler({}), _RestrictedRedirectHandler()]
    if getattr(ssl, "create_default_context", None) is not None:
        handlers.append(_TimedHTTPHandler())
        handlers.append(_TimedHTTPSHandler())
    return urllib.request.build_opener(*handlers)


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (408, 429) or error.code >= 500
    if isinstance(error, (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError)):
        return True
    return False


def _stream_once(
    *,
    url: str,
    expected_size: int,
    expected_sha256: str,
    budget_remaining: int,
    target_part: Path,
    lock: _ArtifactLock | None,
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    response = _opener().open(request, timeout=CONNECT_TIMEOUT)
    try:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared != expected_size:
                raise IntegrityError(
                    f"Content-Length {declared} does not match expected size {expected_size}"
                )
        digest = hashlib.sha256()
        written = 0
        with open(target_part, "wb") as handle:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > expected_size:
                    raise IntegrityError(
                        f"download exceeded expected size {expected_size} (received at least {written})"
                    )
                if written > budget_remaining:
                    raise IntegrityError(
                        f"download exceeded the remaining byte allowance {budget_remaining}"
                    )
                digest.update(chunk)
                handle.write(chunk)
                if lock is not None:
                    lock.touch()
            if written != expected_size:
                raise IntegrityError(
                    f"downloaded size {written} does not match expected size {expected_size}"
                )
            if digest.hexdigest() != expected_sha256:
                raise IntegrityError("SHA-256 mismatch after download")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        response.close()


def _download_file(
    *,
    url: str,
    expected_size: int,
    expected_sha256: str,
    budget_remaining: int,
    target_part: Path,
    lock: _ArtifactLock | None,
    stats: dict[str, int],
) -> None:
    attempts = 0
    retries = 0
    last_error: BaseException | None = None
    while attempts <= MAX_RETRIES:
        attempts += 1
        stats["requests_attempted"] += 1
        try:
            _stream_once(
                url=url,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                budget_remaining=budget_remaining,
                target_part=target_part,
                lock=lock,
            )
            return
        except urllib.error.HTTPError as error:
            last_error = error
            if not _is_retryable(error):
                raise _NonRetryableHttpError(
                    f"server returned HTTP {error.code} for {url}", attempts=attempts, retries=retries
                ) from error
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as error:
            last_error = error
        except IntegrityError:
            raise
        if attempts > MAX_RETRIES:
            break
        retries += 1
        stats["retries"] += 1
        time.sleep(RETRY_BACKOFF_SECONDS)
    raise TransportError(
        f"download failed after {attempts} attempt(s) and {retries} retry(ies): {last_error}",
        attempts=attempts,
        retries=retries,
    )


def _remove_part(target_part: Path | None) -> None:
    if target_part is None:
        return
    try:
        target_part.unlink()
    except OSError:
        pass


def _base_report(
    *,
    command: str,
    manifest: dict[str, Any] | None,
    cache_root: Path,
    entries: list[dict[str, Any]] | None,
    max_bytes: int | None,
    network: dict[str, int],
) -> dict[str, Any]:
    file_count = len(manifest["files"]) if manifest is not None else 0
    total = manifest["total_size"] if manifest is not None else 0
    required = 0
    counts = {state: 0 for state in FILE_STATES}
    if entries is not None:
        counts = _count_states(entries)
        required = sum(
            entry["expected_size"] for entry in entries if entry["state"] != "VERIFIED"
        )
    report: dict[str, Any] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": command,
        "success": False,
        "classification": "ERROR",
        "exit_code": EXIT_INTERNAL_ERROR,
        "artifact_set": manifest["artifact_set"] if manifest is not None else None,
        "revision": manifest["revision"] if manifest is not None else None,
        "namespace": manifest["namespace"] if manifest is not None else None,
        "plan_id": _plan_id(manifest) if manifest is not None else None,
        "cache_root": str(cache_root),
        "file_count": file_count,
        "file_counts": counts,
        "bytes": {
            "total_expected": total,
            "required": required,
            "max_bytes": max_bytes,
        },
        "acquirable": manifest is not None,
        "fully_cached": entries is not None and required == 0,
        "files": entries or [],
        "network": dict(network),
        "detail": None,
    }
    return report


def _success_report(report: dict[str, Any]) -> dict[str, Any]:
    report.update(success=True, classification="OK", exit_code=EXIT_OK)
    return report


def _failure_report(report: dict[str, Any], error: ModelCacheError) -> dict[str, Any]:
    report.update(
        success=False,
        classification=error.classification,
        exit_code=error.exit_code,
        detail=str(error),
    )
    return report


def _read_manifest(manifest_arg: str) -> dict[str, Any]:
    path = Path(manifest_arg)
    if not path.exists():
        raise ManifestValidationError(f"manifest file does not exist: {path}")
    return parse_manifest(path)


def run_plan(args: argparse.Namespace, environment: dict[str, str]) -> dict[str, Any]:
    manifest = _read_manifest(args.manifest)
    cache_root = _cache_root(environment)
    entries = _collect_states(cache_root, manifest)
    report = _base_report(
        command="plan",
        manifest=manifest,
        cache_root=cache_root,
        entries=entries,
        max_bytes=None,
        network={"requests_attempted": 0, "retries": 0},
    )
    return _success_report(report)


def run_status(args: argparse.Namespace, environment: dict[str, str]) -> dict[str, Any]:
    manifest = _read_manifest(args.manifest)
    cache_root = _cache_root(environment)
    entries = _collect_states(cache_root, manifest)
    report = _base_report(
        command="status",
        manifest=manifest,
        cache_root=cache_root,
        entries=entries,
        max_bytes=None,
        network={"requests_attempted": 0, "retries": 0},
    )
    return _success_report(report)


def run_verify(args: argparse.Namespace, environment: dict[str, str]) -> dict[str, Any]:
    manifest = _read_manifest(args.manifest)
    cache_root = _cache_root(environment)
    entries = _collect_states(cache_root, manifest)
    report = _base_report(
        command="verify",
        manifest=manifest,
        cache_root=cache_root,
        entries=entries,
        max_bytes=None,
        network={"requests_attempted": 0, "retries": 0},
    )
    all_verified = all(entry["state"] == "VERIFIED" for entry in entries)
    if all_verified:
        return _success_report(report)
    report.update(
        success=False,
        classification="NOT_VERIFIED",
        exit_code=EXIT_INTEGRITY_FAILURE,
        detail="one or more artifacts are not verified (see per-file states)",
    )
    return report


def run_acquire(args: argparse.Namespace, environment: dict[str, str]) -> dict[str, Any]:
    manifest = _read_manifest(args.manifest)
    cache_root = _cache_root(environment)
    entries = _collect_states(cache_root, manifest)
    required = sum(entry["expected_size"] for entry in entries if entry["state"] != "VERIFIED")

    if not args.confirm_download:
        error = PolicyRefusalError(
            "acquisition requires --confirm-download; no network access was attempted"
        )
        error.entries = entries
        error.manifest = manifest
        raise error
    if args.max_bytes is None:
        error = PolicyRefusalError(
            "acquisition requires --max-bytes; no network access was attempted"
        )
        error.entries = entries
        error.manifest = manifest
        raise error
    if args.max_bytes < required:
        error = PolicyRefusalError(
            f"required {required} bytes exceeds --max-bytes {args.max_bytes}; "
            "no network access was attempted"
        )
        error.entries = entries
        error.manifest = manifest
        raise error

    plan_id = _plan_id(manifest)
    lock = _ArtifactLock(cache_root, plan_id)
    lock.acquire()
    stats = {"requests_attempted": 0, "retries": 0}
    cumulative = 0
    try:
        # Re-inspect the cache after acquiring the lock so files verified by a
        # concurrent acquirer are reused instead of downloaded again.
        entries = _collect_states(cache_root, manifest)
        for entry in entries:
            entry["downloaded"] = False
            target_part: Path | None = None
            if entry["state"] == "VERIFIED":
                entry["action"] = "reused"
                continue
            try:
                target = _validate_destination(cache_root, manifest, entry["path"])
                target_part = Path(str(target) + ".part")
                if target_part.exists():
                    if not target_part.is_file():
                        raise ManifestValidationError(
                            f"partial path is not a regular file: {target_part}"
                        )
                    target_part.unlink()
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    raise ManifestValidationError(
                        f"cannot create destination directory {target.parent}: {error}"
                    ) from error
                budget_remaining = args.max_bytes - cumulative
                _download_file(
                    url=entry["url"],
                    expected_size=entry["expected_size"],
                    expected_sha256=entry["sha256"],
                    budget_remaining=budget_remaining,
                    target_part=target_part,
                    lock=lock,
                    stats=stats,
                )
                try:
                    os.replace(target_part, target)
                except OSError as error:
                    raise ManifestValidationError(
                        f"cannot promote verified download to {target}: {error}"
                    ) from error
                cumulative += entry["expected_size"]
                entry["downloaded"] = True
                entry["action"] = "downloaded"
                entry["state"] = "VERIFIED"
                entry["detail"] = None
            except ModelCacheError as error:
                error.entries = entries
                error.stats = stats
                error.manifest = manifest
                _remove_part(target_part)
                raise
    finally:
        lock.release()

    report = _base_report(
        command="acquire",
        manifest=manifest,
        cache_root=cache_root,
        entries=entries,
        max_bytes=args.max_bytes,
        network=stats,
    )
    report["bytes"]["downloaded"] = cumulative
    return _success_report(report)


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(
        prog="models",
        description="Manage the external model cache (plan/status/acquire/verify).",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")

    for name in ("plan", "status", "verify"):
        sub = subparsers.add_parser(name, help=f"inspect model cache {name} offline")
        sub.add_argument("--manifest", required=True, help="path to the artifact manifest JSON")
        sub.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    acquire = subparsers.add_parser("acquire", help="download artifacts with explicit authorization")
    acquire.add_argument("--manifest", required=True, help="path to the artifact manifest JSON")
    acquire.add_argument("--confirm-download", action="store_true", help="explicitly authorize downloads")
    acquire.add_argument("--max-bytes", type=int, default=None, help="hard maximum byte allowance")
    acquire.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _usage_report(message: str) -> dict[str, Any]:
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": None,
        "success": False,
        "classification": UsageError.classification,
        "exit_code": UsageError.exit_code,
        "artifact_set": None,
        "revision": None,
        "namespace": None,
        "plan_id": None,
        "cache_root": str(_cache_root()),
        "file_count": 0,
        "file_counts": {state: 0 for state in FILE_STATES},
        "bytes": {"total_expected": 0, "required": 0, "max_bytes": None},
        "acquirable": False,
        "fully_cached": False,
        "files": [],
        "network": {"requests_attempted": 0, "retries": 0},
        "detail": message,
    }


def _internal_error_report(command: str | None, detail: str) -> dict[str, Any]:
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": command,
        "success": False,
        "classification": "INTERNAL_ERROR",
        "exit_code": EXIT_INTERNAL_ERROR,
        "artifact_set": None,
        "revision": None,
        "namespace": None,
        "plan_id": None,
        "cache_root": str(_cache_root()),
        "file_count": 0,
        "file_counts": {state: 0 for state in FILE_STATES},
        "bytes": {"total_expected": 0, "required": 0, "max_bytes": None},
        "acquirable": False,
        "fully_cached": False,
        "files": [],
        "network": {"requests_attempted": 0, "retries": 0},
        "detail": detail,
    }


def _format_human(report: dict[str, Any]) -> str:
    lines = [
        f"SCHEMA_VERSION={report['schema_version']}",
        f"COMMAND={report['command']}",
        f"STATUS={'OK' if report['success'] else report['classification']}",
        f"EXIT_CODE={report['exit_code']}",
        f"ARTIFACT_SET={report['artifact_set']}",
        f"REVISION={report['revision']}",
        f"NAMESPACE={report['namespace']}",
        f"PLAN_ID={report['plan_id']}",
        f"CACHE_ROOT={report['cache_root']}",
        f"FILE_COUNT={report['file_count']}",
        "FILE_COUNTS=" + ", ".join(f"{key}={value}" for key, value in report["file_counts"].items()),
        "BYTES=" + ", ".join(f"{key}={value}" for key, value in report["bytes"].items()),
        f"ACQUIRABLE={'YES' if report['acquirable'] else 'NO'}",
        f"FULLY_CACHED={'YES' if report['fully_cached'] else 'NO'}",
        f"REQUESTS_ATTEMPTED={report['network']['requests_attempted']}",
        f"RETRIES={report['network']['retries']}",
    ]
    if report.get("detail"):
        lines.append(f"DETAIL={report['detail']}")
    lines.append("FILES:")
    for entry in report.get("files", []):
        lines.append(
            f"- {entry['path']}: {entry['state']} (expected {entry['expected_size']} bytes, target {entry['target']})"
        )
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    environment: dict[str, str] | None = None,
) -> int:
    env = dict(os.environ if environment is None else environment)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as error:
        print(json.dumps(_usage_report(str(error)), indent=2, sort_keys=True))
        return error.exit_code

    if getattr(args, "max_bytes", None) is not None and args.max_bytes < 0:
        print(json.dumps(_usage_report("--max-bytes must be a non-negative integer"), indent=2, sort_keys=True))
        return UsageError.exit_code

    try:
        if args.subcommand == "plan":
            report = run_plan(args, env)
        elif args.subcommand == "status":
            report = run_status(args, env)
        elif args.subcommand == "verify":
            report = run_verify(args, env)
        elif args.subcommand == "acquire":
            report = run_acquire(args, env)
        else:  # pragma: no cover - argparse enforces the subcommand set
            raise UsageError(f"unknown subcommand {args.subcommand!r}")
    except ModelCacheError as error:
        stats = error.stats or {"requests_attempted": 0, "retries": 0}
        report = _failure_report(_base_report(
            command=args.subcommand,
            manifest=error.manifest,
            cache_root=_cache_root(env),
            entries=error.entries,
            max_bytes=getattr(args, "max_bytes", None) if args.subcommand == "acquire" else None,
            network=stats,
        ), error)
    except Exception as error:  # pragma: no cover - defensive boundary
        report = _internal_error_report(args.subcommand, f"{type(error).__name__}: {error}")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_human(report))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
