#!/usr/bin/env python3
"""Append-only, tamper-evident research-logger for bounded provenance capture.

The logger is deliberately standard-library-only. It is used both as a tested
library and as a command-line tool. The authoritative ``session.log.jsonl`` file
is append-only and hash-chained. Nothing in this module rewrites, resequences,
sorts, truncates, or repairs an existing log file.

The session directory contains:

    session-plan.json
    session-state.json
    session.log.jsonl
    session-summary.json
    responses/

Raw response bodies are never committed. Only sanitized provenance/evidence
derived from the validated session may be committed.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_MAX_REQUESTS = 10
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_SESSION_DIR = ".tmp/t0026-provenance"
CHUNK_SIZE = 64 * 1024
TIMEOUT_SECONDS = 30.0
ZERO_HASH = "0" * 64
USER_AGENT = "pharaon-provenance-capture/" + str(SCHEMA_VERSION)

EXIT_OK = 0
EXIT_POLICY_REFUSAL = 2
EXIT_SESSION_INVALID = 3
EXIT_BUDGET_BLOCKED = 4
EXIT_USAGE = 64
EXIT_INTERNAL = 70

FORBIDDEN_BODY_PATH_FRAGMENTS = (
    ".ckpt",
    ".safetensors",
    ".onnx",
    ".pt",
    ".pth",
    ".bin",
    ".pkl",
    ".msgpack",
)


def canonical_json_bytes(value: object) -> bytes:
    """Return canonical UTF-8 JSON with sorted keys and no insignificant spaces."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_string(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceError(Exception):
    """Base class for expected provenance-capture failures."""

    classification = "PROVENANCE_ERROR"
    exit_code = EXIT_INTERNAL


class PlanValidationError(ProvenanceError):
    classification = "PLAN_INVALID"
    exit_code = EXIT_POLICY_REFUSAL


class SessionInvalidError(ProvenanceError):
    classification = "SESSION_INVALID"
    exit_code = EXIT_SESSION_INVALID


class RequestPolicyError(ProvenanceError):
    classification = "REQUEST_POLICY_REFUSAL"
    exit_code = EXIT_POLICY_REFUSAL


class BudgetBlockedError(ProvenanceError):
    classification = "BUDGET_BLOCKED"
    exit_code = EXIT_BUDGET_BLOCKED


class SessionFinalizedError(ProvenanceError):
    classification = "SESSION_FINALIZED"
    exit_code = EXIT_SESSION_INVALID


class UnexpectedRedirectError(RequestPolicyError):
    classification = "UNEXPECTED_REDIRECT"


def _reject_constant(value: str) -> None:
    raise SessionInvalidError(f"non-finite JSON constant is not allowed: {value}")


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SessionInvalidError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_json_text(text: str, subject: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except SessionInvalidError:
        raise
    except RecursionError as error:
        raise SessionInvalidError(
            f"{subject} exceeds the supported JSON nesting depth"
        ) from error
    except (ValueError, json.JSONDecodeError) as error:
        raise SessionInvalidError(f"{subject} is not valid JSON: {error}") from error


def _read_json_file(path: Path, subject: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SessionInvalidError(f"cannot read {subject} {path}: {error}") from error
    return _load_json_text(text, subject)


def record_hash(record: Mapping[str, Any]) -> str:
    """Compute the hash-chain current hash for one record.

    The current-hash field is excluded from the canonical representation.
    """

    payload = {key: value for key, value in record.items() if key != "current_hash"}
    return sha256_bytes(canonical_json_bytes(payload))


def verify_record_chain(
    records: Sequence[Mapping[str, Any]],
) -> None:
    """Verify sequence continuity and the complete hash chain."""

    previous = ZERO_HASH
    seen_sequence: set[int] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, dict):
            raise SessionInvalidError(f"record {index} must be a JSON object")
        sequence = raw.get("sequence")
        if sequence != index:
            raise SessionInvalidError(
                f"record sequence is {sequence!r}; expected {index}"
            )
        if sequence in seen_sequence:
            raise SessionInvalidError(f"duplicate sequence number: {sequence}")
        seen_sequence.add(sequence)
        if raw.get("previous_hash") != previous:
            raise SessionInvalidError(
                f"record {index} previous hash does not match the prior record"
            )
        if raw.get("current_hash") != record_hash(raw):
            raise SessionInvalidError(f"record {index} current hash is invalid")
        previous = raw["current_hash"]


def _parse_log_lines(lines: Sequence[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        value = _load_json_text(line, f"session log record {line_number}")
        if not isinstance(value, dict):
            raise SessionInvalidError(
                f"session log record {line_number} must be a JSON object"
            )
        records.append(value)
    verify_record_chain(records)
    return records


def _normalise_host(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if not host:
        raise PlanValidationError(f"URL is missing a host: {url!r}")
    return host.lower()


def _validate_public_url(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    allow_query: bool,
) -> urllib.parse.SplitResult:
    if not isinstance(url, str) or not url:
        raise RequestPolicyError("URL must be a non-empty string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise RequestPolicyError("only HTTPS URLs are authorized")
    if not parsed.hostname:
        raise RequestPolicyError("URL is missing a host")
    if parsed.username is not None or parsed.password is not None:
        raise RequestPolicyError("URL must not contain credentials")
    if parsed.fragment:
        raise RequestPolicyError("URL must not contain a fragment")
    if parsed.query and not allow_query:
        raise RequestPolicyError("URL query strings are not authorized")
    host = parsed.hostname.lower()
    if host not in allowed_hosts:
        raise RequestPolicyError(f"host is not authorized: {host!r}")
    lowered_path = parsed.path.lower()
    if any(token in lowered_path for token in FORBIDDEN_BODY_PATH_FRAGMENTS):
        raise RequestPolicyError(
            "checkpoint/weight body paths are not authorized"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class PlanRequest:
    id: str
    method: str
    url: str
    purpose: str
    allow_query: bool = False
    retain: bool = False
    follow: str | None = None
    range_request: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "method": self.method,
            "url": self.url,
            "purpose": self.purpose,
            "allow_query": self.allow_query,
            "retain": self.retain,
            "follow": self.follow,
            "range_request": self.range_request,
        }


class SessionPlan:
    """A validated, immutable research request plan."""

    def __init__(
        self,
        *,
        plan_id: str,
        max_requests: int,
        max_bytes: int,
        allowed_hosts: frozenset[str],
        requests: tuple[PlanRequest, ...],
    ) -> None:
        self.plan_id = plan_id
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.allowed_hosts = allowed_hosts
        self.requests = requests
        self._by_id = {request.id: request for request in requests}
        self.first_id = requests[0].id if requests else None

    def get(self, request_id: str) -> PlanRequest:
        try:
            return self._by_id[request_id]
        except KeyError as error:
            raise PlanValidationError(
                f"unknown plan request id: {request_id!r}"
            ) from error

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "max_requests": self.max_requests,
            "max_bytes": self.max_bytes,
            "allowed_hosts": sorted(self.allowed_hosts),
            "requests": [request.to_dict() for request in self.requests],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionPlan":
        if not isinstance(value, dict):
            raise PlanValidationError("session plan must be a JSON object")
        schema_version = value.get("schema_version")
        if schema_version != SCHEMA_VERSION:
            raise PlanValidationError(
                f"unsupported plan schema_version: {schema_version!r}"
            )
        max_requests = value.get("max_requests")
        if not isinstance(max_requests, int) or isinstance(max_requests, bool):
            raise PlanValidationError("max_requests must be an integer")
        if not 1 <= max_requests <= DEFAULT_MAX_REQUESTS:
            raise PlanValidationError(
                f"max_requests must be between 1 and {DEFAULT_MAX_REQUESTS}"
            )
        max_bytes = value.get("max_bytes")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
            raise PlanValidationError("max_bytes must be an integer")
        if max_bytes <= 0:
            raise PlanValidationError("max_bytes must be positive")

        raw_allowed = value.get("allowed_hosts")
        if not isinstance(raw_allowed, list) or not raw_allowed:
            raise PlanValidationError("allowed_hosts must be a non-empty list")
        allowed_hosts: set[str] = set()
        for host in raw_allowed:
            if not isinstance(host, str) or not host or "://" in host:
                raise PlanValidationError(
                    "allowed_hosts entries must be bare hostnames"
                )
            allowed_hosts.add(host.lower())

        raw_requests = value.get("requests")
        if not isinstance(raw_requests, list) or not raw_requests:
            raise PlanValidationError("requests must be a non-empty list")
        if len(raw_requests) > DEFAULT_MAX_REQUESTS:
            raise PlanValidationError(
                f"requests must contain at most {DEFAULT_MAX_REQUESTS} slots"
            )

        requests: list[PlanRequest] = []
        ids: set[str] = set()
        for index, raw in enumerate(raw_requests):
            if not isinstance(raw, dict):
                raise PlanValidationError(f"requests[{index}] must be an object")
            request_id = raw.get("id")
            if not isinstance(request_id, str) or not request_id:
                raise PlanValidationError(
                    f"requests[{index}].id must be a non-empty string"
                )
            if request_id in ids:
                raise PlanValidationError(f"duplicate request id: {request_id}")
            ids.add(request_id)
            method = str(raw.get("method", "GET")).upper()
            if method not in {"GET"}:
                raise PlanValidationError(
                    f"requests[{index}].method must be GET"
                )
            url = raw.get("url")
            if not isinstance(url, str) or not url:
                raise PlanValidationError(
                    f"requests[{index}].url must be a non-empty string"
                )
            try:
                _validate_public_url(
                    url,
                    allowed_hosts=frozenset(allowed_hosts),
                    allow_query=bool(raw.get("allow_query", False)),
                )
            except RequestPolicyError as error:
                raise PlanValidationError(str(error)) from error
            purpose = raw.get("purpose")
            if not isinstance(purpose, str) or not purpose:
                raise PlanValidationError(
                    f"requests[{index}].purpose must be a non-empty string"
                )
            follow = raw.get("follow")
            if follow is not None and not isinstance(follow, str):
                raise PlanValidationError(
                    f"requests[{index}].follow must be a string when present"
                )
            requests.append(
                PlanRequest(
                    id=request_id,
                    method=method,
                    url=url,
                    purpose=purpose,
                    allow_query=bool(raw.get("allow_query", False)),
                    retain=bool(raw.get("retain", False)),
                    follow=follow,
                    range_request=bool(raw.get("range_request", False)),
                )
            )

        for request in requests:
            if request.follow is not None and request.follow not in ids:
                raise PlanValidationError(
                    f"request {request.id!r} follows unknown id {request.follow!r}"
                )

        computed = compute_plan_id(
            {
                "schema_version": SCHEMA_VERSION,
                "max_requests": max_requests,
                "max_bytes": max_bytes,
                "allowed_hosts": sorted(allowed_hosts),
                "requests": [request.to_dict() for request in requests],
            }
        )
        provided = value.get("plan_id")
        if provided is not None and provided != computed:
            raise PlanValidationError(
                "session plan id does not match the canonical plan"
            )
        return cls(
            plan_id=computed,
            max_requests=max_requests,
            max_bytes=max_bytes,
            allowed_hosts=frozenset(allowed_hosts),
            requests=tuple(requests),
        )


def compute_plan_id(plan_payload: Mapping[str, Any]) -> str:
    payload = {
        "schema_version": plan_payload["schema_version"],
        "max_requests": plan_payload["max_requests"],
        "max_bytes": plan_payload["max_bytes"],
        "allowed_hosts": sorted(plan_payload["allowed_hosts"]),
        "requests": sorted(
            (
                {
                    "id": request["id"],
                    "method": request["method"],
                    "url": request["url"],
                    "purpose": request["purpose"],
                    "allow_query": request["allow_query"],
                    "retain": request["retain"],
                    "follow": request["follow"],
                    "range_request": request["range_request"],
                }
                for request in plan_payload["requests"]
            ),
            key=lambda request: request["id"],
        ),
    }
    return sha256_bytes(canonical_json_bytes(payload))


class _HTTPTransport:
    """Real HTTPS transport with automatic redirects disabled."""

    def __init__(self, timeout: float = TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
    ) -> Any:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or 443
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            port=port,
            timeout=self.timeout,
            context=ssl.create_default_context(),
        )
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request(method, path, headers=headers)
        return connection.getresponse()


class ProvenanceSession:
    """Manage one bounded provenance-capture session."""

    def __init__(
        self,
        session_dir: str | Path,
        *,
        plan: SessionPlan | None = None,
    ) -> None:
        self.root = Path(session_dir)
        self.plan = plan
        self.log_path = self.root / "session.log.jsonl"
        self.state_path = self.root / "session-state.json"
        self.summary_path = self.root / "session-summary.json"
        self.plan_path = self.root / "session-plan.json"
        self.responses_dir = self.root / "responses"

    @property
    def plan(self) -> SessionPlan | None:
        return self._plan

    @plan.setter
    def plan(self, value: SessionPlan | None) -> None:
        self._plan = value

    def _ensure_plan(self) -> SessionPlan:
        if self.plan is None:
            raise PlanValidationError("no session plan is attached")
        return self.plan

    def initialize(self, plan: SessionPlan | None = None) -> None:
        plan = plan or self._ensure_plan()
        self.plan = plan
        self.root.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists() and self.log_path.stat().st_size > 0:
            raise SessionInvalidError(
                f"session log already exists; refusing to reinitialize {self.log_path}"
            )
        if self.state_path.exists() or self.summary_path.exists():
            raise SessionInvalidError(
                "session state or summary already exists; refusing to reinitialize"
            )
        self._write_json_atomic(
            self.plan_path,
            plan.to_dict(),
            "session plan",
        )
        self._write_json_atomic(
            self.state_path,
            self._new_state(plan, [], blocked_reason=None),
            "session state",
        )
        self._write_json_atomic(
            self.summary_path,
            self._summary_from([], plan, finalized=False),
            "session summary",
        )
        if not self.log_path.exists():
            self.log_path.touch(mode=0o600, exist_ok=True)

    def _write_json_atomic(
        self,
        path: Path,
        value: Any,
        subject: str,
    ) -> None:
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(value, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as error:
            raise SessionInvalidError(f"cannot write {subject} {path}: {error}") from error

    def _new_state(
        self,
        plan: SessionPlan,
        records: Sequence[Mapping[str, Any]],
        *,
        blocked_reason: str | None,
    ) -> dict[str, Any]:
        aggregate = sum(
            int(record.get("response_body_bytes", 0))
            for record in records
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_id": plan.plan_id,
            "request_count": len(records),
            "aggregate_bytes": aggregate,
            "remaining_requests": plan.max_requests - len(records),
            "remaining_bytes": plan.max_bytes - aggregate,
            "completed_entries": sorted(
                {
                    record["plan_entry_id"]
                    for record in records
                    if "plan_entry_id" in record
                }
            ),
            "blocked_reason": blocked_reason,
            "finalized": False,
            "created_utc": utc_now(),
        }

    def _summary_from(
        self,
        records: Sequence[Mapping[str, Any]],
        plan: SessionPlan,
        *,
        finalized: bool,
    ) -> dict[str, Any]:
        aggregate = sum(
            int(record.get("response_body_bytes", 0))
            for record in records
        )
        final_hash = records[-1]["current_hash"] if records else None
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_id": plan.plan_id,
            "request_count": len(records),
            "aggregate_bytes": aggregate,
            "final_hash": final_hash,
            "finalized": finalized,
        }

    def load_records(self) -> list[dict[str, Any]]:
        try:
            lines = self.log_path.read_text(
                encoding="utf-8"
            ).splitlines()
        except OSError as error:
            raise SessionInvalidError(
                f"cannot read session log {self.log_path}: {error}"
            ) from error
        return _parse_log_lines(lines)

    def verify_session(
        self,
        *,
        plan: SessionPlan | None = None,
    ) -> list[dict[str, Any]]:
        plan = plan or self.plan
        records = self.load_records()
        if plan is None:
            raise PlanValidationError("no session plan is attached")

        state = _read_json_file(self.state_path, "session state")
        summary = _read_json_file(self.summary_path, "session summary")
        if not isinstance(state, dict) or not isinstance(summary, dict):
            raise SessionInvalidError("session state and summary must be JSON objects")
        if state.get("schema_version") != SCHEMA_VERSION:
            raise SessionInvalidError("unsupported session state schema_version")
        if summary.get("schema_version") != SCHEMA_VERSION:
            raise SessionInvalidError("unsupported session summary schema_version")
        if state.get("plan_id") != plan.plan_id:
            raise SessionInvalidError("session state plan_id does not match the plan")
        if summary.get("plan_id") != plan.plan_id:
            raise SessionInvalidError("session summary plan_id does not match the plan")
        aggregate = sum(
            int(record.get("response_body_bytes", 0))
            for record in records
        )
        if state.get("request_count") != len(records):
            raise SessionInvalidError("session state request count does not match the log")
        if state.get("aggregate_bytes") != aggregate:
            raise SessionInvalidError("session state aggregate bytes do not match the log")
        if summary.get("request_count") != len(records):
            raise SessionInvalidError("session summary request count does not match the log")
        if summary.get("aggregate_bytes") != aggregate:
            raise SessionInvalidError("session summary aggregate bytes do not match the log")
        expected_remaining_requests = plan.max_requests - len(records)
        expected_remaining_bytes = plan.max_bytes - aggregate
        if state.get("remaining_requests") != expected_remaining_requests:
            raise SessionInvalidError("session state remaining requests are invalid")
        if state.get("remaining_bytes") != expected_remaining_bytes:
            raise SessionInvalidError("session state remaining bytes are invalid")
        expected_final_hash = records[-1]["current_hash"] if records else None
        if summary.get("final_hash") != expected_final_hash:
            raise SessionInvalidError("session summary final hash is invalid")
        return records

    def _append_record(
        self,
        record: Mapping[str, Any],
        *,
        plan: SessionPlan,
        blocked_reason: str | None = None,
    ) -> dict[str, Any]:
        records = self.verify_session(plan=plan)
        sequence = len(records) + 1
        previous_hash = records[-1]["current_hash"] if records else ZERO_HASH
        full_record = dict(record)
        full_record.update(
            {
                "schema_version": SCHEMA_VERSION,
                "sequence": sequence,
                "previous_hash": previous_hash,
            }
        )
        full_record["current_hash"] = record_hash(full_record)
        self._append_jsonl_line(full_record)
        records.append(full_record)
        self._write_json_atomic(
            self.state_path,
            self._new_state(plan, records, blocked_reason=blocked_reason),
            "session state",
        )
        self._write_json_atomic(
            self.summary_path,
            self._summary_from(records, plan, finalized=False),
            "session summary",
        )
        return full_record

    def _append_jsonl_line(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        try:
            with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
        except OSError as error:
            raise SessionInvalidError(
                f"cannot append to session log {self.log_path}: {error}"
            ) from error

    def finalize(self) -> dict[str, Any]:
        plan = self._ensure_plan()
        records = self.verify_session(plan=plan)
        self._write_json_atomic(
            self.state_path,
            self._new_state(
                plan,
                records,
                blocked_reason=self._state_blocked_reason(records),
            ),
            "session state",
        )
        state = _read_json_file(self.state_path, "session state")
        state["finalized"] = True
        self._write_json_atomic(self.state_path, state, "session state")
        summary = _read_json_file(self.summary_path, "session summary")
        summary["finalized"] = True
        self._write_json_atomic(self.summary_path, summary, "session summary")
        return summary

    def _state_blocked_reason(
        self,
        records: Sequence[Mapping[str, Any]],
    ) -> str | None:
        if not records:
            return None
        if records[-1].get("transport_classification") in {
            "TIMEOUT",
            "TRANSPORT_ERROR",
            "BUDGET_REFUSAL",
            "CONTENT_LENGTH_INVALID",
            "BYTE_BUDGET_EXCEEDED", "UNEXPECTED_REDIRECT",
        }:
            return records[-1].get("transport_classification")
        return None

    def _headers_dict(self, response: Any) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        get_all = getattr(response, "get_all", None)
        getheaders = getattr(response, "getheaders", None)
        if callable(get_all):
            names = {
                str(name).lower()
                for name, _value in getheaders()
                if getheaders
            }
            for name in names:
                values[name] = [str(value) for value in get_all(name)]
            return values
        if callable(getheaders):
            for name, value in getheaders():
                key = str(name).lower()
                values.setdefault(key, []).append(str(value))
        return values

    def _parse_content_length(
        self,
        response: Any,
    ) -> int | None:
        headers = self._headers_dict(response)
        values = headers.get("content-length", [])
        if not values:
            return None
        parsed: list[int] = []
        for value in values:
            if not re.fullmatch(r"[0-9]+", value.strip()):
                raise SessionInvalidError(
                    f"invalid Content-Length value: {value!r}"
                )
            parsed.append(int(value, 10))
        if len(set(parsed)) != 1:
            raise SessionInvalidError(
                "conflicting Content-Length values"
            )
        return parsed[0]

    def _validate_plan_request(
        self,
        request: PlanRequest,
        plan: SessionPlan,
        *,
        remaining_bytes: int,
    ) -> urllib.parse.SplitResult:
        _validate_public_url(
            request.url,
            allowed_hosts=plan.allowed_hosts,
            allow_query=request.allow_query,
        )
        if request.range_request:
            raise RequestPolicyError("range requests are not authorized")
        if request.url != request.url:
            raise RequestPolicyError("request URL mismatch")
        if remaining_bytes <= 0:
            raise BudgetBlockedError("no response-byte budget remains")
        return urllib.parse.urlsplit(request.url)

    def _save_body(
        self,
        body: bytes,
        *,
        request: PlanRequest,
        sequence: int,
    ) -> str | None:
        if not request.retain:
            return None
        if len(body) > DEFAULT_MAX_BYTES:
            raise BudgetBlockedError(
                "retained body exceeds the maximum supported response body size"
            )
        filename = f"{sequence:02d}-{request.id}.bin"
        target = self.responses_dir / filename
        try:
            target.write_bytes(body)
        except OSError as error:
            raise SessionInvalidError(
                f"cannot save response body {target}: {error}"
            ) from error
        return filename

    def _read_exact(
        self,
        response: Any,
        amount: int,
    ) -> bytes:
        chunks: list[bytes] = []
        remaining = amount
        while remaining > 0:
            chunk = response.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_streaming(
        self,
        response: Any,
        remaining_bytes: int,
    ) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        remaining = remaining_bytes
        eof = False
        while True:
            if remaining <= 0:
                eof = False
                break
            chunk = response.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                eof = True
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), eof

    def execute(
        self,
        transport: Callable[[str, str, dict[str, str]], Any] | None = None,
    ) -> list[dict[str, Any]]:
        plan = self._ensure_plan()
        transport = transport or _HTTPTransport()
        completed: set[str] = set()
        records = self.verify_session(plan=plan)

        current_id = plan.first_id
        while current_id is not None:
            records = self.verify_session(plan=plan)
            if self._is_finalized_or_blocked(records):
                raise SessionFinalizedError(
                    "session is finalized or blocked; refusing additional requests"
                )
            aggregate = sum(
                int(record.get("response_body_bytes", 0))
                for record in records
            )
            if len(records) >= plan.max_requests:
                raise BudgetBlockedError("request budget is exhausted")
            if aggregate >= plan.max_bytes:
                raise BudgetBlockedError("response-byte budget is exhausted")
            request = plan.get(current_id)
            if request.id in completed:
                raise RequestPolicyError(
                    f"request {request.id!r} has already been completed"
                )
            remaining_bytes = plan.max_bytes - aggregate
            self._validate_plan_request(
                request,
                plan,
                remaining_bytes=remaining_bytes,
            )

            record, next_id = self._perform_one(
                request,
                plan,
                remaining_bytes=remaining_bytes,
                transport=transport,
                records=records,
            )
            completed.add(request.id)
            blocked_reason = None
            if record.get("transport_classification") in {
                "TIMEOUT",
                "TRANSPORT_ERROR",
                "BUDGET_REFUSAL",
                "CONTENT_LENGTH_INVALID",
                "BYTE_BUDGET_EXCEEDED", "UNEXPECTED_REDIRECT",
            }:
                blocked_reason = record["transport_classification"]
            self._append_record(
                record,
                plan=plan,
                blocked_reason=blocked_reason,
            )
            records = self.verify_session(plan=plan)
            if blocked_reason is not None:
                raise BudgetBlockedError(
                    f"session blocked: {blocked_reason}"
                )
            current_id = next_id
        return self.load_records()

    def _is_finalized_or_blocked(
        self,
        records: Sequence[Mapping[str, Any]],
    ) -> bool:
        state = _read_json_file(self.state_path, "session state")
        if state.get("finalized"):
            return True
        if state.get("blocked_reason"):
            return True
        if records and records[-1].get("transport_classification") in {
            "TIMEOUT",
            "TRANSPORT_ERROR",
            "BUDGET_REFUSAL",
            "CONTENT_LENGTH_INVALID",
            "BYTE_BUDGET_EXCEEDED", "UNEXPECTED_REDIRECT",
        }:
            return True
        return False

    def _perform_one(
        self,
        request: PlanRequest,
        plan: SessionPlan,
        *,
        remaining_bytes: int,
        transport: Callable[[str, str, dict[str, str]], Any],
        records: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], str | None]:
        sequence = len(records) + 1
        host = _normalise_host(request.url)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
        }
        try:
            response = transport(request.method, request.url, headers)
        except (socket.timeout, TimeoutError) as error:
            return self._transport_record(
                request,
                plan,
                host,
                sequence,
                remaining_bytes,
                "TIMEOUT",
                detail="timeout",
            ), None
        except Exception as error:
            return self._transport_record(
                request,
                plan,
                host,
                sequence,
                remaining_bytes,
                "TRANSPORT_ERROR",
                detail="transport_error",
            ), None

        return self._response_record(
            request,
            plan,
            response,
            host,
            sequence,
            remaining_bytes,
        )

    def _transport_record(
        self,
        request: PlanRequest,
        plan: SessionPlan,
        host: str,
        sequence: int,
        remaining_bytes: int,
        classification: str,
        *,
        detail: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "timestamp": utc_now(),
            "method": request.method,
            "url": request.url,
            "host": host,
            "status": None,
            "transport_classification": classification,
            "response_body_bytes": 0,
            "response_body_sha256": None,
            "no_body_identity": "no_http_response",
            "content_length": None,
            "redirect_location": None,
            "redirect_followed": False,
            "final_host": host,
            "retained_filename": None,
            "remaining_request_budget": plan.max_requests - sequence,
            "remaining_byte_budget": remaining_bytes,
            "body_measured": False,
            "refusal_reason": None,
            "plan_entry_id": request.id,
            "detail": detail,
        }

    def _response_record(
        self,
        request: PlanRequest,
        plan: SessionPlan,
        response: Any,
        host: str,
        sequence: int,
        remaining_bytes: int,
    ) -> tuple[dict[str, Any], str | None]:
        status = int(response.status)
        content_length = self._parse_content_length(response)
        redirect_location = self._header(response, "location")
        final_host = getattr(response, "final_host", host)

        if content_length is not None and content_length > remaining_bytes:
            return self._refusal_record(
                request=request,
                plan=plan,
                host=host,
                final_host=final_host,
                sequence=sequence,
                status=status,
                remaining_bytes=remaining_bytes,
                content_length=content_length,
                redirect_location=redirect_location,
                classification="BUDGET_REFUSAL",
                reason="content_length_exceeds_remaining_budget",
            ), None

        if content_length is not None:
            body = self._read_exact(response, content_length)
            actual = len(body)
            eof = actual == content_length
            classification = None
            blocked = False
        else:
            body, eof = self._read_streaming(response, remaining_bytes)
            actual = len(body)
            classification = None
            blocked = not eof
            if blocked:
                classification = "BYTE_BUDGET_EXCEEDED"

        body_hash = sha256_bytes(body)
        retained = self._save_body(body, request=request, sequence=sequence)

        redirect_followed = False
        next_id: str | None = request.follow
        if 300 <= status < 400 and status not in (304,):
            if not redirect_location:
                classification = "UNEXPECTED_REDIRECT"
                blocked = True
                next_id = None
            else:
                try:
                    self._validate_redirect_location(redirect_location, plan)
                except RequestPolicyError:
                    classification = "UNEXPECTED_REDIRECT"
                    blocked = True
                    next_id = None
                else:
                    if request.follow is None:
                        classification = "UNEXPECTED_REDIRECT"
                        blocked = True
                        next_id = None
                    else:
                        redirect_followed = True

        record = {
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "timestamp": utc_now(),
            "method": request.method,
            "url": request.url,
            "host": host,
            "status": status,
            "transport_classification": classification,
            "response_body_bytes": actual,
            "response_body_sha256": body_hash,
            "no_body_identity": None,
            "content_length": content_length,
            "redirect_location": redirect_location,
            "redirect_followed": redirect_followed,
            "final_host": final_host,
            "retained_filename": retained,
            "remaining_request_budget": plan.max_requests - sequence,
            "remaining_byte_budget": remaining_bytes - actual,
            "body_measured": True,
            "refusal_reason": None,
            "plan_entry_id": request.id,
            "detail": "truncated_body" if not eof and not blocked else None,
        }
        return record, next_id

    def _header(self, response: Any, name: str) -> str | None:
        getheader = getattr(response, "getheader", None)
        if callable(getheader):
            value = getheader(name)
            return None if value is None else str(value)
        return None

    def _validate_redirect_location(
        self,
        location: str,
        plan: SessionPlan,
    ) -> urllib.parse.SplitResult:
        return _validate_public_url(
            location,
            allowed_hosts=plan.allowed_hosts,
            allow_query=False,
        )

    def _refusal_record(
        self,
        *,
        request: PlanRequest,
        plan: SessionPlan,
        host: str,
        final_host: str,
        sequence: int,
        status: int,
        remaining_bytes: int,
        content_length: int,
        redirect_location: str | None,
        classification: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "timestamp": utc_now(),
            "method": request.method,
            "url": request.url,
            "host": host,
            "status": status,
            "transport_classification": classification,
            "response_body_bytes": 0,
            "response_body_sha256": None,
            "no_body_identity": "body_not_read",
            "content_length": content_length,
            "redirect_location": redirect_location,
            "redirect_followed": False,
            "final_host": final_host,
            "retained_filename": None,
            "remaining_request_budget": plan.max_requests - sequence,
            "remaining_byte_budget": remaining_bytes,
            "body_measured": False,
            "refusal_reason": reason,
            "plan_entry_id": request.id,
            "detail": "body_not_read",
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provenance_capture",
        description="Append-only bounded provenance research logger.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize a new session")
    init_parser.add_argument("--session", default=DEFAULT_SESSION_DIR)
    init_parser.add_argument("--plan", required=True)

    verify_parser = subparsers.add_parser("verify", help="verify an existing session")
    verify_parser.add_argument("--session", default=DEFAULT_SESSION_DIR)

    run_parser = subparsers.add_parser("run", help="execute the authorized session plan")
    run_parser.add_argument("--session", default=DEFAULT_SESSION_DIR)

    finalize_parser = subparsers.add_parser("finalize", help="finalize the session")
    finalize_parser.add_argument("--session", default=DEFAULT_SESSION_DIR)
    return parser


def _load_plan_from_file(path: str | Path) -> SessionPlan:
    value = _read_json_file(Path(path), "session plan")
    return SessionPlan.from_dict(value)


def _structured_output(command: str, success: bool, detail: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "success": success,
        "detail": detail,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            plan = _load_plan_from_file(args.plan)
            session = ProvenanceSession(args.session, plan=plan)
            session.initialize()
            print(json.dumps(
                _structured_output("init", True, plan.plan_id),
                indent=2,
                sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "verify":
            plan_value = _read_json_file(
                Path(args.session) / "session-plan.json",
                "session plan",
            )
            plan = SessionPlan.from_dict(plan_value)
            session = ProvenanceSession(args.session, plan=plan)
            records = session.verify_session()
            print(json.dumps(
                _structured_output(
                    "verify",
                    True,
                    f"records={len(records)} "
                    f"final_hash={records[-1]['current_hash'] if records else None}",
                ),
                indent=2,
                sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "run":
            plan_value = _read_json_file(
                Path(args.session) / "session-plan.json",
                "session plan",
            )
            plan = SessionPlan.from_dict(plan_value)
            session = ProvenanceSession(args.session, plan=plan)
            records = session.execute()
            print(json.dumps(
                _structured_output("run", True, f"records={len(records)}"),
                indent=2,
                sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "finalize":
            plan_value = _read_json_file(
                Path(args.session) / "session-plan.json",
                "session plan",
            )
            plan = SessionPlan.from_dict(plan_value)
            session = ProvenanceSession(args.session, plan=plan)
            summary = session.finalize()
            print(json.dumps(
                _structured_output("finalize", True, summary["final_hash"] or "empty"),
                indent=2,
                sort_keys=True,
            ))
            return EXIT_OK
        raise ProvenanceError(f"unknown command {args.command!r}")
    except ProvenanceError as error:
        print(json.dumps(
            {
                ** _structured_output(args.command, False, str(error)),
                "classification": error.classification,
                "exit_code": error.exit_code,
            },
            indent=2,
            sort_keys=True,
        ))
        return error.exit_code
    except Exception as error:  # pragma: no cover - defensive CLI boundary
        print(json.dumps(
            {
                **_structured_output(args.command, False, f"{type(error).__name__}: {error}"),
                "classification": "INTERNAL_ERROR",
                "exit_code": EXIT_INTERNAL,
            },
            indent=2,
            sort_keys=True,
        ))
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
