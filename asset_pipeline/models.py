"""Immutable Hunyuan shape-model binding and preflight helpers.

This module is deliberately standard-library-only and performs no network
access, filesystem writes, model downloads, or heavy ML imports. It reuses the
T-0014 manifest parser and cache-verification implementation instead of
maintaining a second parser or hashing policy.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from docker import model_cache


MODEL_BINDING_SCHEMA_VERSION = 1
CANONICAL_ARTIFACT_SET = "hunyuan3d-2.1-shape"
CANONICAL_HOST = "huggingface.co"
CANONICAL_PATH_PREFIX = "/tencent/Hunyuan3D-2.1/resolve/{revision}/hunyuan3d-dit-v2-1/"
ACCEPTED_ROLES = frozenset({"shape-config", "shape-weights", "shape-auxiliary"})
REQUIRED_ROLES = frozenset({"shape-config", "shape-weights"})
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class ModelPreflightError(Exception):
    """Base class for expected model-preflight failures."""

    exit_code = 3
    status = "ERROR"
    classification = "MODEL_PREFLIGHT_ERROR"


class ModelBindingError(ModelPreflightError):
    """The operator-supplied manifest does not satisfy the strict binding policy."""

    exit_code = 2
    status = "INVALID"
    classification = "MODEL_BINDING_REFUSAL"


class ModelManifestError(ModelPreflightError):
    """The operator-supplied model manifest cannot be read or validated."""

    exit_code = 3
    status = "ERROR"
    classification = "MODEL_MANIFEST_INVALID"


class ModelCacheVerificationError(ModelPreflightError):
    """A valid manifest is bound but one or more cache artifacts are not verified."""

    exit_code = 4
    status = "ERROR"
    classification = "MODEL_CACHE_NOT_VERIFIED"


def _validate_revision(revision: object) -> str:
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise ModelBindingError(
            "manifest revision must be a lowercase 40-character hexadecimal "
            "immutable commit identifier"
        )
    return revision


def _validate_role(role: object, path: str) -> str:
    if not isinstance(role, str) or not role:
        raise ModelBindingError(f"file role is missing or empty for {path!r}")
    if role not in ACCEPTED_ROLES:
        raise ModelBindingError(
            f"file role {role!r} is not supported; accepted roles are "
            f"{', '.join(sorted(ACCEPTED_ROLES))}"
        )
    return role


def _validate_url(url: object, revision: str, file_path: str) -> None:
    if not isinstance(url, str) or not url:
        raise ModelBindingError(f"file URL is missing for {file_path!r}")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ModelBindingError(
            f"file URL must use https for {file_path!r}"
        )
    if parsed.netloc != CANONICAL_HOST:
        raise ModelBindingError(
            f"file URL host must be exactly {CANONICAL_HOST!r} for {file_path!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ModelBindingError(
            f"file URL must not contain credentials for {file_path!r}"
        )
    if parsed.fragment:
        raise ModelBindingError(
            f"file URL must not contain a fragment for {file_path!r}"
        )
    if parsed.query:
        raise ModelBindingError(
            f"file URL must not contain a query string for {file_path!r}"
        )

    prefix = CANONICAL_PATH_PREFIX.format(revision=revision)
    if not parsed.path.startswith(prefix):
        raise ModelBindingError(
            f"file URL does not follow the immutable Hunyuan shape path boundary "
            f"for {file_path!r}"
        )
    suffix = parsed.path[len(prefix):]
    if not suffix:
        raise ModelBindingError(f"file URL path is missing for {file_path!r}")
    if "%" in suffix:
        raise ModelBindingError(
            f"file URL path must not contain percent-encoded components for "
            f"{file_path!r}; immutable model paths are already URL-safe"
        )
    if "\\" in suffix or "\x00" in suffix:
        raise ModelBindingError(
            f"file URL path contains an unsafe separator or NUL byte for {file_path!r}"
        )
    if suffix != file_path:
        raise ModelBindingError(
            f"file URL path below the immutable subdirectory must equal the "
            f"manifest file path exactly for {file_path!r}"
        )
    if any(segment in ("", ".", "..") for segment in suffix.split("/")):
        raise ModelBindingError(
            f"file URL path contains an empty or traversal segment for {file_path!r}"
        )


@dataclass(frozen=True, slots=True)
class ModelFileBinding:
    """One immutable model artifact binding without its source URL."""

    path: str
    role: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "role": self.role,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ModelBinding:
    """Immutable runtime binding for one validated model manifest."""

    schema_version: int
    backend_id: str
    artifact_set: str
    revision: str
    namespace: str
    plan_id: str
    model_root: str
    file_count: int
    total_expected_bytes: int
    files: tuple[ModelFileBinding, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a new JSON-compatible dictionary with no source URLs."""

        return {
            "schema_version": self.schema_version,
            "backend_id": self.backend_id,
            "artifact_set": self.artifact_set,
            "revision": self.revision,
            "namespace": self.namespace,
            "plan_id": self.plan_id,
            "model_root": self.model_root,
            "file_count": self.file_count,
            "total_expected_bytes": self.total_expected_bytes,
            "files": [file.to_dict() for file in self.files],
        }


def bind_parsed_model_manifest(
    manifest: Mapping[str, Any],
    *,
    backend_id: str,
    cache_root: str | Path,
) -> ModelBinding:
    """Bind an already-parsed, already-validated model manifest.

    This is the canonical binding path used by ``shape preflight`` so the
    operator-supplied manifest is parsed and validated exactly once before both
    binding and cache verification consume the same parsed object.
    """

    if not isinstance(manifest, dict):
        raise ModelManifestError("parsed model manifest must be a JSON object")

    artifact_set = manifest["artifact_set"]
    if artifact_set != CANONICAL_ARTIFACT_SET:
        raise ModelBindingError(
            f"artifact_set must be exactly {CANONICAL_ARTIFACT_SET!r}"
        )

    revision = _validate_revision(manifest["revision"])
    expected_namespace = f"{CANONICAL_ARTIFACT_SET}/{revision}"
    if manifest["namespace"] != expected_namespace:
        raise ModelBindingError(
            f"namespace must be exactly {expected_namespace!r}"
        )

    bound_files: list[ModelFileBinding] = []
    roles_seen: set[str] = set()
    for file in manifest["files"]:
        rel_path = file["path"]
        _validate_url(file["url"], revision, rel_path)
        role = _validate_role(file.get("role"), rel_path)
        roles_seen.add(role)
        bound_files.append(
            ModelFileBinding(
                path=rel_path,
                role=role,
                size=file["size"],
                sha256=file["sha256"],
            )
        )

    missing_roles = REQUIRED_ROLES - roles_seen
    if missing_roles:
        names = ", ".join(sorted(missing_roles))
        raise ModelBindingError(
            f"manifest must contain at least one file with each required role: {names}"
        )

    bound_files.sort(key=lambda file: file.path)
    return ModelBinding(
        schema_version=MODEL_BINDING_SCHEMA_VERSION,
        backend_id=backend_id,
        artifact_set=artifact_set,
        revision=revision,
        namespace=manifest["namespace"],
        plan_id=model_cache.manifest_plan_id(manifest),
        model_root=str(Path(cache_root) / manifest["namespace"]),
        file_count=len(bound_files),
        total_expected_bytes=sum(file.size for file in bound_files),
        files=tuple(bound_files),
    )


def bind_model_manifest(
    manifest_path: str | Path,
    *,
    backend_id: str,
    cache_root: str | Path,
) -> ModelBinding:
    """Parse and bind one operator-supplied immutable Hunyuan shape manifest.

    This path-based convenience wrapper parses and validates *manifest_path*
    once and then delegates to :func:`bind_parsed_model_manifest`. Canonical
    CLI preflight should call the parsed-manifest form directly after loading
    the manifest once.
    """

    try:
        manifest = model_cache.parse_manifest(Path(manifest_path))
    except model_cache.ManifestValidationError as error:
        raise ModelManifestError(str(error)) from error
    return bind_parsed_model_manifest(
        manifest,
        backend_id=backend_id,
        cache_root=cache_root,
    )


def assert_binding_matches_verification(
    binding: ModelBinding,
    verification: Mapping[str, Any],
) -> None:
    """Refuse success when binding and verification describe different models.

    The fields checked here are the minimum canonical identity required by the
    preflight contract. A mismatch is an internal consistency failure and must
    never produce ``SHAPE_MODEL_PREFLIGHT_READY``.
    """

    binding_identity = {
        "plan_id": binding.plan_id,
        "artifact_set": binding.artifact_set,
        "revision": binding.revision,
        "namespace": binding.namespace,
        "file_count": binding.file_count,
        "total_expected_bytes": binding.total_expected_bytes,
    }
    verification_bytes = verification.get("bytes", {}) if isinstance(verification, dict) else {}
    verification_identity = {
        "plan_id": verification.get("plan_id"),
        "artifact_set": verification.get("artifact_set"),
        "revision": verification.get("revision"),
        "namespace": verification.get("namespace"),
        "file_count": verification.get("file_count"),
        "total_expected_bytes": verification_bytes.get("total_expected"),
    }
    mismatches = [
        field
        for field in (
            "plan_id",
            "artifact_set",
            "revision",
            "namespace",
            "file_count",
            "total_expected_bytes",
        )
        if binding_identity[field] != verification_identity[field]
    ]
    if mismatches:
        raise ModelCacheVerificationError(
            "model binding identity does not match cache verification: "
            + ", ".join(mismatches)
        )


def verification_summary(
    verification: Mapping[str, Any],
) -> dict[str, object]:
    """Return a sanitized, deterministic cache-verification summary.

    The summary deliberately omits per-file URLs and paths beyond the counts,
    so signed or otherwise sensitive source URLs are never emitted by the
    asset pipeline.
    """

    counts = verification.get("file_counts", {})
    state_counts = {str(state): int(count) for state, count in counts.items()}
    return {
        "cache_root": str(verification.get("cache_root", "")),
        "artifact_set": str(verification.get("artifact_set", "")),
        "revision": str(verification.get("revision", "")),
        "namespace": str(verification.get("namespace", "")),
        "plan_id": str(verification.get("plan_id", "")),
        "file_count": int(verification.get("file_count", 0)),
        "state_counts": state_counts,
        "total_expected_bytes": int(
            verification.get("bytes", {}).get("total_expected", 0)
        ),
        "required_bytes": int(
            verification.get("bytes", {}).get("required", 0)
        ),
        "fully_cached": bool(verification.get("fully_cached", False)),
    }
