#!/usr/bin/env python3
"""Append-only, tamper-evident offline provenance capture logger.

This module is deliberately standard-library-only. It is usable as a tested
library and as a command-line tool. The authoritative ``session.log.jsonl`` file
is append-only and hash-chained. No code path rewrites, resequences, sorts,
truncates, repairs, or manually inserts records into an existing log.

The session directory contains:

    session-plan.json
    session-state.json
    session.log.jsonl
    session-summary.json
    responses/

Raw response bodies are only retained when a plan entry explicitly requests it.
Retained bodies are saved before any analysis and before any redirect decision is
committed. Raw response bodies are not intended to be committed to the repository.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import stat
import sys
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = 2
DEFAULT_MAX_REQUESTS = 10
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_SESSION_DIR = ".tmp/t0027-provenance"
MAX_PLAN_BYTES = 1024 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_SUMMARY_BYTES = 256 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_URL_CHARS = 8192
MAX_PERCENT_DECODE_PASSES = 10
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

BLOCKING_CLASSIFICATIONS = {
    "TIMEOUT",
    "TRANSPORT_ERROR",
    "BUDGET_REFUSAL",
    "CONTENT_LENGTH_INVALID",
    "BYTE_BUDGET_EXCEEDED",
    "UNEXPECTED_REDIRECT",
    "UNEXPECTED_STATUS",
    "RESPONSE_READ_TIMEOUT",
    "RESPONSE_READ_ERROR",
    "RESPONSE_STORAGE_ERROR",
}

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

FOLLOW_REDIRECT_STATUSES = frozenset({300, 301, 302, 303, 307, 308})
REQUEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SAFE_RETAINED_FILENAME_RE = re.compile(r"^[0-9]{4}\.bin$")
PLAN_TOP_LEVEL_KEYS = {
    "schema_version",
    "plan_hash",
    "max_requests",
    "max_bytes",
    "allowed_hosts",
    "requests",
}
PLAN_REQUEST_KEYS = {
    "id",
    "method",
    "url",
    "purpose",
    "allow_query",
    "retain",
    "range_request",
    "expected_statuses",
    "redirect_target_id",
    "redirect_from_id",
}
HTTP_RECORD_TYPE = "HTTP"
RESERVATION_RECORD_TYPE = "REQUEST_RESERVED"
TERMINAL_RECORD_TYPE = "SESSION_FINALIZED"
PROJECTION_FAILURE_RECORD_TYPE = "SESSION_STORAGE_ERROR"


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


class SessionBusyError(ProvenanceError):
    classification = "SESSION_BUSY"
    exit_code = EXIT_SESSION_INVALID


class UnexpectedRedirectError(RequestPolicyError):
    classification = "UNEXPECTED_REDIRECT"


def canonical_json_bytes(value: object) -> bytes:
    """Return canonical UTF-8 JSON with sorted keys and compact separators."""

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


def _reject_constant(value: str) -> None:
    raise SessionInvalidError(f"non-finite JSON constant is not allowed: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
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


def _read_utf8_bounded(path: Path, subject: str, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as error:
        raise SessionInvalidError(f"cannot read {subject} {path}: {error}") from error
    if len(raw) > max_bytes:
        raise SessionInvalidError(
            f"{subject} {path} exceeds the {max_bytes}-byte size limit"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SessionInvalidError(f"{subject} {path} is not valid UTF-8") from error


def _load_json_file_bounded(path: Path, subject: str, max_bytes: int) -> Any:
    return _load_json_text(_read_utf8_bounded(path, subject, max_bytes), subject)


def record_hash(record: Mapping[str, Any]) -> str:
    """Compute the current hash for one log record.

    The ``current_hash`` field is excluded from the canonical representation.
    """

    payload = {key: value for key, value in record.items() if key != "current_hash"}
    return sha256_bytes(canonical_json_bytes(payload))


def verify_record_chain(records: Sequence[Mapping[str, Any]]) -> None:
    """Verify sequence continuity and the complete hash chain."""

    previous = ZERO_HASH
    seen_sequence: set[int] = set()
    for index, raw in enumerate(records, start=1):
        if not isinstance(raw, dict):
            raise SessionInvalidError(f"record {index} must be a JSON object")
        sequence = raw.get("sequence")
        if sequence != index:
            raise SessionInvalidError(f"record sequence is {sequence!r}; expected {index}")
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


def _validate_exact_plan_prefix(
    plan: "SessionPlan",
    records: Sequence[Mapping[str, Any]],
    *,
    allow_pending_reservation: bool = False,
) -> None:
    terminal = bool(
        records
        and records[-1].get("record_type") == TERMINAL_RECORD_TYPE
    )
    non_terminal = list(records[:-1]) if terminal else list(records)
    pending_reservation = None
    if allow_pending_reservation and non_terminal and non_terminal[-1].get("record_type") == RESERVATION_RECORD_TYPE:
        pending_reservation = non_terminal[-1]
        non_terminal = non_terminal[:-1]
    if len(non_terminal) % 2 != 0:
        raise SessionInvalidError(
            "session log does not contain complete reservation/result pairs"
        )
    pair_count = len(non_terminal) // 2
    if pair_count + (1 if pending_reservation is not None else 0) > len(plan.requests):
        raise SessionInvalidError(
            "session log contains more attempts than the plan allows"
        )
    for index in range(pair_count):
        request = plan.requests[index]
        reservation = non_terminal[2 * index]
        result = non_terminal[2 * index + 1]
        if reservation.get("record_type") != RESERVATION_RECORD_TYPE:
            raise SessionInvalidError(
                f"expected reservation record before request {request.id!r}"
            )
        if reservation.get("plan_entry_id") != request.id:
            raise SessionInvalidError(
                f"reservation record plan_entry_id does not match {request.id!r}"
            )
        if result.get("record_type") != HTTP_RECORD_TYPE:
            raise SessionInvalidError(
                f"expected HTTP result record for request {request.id!r}"
            )
        if result.get("plan_entry_id") != request.id:
            raise SessionInvalidError(
                f"result record plan_entry_id does not match {request.id!r}"
            )
    if pending_reservation is not None:
        expected_id = plan.requests[pair_count].id
        if pending_reservation.get("plan_entry_id") != expected_id:
            raise SessionInvalidError(
                f"pending reservation plan_entry_id does not match {expected_id!r}"
            )


def _normalise_host(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if not host:
        raise PlanValidationError(f"URL is missing a host: {url!r}")
    return host.lower()


def _parse_boolean_field(
    raw: Mapping[str, Any],
    *,
    index: int,
    name: str,
) -> bool:
    value = raw.get(name, False)
    if type(value) is not bool:
        raise PlanValidationError(
            f"requests[{index}].{name} must be a boolean"
        )
    return value


def _validate_request_id(request_id: str) -> None:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise PlanValidationError(
            f"request id must match {REQUEST_ID_RE.pattern!r}: {request_id!r}"
        )
    lowered = request_id.lower()
    if lowered in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }:
        raise PlanValidationError(f"request id is Windows-unsafe: {request_id!r}")
    if request_id.endswith((".", " ")):
        raise PlanValidationError(f"request id has an unsafe suffix: {request_id!r}")


def canonical_url_identity(url: str) -> tuple[str, str, int, str, str]:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    if not host:
        raise PlanValidationError(f"URL is missing a host: {url!r}")
    return (
        parts.scheme.lower(),
        host.lower(),
        parts.port or 443,
        parts.path or "/",
        parts.query,
    )


def _screen_forbidden_body_markers(value: str) -> None:
    current = value.lower()
    for _ in range(MAX_PERCENT_DECODE_PASSES):
        if any(token in current for token in FORBIDDEN_BODY_PATH_FRAGMENTS):
            raise RequestPolicyError(
                "checkpoint/weight body markers are not authorized"
            )
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            return
        current = decoded
    raise RequestPolicyError(
        "URL percent-decoding did not stabilize within the security bound"
    )


def _validate_public_url(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    allow_query: bool,
) -> urllib.parse.SplitResult:
    if not isinstance(url, str) or not url:
        raise RequestPolicyError("URL must be a non-empty string")
    if len(url) > MAX_URL_CHARS:
        raise RequestPolicyError(
            f"URL exceeds the {MAX_URL_CHARS}-character size limit"
        )
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise RequestPolicyError(f"URL has an invalid port: {error}") from error
    if parsed.scheme.lower() != "https":
        raise RequestPolicyError("only HTTPS URLs are authorized")
    if not parsed.hostname:
        raise RequestPolicyError("URL is missing a host")
    if parsed.username is not None or parsed.password is not None:
        raise RequestPolicyError("URL must not contain credentials")
    if parsed.fragment:
        raise RequestPolicyError("URL must not contain a fragment")
    if port is not None and port != 443:
        raise RequestPolicyError("only the default HTTPS port is authorized")
    if parsed.query and not allow_query:
        raise RequestPolicyError("URL query strings are not authorized")
    host = parsed.hostname.lower()
    if host not in allowed_hosts:
        raise RequestPolicyError(f"host is not authorized: {host!r}")
    _screen_forbidden_body_markers(parsed.path)
    _screen_forbidden_body_markers(parsed.query)
    return parsed


def urls_match_exactly(left: str, right: str) -> bool:
    """Compare two already-policy-valid HTTPS URLs under the documented contract."""

    left_parts = urllib.parse.urlsplit(left)
    right_parts = urllib.parse.urlsplit(right)
    if left_parts.scheme.lower() != right_parts.scheme.lower():
        return False
    if left_parts.hostname.lower() != right_parts.hostname.lower():
        return False
    left_port = left_parts.port or 443
    right_port = right_parts.port or 443
    if left_port != right_port:
        return False
    if left_parts.path != right_parts.path:
        return False
    if left_parts.query != right_parts.query:
        return False
    if left_parts.fragment or right_parts.fragment:
        return False
    return True


def _request_canonical(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": request["id"],
        "method": request["method"],
        "url": request["url"],
        "purpose": request["purpose"],
        "allow_query": request["allow_query"],
        "retain": request["retain"],
        "range_request": request["range_request"],
        "expected_statuses": sorted(request["expected_statuses"]),
        "redirect_target_id": request["redirect_target_id"],
        "redirect_from_id": request["redirect_from_id"],
    }


def compute_plan_hash(plan_payload: Mapping[str, Any]) -> str:
    """Compute the canonical immutable SHA-256 for a validated plan payload."""

    payload = {
        "schema_version": plan_payload["schema_version"],
        "max_requests": plan_payload["max_requests"],
        "max_bytes": plan_payload["max_bytes"],
        "allowed_hosts": sorted(plan_payload["allowed_hosts"]),
        "requests": [
            _request_canonical(request)
            for request in plan_payload["requests"]
        ],
    }
    return sha256_bytes(canonical_json_bytes(payload))


def _locked_session(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def wrapper(self: "ProvenanceSession", *args: Any, **kwargs: Any) -> Any:
        with self._session_lock():
            return method(self, *args, **kwargs)
    return wrapper


@dataclass(frozen=True, slots=True)
class PlanRequest:
    id: str
    method: str
    url: str
    purpose: str
    allow_query: bool = False
    retain: bool = False
    range_request: bool = False
    expected_statuses: tuple[int, ...] = ()
    redirect_target_id: str | None = None
    redirect_from_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "method": self.method,
            "url": self.url,
            "purpose": self.purpose,
            "allow_query": self.allow_query,
            "retain": self.retain,
            "range_request": self.range_request,
            "expected_statuses": sorted(self.expected_statuses),
            "redirect_target_id": self.redirect_target_id,
            "redirect_from_id": self.redirect_from_id,
        }


class SessionPlan:
    """A validated, immutable, ordered request plan."""

    def __init__(
        self,
        *,
        plan_hash: str,
        max_requests: int,
        max_bytes: int,
        allowed_hosts: frozenset[str],
        requests: tuple[PlanRequest, ...],
    ) -> None:
        self.plan_hash = plan_hash
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.allowed_hosts = allowed_hosts
        self.requests = requests
        self._by_id = {request.id: request for request in requests}

    def get(self, request_id: str) -> PlanRequest:
        try:
            return self._by_id[request_id]
        except KeyError as error:
            raise PlanValidationError(f"unknown plan request id: {request_id!r}") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_hash": self.plan_hash,
            "max_requests": self.max_requests,
            "max_bytes": self.max_bytes,
            "allowed_hosts": sorted(self.allowed_hosts),
            "requests": [request.to_dict() for request in self.requests],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionPlan":
        if not isinstance(value, dict):
            raise PlanValidationError("session plan must be a JSON object")
        unknown_top = set(value) - PLAN_TOP_LEVEL_KEYS
        if unknown_top:
            raise PlanValidationError(
                f"unknown session plan field(s): {', '.join(sorted(unknown_top))}"
            )
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
        if not 1 <= max_bytes <= DEFAULT_MAX_BYTES:
            raise PlanValidationError(
                f"max_bytes must be between 1 and {DEFAULT_MAX_BYTES}"
            )

        raw_allowed = value.get("allowed_hosts")
        if not isinstance(raw_allowed, list) or not raw_allowed:
            raise PlanValidationError("allowed_hosts must be a non-empty list")
        allowed_hosts: set[str] = set()
        for host in raw_allowed:
            if not isinstance(host, str) or not host or "://" in host:
                raise PlanValidationError("allowed_hosts entries must be bare hostnames")
            lowered_host = host.lower()
            if lowered_host in allowed_hosts:
                raise PlanValidationError(
                    f"duplicate allowed host: {lowered_host!r}"
                )
            allowed_hosts.add(lowered_host)

        raw_requests = value.get("requests")
        if not isinstance(raw_requests, list) or not raw_requests:
            raise PlanValidationError("requests must be a non-empty list")
        if len(raw_requests) > DEFAULT_MAX_REQUESTS:
            raise PlanValidationError(
                f"requests must contain at most {DEFAULT_MAX_REQUESTS} slots"
            )
        if len(raw_requests) > max_requests:
            raise PlanValidationError(
                f"request count {len(raw_requests)} exceeds max_requests {max_requests}"
            )

        requests: list[PlanRequest] = []
        ids: set[str] = set()
        urls: set[str] = set()
        for index, raw in enumerate(raw_requests):
            if not isinstance(raw, dict):
                raise PlanValidationError(f"requests[{index}] must be an object")
            unknown_request = set(raw) - PLAN_REQUEST_KEYS
            if unknown_request:
                raise PlanValidationError(
                    f"requests[{index}] has unknown field(s): "
                    f"{', '.join(sorted(unknown_request))}"
                )
            request_id = raw.get("id")
            if not isinstance(request_id, str) or not request_id:
                raise PlanValidationError(
                    f"requests[{index}].id must be a non-empty string"
                )
            if request_id in ids:
                raise PlanValidationError(f"duplicate request id: {request_id}")
            _validate_request_id(request_id)
            ids.add(request_id)
            method = str(raw.get("method", "GET")).upper()
            if method != "GET":
                raise PlanValidationError(f"requests[{index}].method must be GET")
            url = raw.get("url")
            if not isinstance(url, str) or not url:
                raise PlanValidationError(f"requests[{index}].url must be a non-empty string")
            allow_query = _parse_boolean_field(
                raw,
                index=index,
                name="allow_query",
            )
            retain = _parse_boolean_field(
                raw,
                index=index,
                name="retain",
            )
            range_request = _parse_boolean_field(
                raw,
                index=index,
                name="range_request",
            )
            try:
                _validate_public_url(
                    url,
                    allowed_hosts=frozenset(allowed_hosts),
                    allow_query=allow_query,
                )
            except RequestPolicyError as error:
                raise PlanValidationError(str(error)) from error
            try:
                url_identity = canonical_url_identity(url)
            except (ValueError, PlanValidationError) as error:
                raise PlanValidationError(str(error)) from error
            if url_identity in urls:
                raise PlanValidationError(
                    f"canonically duplicate request URL: {url}"
                )
            urls.add(url_identity)
            purpose = raw.get("purpose")
            if not isinstance(purpose, str) or not purpose:
                raise PlanValidationError(
                    f"requests[{index}].purpose must be a non-empty string"
                )
            raw_statuses = raw.get("expected_statuses", [])
            if not isinstance(raw_statuses, list):
                raise PlanValidationError(
                    f"requests[{index}].expected_statuses must be a list"
                )
            statuses: list[int] = []
            for status in raw_statuses:
                if (
                    not isinstance(status, int)
                    or isinstance(status, bool)
                    or not 100 <= status <= 599
                ):
                    raise PlanValidationError(
                        f"requests[{index}].expected_statuses contains an invalid status"
                    )
                if status in statuses:
                    raise PlanValidationError(
                        f"requests[{index}].expected_statuses contains duplicates"
                    )
                statuses.append(status)
            redirect_target_id = raw.get("redirect_target_id")
            if redirect_target_id is not None and not isinstance(redirect_target_id, str):
                raise PlanValidationError(
                    f"requests[{index}].redirect_target_id must be a string when present"
                )
            redirect_from_id = raw.get("redirect_from_id")
            if redirect_from_id is not None and not isinstance(redirect_from_id, str):
                raise PlanValidationError(
                    f"requests[{index}].redirect_from_id must be a string when present"
                )
            if redirect_target_id is not None and redirect_from_id is not None:
                raise PlanValidationError(
                    f"requests[{index}] cannot be both a redirect source and target"
                )
            if redirect_target_id is not None:
                if not statuses or any(
                    status not in FOLLOW_REDIRECT_STATUSES
                    for status in statuses
                ):
                    raise PlanValidationError(
                        f"requests[{index}].expected_statuses must contain only "
                        f"supported followable redirect statuses"
                    )
            requests.append(
                PlanRequest(
                    id=request_id,
                    method=method,
                    url=url,
                    purpose=purpose,
                    allow_query=allow_query,
                    retain=retain,
                    range_request=range_request,
                    expected_statuses=tuple(sorted(statuses)),
                    redirect_target_id=redirect_target_id,
                    redirect_from_id=redirect_from_id,
                )
            )

        by_id = {request.id: request for request in requests}
        _validate_redirect_graph(requests, by_id)
        computed = compute_plan_hash(
            {
                "schema_version": SCHEMA_VERSION,
                "max_requests": max_requests,
                "max_bytes": max_bytes,
                "allowed_hosts": sorted(allowed_hosts),
                "requests": [request.to_dict() for request in requests],
            }
        )
        provided = value.get("plan_hash")
        if provided is not None and provided != computed:
            raise PlanValidationError("plan_hash does not match the canonical plan")
        return cls(
            plan_hash=computed,
            max_requests=max_requests,
            max_bytes=max_bytes,
            allowed_hosts=frozenset(allowed_hosts),
            requests=tuple(requests),
        )


def _validate_redirect_graph(
    requests: Sequence[PlanRequest],
    by_id: Mapping[str, PlanRequest],
) -> None:
    in_degree: dict[str, int] = {request.id: 0 for request in requests}
    for index, request in enumerate(requests):
        if request.redirect_target_id is None:
            continue
        target_id = request.redirect_target_id
        if target_id not in by_id:
            raise PlanValidationError(
                f"request {request.id!r} redirects to unknown id {target_id!r}"
            )
        if target_id == request.id:
            raise PlanValidationError(f"self-redirect is not authorized: {request.id!r}")
        target = by_id[target_id]
        if target.redirect_from_id != request.id:
            raise PlanValidationError(
                f"redirect source/target linkage mismatch: "
                f"{request.id!r} -> {target_id!r}"
            )
        if in_degree[target_id]:
            raise PlanValidationError(
                f"redirect target {target_id!r} is named by multiple source entries"
            )
        in_degree[target_id] += 1
        if index + 1 >= len(requests) or requests[index + 1].id != target_id:
            raise PlanValidationError(
                f"redirect target {target_id!r} must be the immediate next planned request"
            )

    for index, request in enumerate(requests):
        if request.redirect_from_id is None:
            continue
        source_id = request.redirect_from_id
        if source_id not in by_id:
            raise PlanValidationError(
                f"request {request.id!r} has unknown redirect source {source_id!r}"
            )
        source = by_id[source_id]
        if source.redirect_target_id != request.id:
            raise PlanValidationError(
                f"redirect target {request.id!r} does not match its source "
                f"{source_id!r} linkage"
            )
        if in_degree[request.id] != 1:
            raise PlanValidationError(
                f"redirect target {request.id!r} must have exactly one source"
            )


class _HTTPTransport:
    """Real HTTPS transport with automatic redirects disabled."""

    def __init__(self, timeout: float = TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def __call__(self, method: str, url: str, headers: dict[str, str]) -> Any:
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
    """Manage one bounded, append-only provenance-capture session."""

    def __init__(self, session_dir: str | Path, *, plan: SessionPlan | None = None) -> None:
        self.root = Path(session_dir)
        self.plan = plan
        self.log_path = self.root / "session.log.jsonl"
        self.state_path = self.root / "session-state.json"
        self.summary_path = self.root / "session-summary.json"
        self.plan_path = self.root / "session-plan.json"
        self.responses_dir = self.root / "responses"
        self.lock_path = self.root / ".session.lock"

    def _reject_symlink_components(self, path: Path) -> None:
        root = self.root
        candidate = path
        while True:
            if candidate.is_symlink():
                raise SessionInvalidError(
                    f"session path contains a symlink component: {candidate}"
                )
            if candidate == root:
                break
            parent = candidate.parent
            if parent == candidate:
                break
            candidate = parent

    def _validate_session_root(self) -> None:
        absolute = os.path.normcase(os.path.abspath(str(self.root)))
        real = os.path.normcase(os.path.realpath(str(self.root)))
        if absolute != real:
            raise SessionInvalidError(
                "session root is reached through a symlink or junction"
            )
        self._reject_symlink_components(self.root)
        if self.root.exists() and not self.root.is_dir():
            raise SessionInvalidError("session root must be a directory")

    def _validate_authoritative_path(self, path: Path, *, directory: bool = False) -> None:
        self._reject_symlink_components(path)
        resolved_root = self.root.resolve()
        resolved_path = path.resolve()
        if resolved_path.parent != resolved_root:
            raise SessionInvalidError(
                f"session path escapes the canonical session root: {path}"
            )
        if directory:
            if not resolved_path.is_dir():
                raise SessionInvalidError(f"expected directory is missing: {path}")
        else:
            if resolved_path.exists():
                if not resolved_path.is_file():
                    raise SessionInvalidError(
                        f"authoritative path must be a regular file: {path}"
                    )
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise SessionInvalidError(
                        f"authoritative path must be a single-link regular file: {path}"
                    )

    def _validate_authoritative_files(self) -> None:
        self._validate_session_root()
        for path in (
            self.plan_path,
            self.state_path,
            self.summary_path,
            self.log_path,
        ):
            self._validate_authoritative_path(path)
        self._validate_authoritative_path(self.responses_dir, directory=True)
        if self.lock_path.is_symlink():
            raise SessionInvalidError("session lock path must not be a symlink")

    def _assert_regular_single_link_fd(
        self,
        fd: int,
        path: Path,
        subject: str,
    ) -> None:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise SessionInvalidError(
                f"{subject} is not a single-link regular file: {path}"
            )

    def _assert_fd_matches_path(self, fd: int, path: Path, subject: str) -> None:
        fd_st = os.fstat(fd)
        path_st = path.lstat()
        if (fd_st.st_dev, fd_st.st_ino) != (path_st.st_dev, path_st.st_ino):
            raise SessionInvalidError(
                f"{subject} path and descriptor identity do not match: {path}"
            )

    def _open_exclusive_regular_file(
        self,
        path: Path,
        subject: str,
    ) -> int:
        self._reject_symlink_components(path)
        if path.exists() or path.is_symlink():
            raise SessionInvalidError(
                f"{subject} already exists; refusing to overwrite: {path}"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError as error:
            raise SessionInvalidError(
                f"{subject} already exists; refusing to overwrite: {path}"
            ) from error
        except OSError as error:
            raise SessionInvalidError(
                f"cannot create {subject} {path}: {error}"
            ) from error
        self._assert_regular_single_link_fd(fd, path, subject)
        self._assert_fd_matches_path(fd, path, subject)
        return fd

    def _write_all_fd(self, fd: int, data: bytes, subject: str) -> None:
        view = memoryview(data)
        total = 0
        while total < len(data):
            try:
                written = os.write(fd, view[total:])
            except OSError as error:
                raise SessionInvalidError(
                    f"cannot write {subject}: {error}"
                ) from error
            if written <= 0:
                raise SessionInvalidError(
                    f"short write while writing {subject}"
                )
            total += written

    def _write_exclusive_bytes(
        self,
        path: Path,
        data: bytes,
        subject: str,
    ) -> None:
        fd = self._open_exclusive_regular_file(path, subject)
        try:
            self._write_all_fd(fd, data, subject)
            try:
                os.fsync(fd)
            except OSError as error:
                raise SessionInvalidError(
                    f"cannot fsync {subject} {path}: {error}"
                ) from error
        except SessionInvalidError:
            os.close(fd)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        except Exception:
            os.close(fd)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        os.close(fd)

    @contextmanager
    def _session_lock(self) -> Any:
        self._validate_session_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self._validate_authoritative_files()
        if self.lock_path.is_symlink():
            raise SessionInvalidError("session lock path must not be a symlink")
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError as error:
            raise SessionBusyError("session is busy; lock already held") from error
        except OSError as error:
            raise SessionInvalidError(f"cannot create session lock: {error}") from error
        try:
            yield
        finally:
            os.close(fd)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def _ensure_plan(self) -> SessionPlan:
        if self.plan is None:
            raise PlanValidationError("no session plan is attached")
        return self.plan

    def _plan_from_disk(self) -> SessionPlan:
        value = _load_json_file_bounded(
            self.plan_path,
            "session plan",
            MAX_PLAN_BYTES,
        )
        plan_hash = value.get("plan_hash")
        if (
            not isinstance(plan_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", plan_hash)
        ):
            raise SessionInvalidError(
                "stored session plan is missing a valid lowercase SHA-256 plan_hash"
            )
        return SessionPlan.from_dict(value)

    def _read_state(self) -> dict[str, Any]:
        value = _load_json_file_bounded(
            self.state_path,
            "session state",
            MAX_STATE_BYTES,
        )
        if not isinstance(value, dict):
            raise SessionInvalidError("session state must be a JSON object")
        return value

    def _read_summary(self) -> dict[str, Any]:
        value = _load_json_file_bounded(
            self.summary_path,
            "session summary",
            MAX_SUMMARY_BYTES,
        )
        if not isinstance(value, dict):
            raise SessionInvalidError("session summary must be a JSON object")
        return value

    def initialize(self, plan: SessionPlan | None = None) -> None:
        plan = plan or self._ensure_plan()
        self.plan = plan
        with self._session_lock():
            if self.log_path.exists() and self.log_path.stat().st_size > 0:
                raise SessionInvalidError(
                    f"session log already exists; refusing to reinitialize {self.log_path}"
                )
            if self.state_path.exists() or self.summary_path.exists():
                raise SessionInvalidError(
                    "session state or summary already exists; refusing to reinitialize"
                )
            session_id = uuid.uuid4().hex
            created_utc = utc_now()
            self._write_json_atomic(self.plan_path, plan.to_dict(), "session plan")
            self._write_json_atomic(
                self.state_path,
                self._new_state(
                    plan,
                    [],
                    blocked_reason=None,
                    session_id=session_id,
                    created_utc=created_utc,
                    finalized=False,
                ),
                "session state",
            )
            self._write_json_atomic(
                self.summary_path,
                self._summary_from(
                    [],
                    plan,
                    session_id=session_id,
                    blocked_reason=None,
                    finalized=False,
                ),
                "session summary",
            )
            if not self.log_path.exists():
                fd = self._open_exclusive_regular_file(
                    self.log_path,
                    "session log",
                )
                os.close(fd)

    def _write_json_atomic(self, path: Path, value: Any, subject: str) -> None:
        self._validate_authoritative_path(path)
        temporary = path.with_name(path.name + ".tmp")
        data = (
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        self._write_exclusive_bytes(temporary, data, subject)
        self._validate_authoritative_path(temporary)
        self._validate_authoritative_path(path)
        try:
            os.replace(temporary, path)
        except OSError as error:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise SessionInvalidError(
                f"cannot replace {subject} {path}: {error}"
            ) from error
        self._validate_authoritative_path(path)

    def _http_records(
        self,
        records: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            dict(record)
            for record in records
            if record.get("record_type")
            not in {TERMINAL_RECORD_TYPE, RESERVATION_RECORD_TYPE}
        ]

    def _blocked_reason_from_records(
        self,
        records: Sequence[Mapping[str, Any]],
    ) -> str | None:
        for record in reversed(records):
            if record.get("record_type") in {
                TERMINAL_RECORD_TYPE,
                RESERVATION_RECORD_TYPE,
            }:
                continue
            return self._is_blocking_classification(record)
        return None

    def _terminal_from_records(
        self,
        records: Sequence[Mapping[str, Any]],
    ) -> bool:
        return bool(
            records
            and records[-1].get("record_type") == TERMINAL_RECORD_TYPE
        )

    def _new_state(
        self,
        plan: SessionPlan,
        records: Sequence[Mapping[str, Any]],
        *,
        blocked_reason: str | None,
        session_id: str,
        created_utc: str,
        finalized: bool,
    ) -> dict[str, Any]:
        http_records = self._http_records(records)
        aggregate = sum(
            int(record.get("response_body_bytes", 0))
            for record in http_records
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "plan_sha256": plan.plan_hash,
            "request_count": len(http_records),
            "aggregate_bytes": aggregate,
            "remaining_requests": plan.max_requests - len(http_records),
            "remaining_bytes": plan.max_bytes - aggregate,
            "completed_entries": sorted(
                {
                    record["plan_entry_id"]
                    for record in http_records
                    if "plan_entry_id" in record
                }
            ),
            "blocked_reason": blocked_reason,
            "finalized": finalized,
            "created_utc": created_utc,
        }

    def _summary_from(
        self,
        records: Sequence[Mapping[str, Any]],
        plan: SessionPlan,
        *,
        session_id: str,
        blocked_reason: str | None,
        finalized: bool,
    ) -> dict[str, Any]:
        http_records = self._http_records(records)
        aggregate = sum(
            int(record.get("response_body_bytes", 0))
            for record in http_records
        )
        final_hash = records[-1]["current_hash"] if records else None
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "plan_sha256": plan.plan_hash,
            "request_count": len(http_records),
            "aggregate_bytes": aggregate,
            "final_hash": final_hash,
            "blocked_reason": blocked_reason,
            "finalized": finalized,
        }

    def load_records(self) -> list[dict[str, Any]]:
        text = _read_utf8_bounded(self.log_path, "session log", MAX_LOG_BYTES)
        lines = text.splitlines()
        return _parse_log_lines(lines)

    def verify_session(
        self,
        *,
        allow_pending_retained: bool = False,
        allow_pending_reservation: bool = False,
    ) -> list[dict[str, Any]]:
        self._validate_authoritative_files()
        try:
            plan = self._plan_from_disk()
        except PlanValidationError as error:
            raise SessionInvalidError(str(error)) from error
        records = self.load_records()
        state = self._read_state()
        summary = self._read_summary()
        if state.get("schema_version") != SCHEMA_VERSION:
            raise SessionInvalidError("unsupported session state schema_version")
        if summary.get("schema_version") != SCHEMA_VERSION:
            raise SessionInvalidError("unsupported session summary schema_version")
        if state.get("plan_sha256") != plan.plan_hash:
            raise SessionInvalidError(
                "session plan has changed after initialization"
            )
        if summary.get("plan_sha256") != plan.plan_hash:
            raise SessionInvalidError("session summary plan hash does not match the plan")
        if state.get("session_id") != summary.get("session_id"):
            raise SessionInvalidError("session state and summary session IDs do not match")
        _validate_exact_plan_prefix(
            plan,
            records,
            allow_pending_reservation=allow_pending_reservation,
        )
        http_records = self._http_records(records)
        aggregate = sum(
            int(record.get("response_body_bytes", 0))
            for record in http_records
        )
        terminal = self._terminal_from_records(records)
        blocked_reason = self._blocked_reason_from_records(records)
        if state.get("request_count") != len(http_records):
            raise SessionInvalidError("session state request count does not match the log")
        if state.get("aggregate_bytes") != aggregate:
            raise SessionInvalidError("session state aggregate bytes do not match the log")
        if summary.get("request_count") != len(http_records):
            raise SessionInvalidError("session summary request count does not match the log")
        if summary.get("aggregate_bytes") != aggregate:
            raise SessionInvalidError("session summary aggregate bytes do not match the log")
        if state.get("blocked_reason") != blocked_reason:
            raise SessionInvalidError(
                "session state blocked_reason does not match the authoritative log"
            )
        if summary.get("blocked_reason") != blocked_reason:
            raise SessionInvalidError(
                "session summary blocked_reason does not match the authoritative log"
            )
        if state.get("finalized") != terminal:
            raise SessionInvalidError(
                "session state finalized flag does not match the authoritative log"
            )
        if summary.get("finalized") != terminal:
            raise SessionInvalidError(
                "session summary finalized flag does not match the authoritative log"
            )
        expected_completed = sorted(
            {
                record["plan_entry_id"]
                for record in http_records
                if "plan_entry_id" in record
            }
        )
        if state.get("completed_entries") != expected_completed:
            raise SessionInvalidError("session state completed entries do not match the log")
        expected_remaining_requests = plan.max_requests - len(http_records)
        expected_remaining_bytes = plan.max_bytes - aggregate
        if state.get("remaining_requests") != expected_remaining_requests:
            raise SessionInvalidError("session state remaining requests are invalid")
        if state.get("remaining_bytes") != expected_remaining_bytes:
            raise SessionInvalidError("session state remaining bytes are invalid")
        expected_final_hash = records[-1]["current_hash"] if records else None
        if summary.get("final_hash") != expected_final_hash:
            raise SessionInvalidError("session summary final hash is invalid")
        for record in records:
            if record.get("session_id") != state.get("session_id"):
                raise SessionInvalidError("record session ID does not match session state")
            if record.get("plan_sha256") != plan.plan_hash:
                raise SessionInvalidError("record plan hash does not match the plan")
            if record.get("record_type") == TERMINAL_RECORD_TYPE:
                continue
            entry_id = record.get("plan_entry_id")
            if entry_id not in {request.id for request in plan.requests}:
                raise SessionInvalidError(
                    f"record references unknown plan entry id: {entry_id!r}"
                )
        self._validate_redirect_follow_chain(plan, records)
        self._verify_retained_bodies(
            records,
            allow_pending_retained=allow_pending_retained,
        )
        return records

    def _is_blocking_classification(self, record: Mapping[str, Any]) -> str | None:
        classification = record.get("transport_classification")
        if classification in BLOCKING_CLASSIFICATIONS:
            return classification
        return None

    def _validate_redirect_follow_chain(
        self,
        plan: SessionPlan,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        http_records = self._http_records(records)
        for index, record in enumerate(http_records):
            request = plan.requests[index]
            if request.redirect_from_id is not None:
                if index == 0:
                    raise SessionInvalidError(
                        f"redirect target {request.id!r} has no preceding source record"
                    )
                source = plan.requests[index - 1]
                source_record = http_records[index - 1]
                status = source_record.get("status")
                source_authorized = (
                    source_record.get("plan_entry_id") == source.id
                    and isinstance(status, int)
                    and 300 <= status < 400
                    and status != 304
                    and source_record.get("redirect_authorized") is True
                    and source_record.get("redirect_exact_match") is True
                    and source_record.get("redirect_followed") is False
                    and source_record.get("redirect_target_id") == request.id
                    and self._is_blocking_classification(source_record) is None
                )
                target_authorized = (
                    record.get("redirect_followed") is True
                    and record.get("redirect_source_entry_id") == source.id
                    and record.get("redirect_source_record_hash")
                    == source_record.get("current_hash")
                )
                if not source_authorized or not target_authorized:
                    raise SessionInvalidError(
                        f"adjacent redirect records do not prove an authorized follow "
                        f"for target {request.id!r}"
                    )
            elif request.redirect_target_id is not None:
                if record.get("redirect_followed") is not False:
                    raise SessionInvalidError(
                        f"redirect source {request.id!r} must not claim "
                        f"redirect_followed before its target executes"
                    )

    def _safe_retained_target(self, filename: str) -> Path:
        if not SAFE_RETAINED_FILENAME_RE.fullmatch(filename):
            raise SessionInvalidError(f"unsafe retained filename: {filename!r}")
        self._validate_session_root()
        self._reject_symlink_components(self.responses_dir)
        root = self.responses_dir.resolve()
        candidate = self.responses_dir / filename
        self._reject_symlink_components(candidate)
        resolved = candidate.resolve()
        if resolved.parent != root:
            raise SessionInvalidError(
                f"retained path escapes responses directory: {filename!r}"
            )
        return resolved

    def _verify_retained_bodies(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        allow_pending_retained: bool = False,
    ) -> None:
        http_records = self._http_records(records)
        referenced: dict[str, Mapping[str, Any]] = {}
        for record in http_records:
            filename = record.get("retained_filename")
            if filename is None:
                continue
            if not isinstance(filename, str):
                raise SessionInvalidError("retained_filename must be a string")
            if filename in referenced:
                raise SessionInvalidError(
                    f"duplicate retained filename: {filename!r}"
                )
            referenced[filename] = record

        if not self.responses_dir.exists():
            raise SessionInvalidError("responses directory is missing")
        if self.responses_dir.is_symlink():
            raise SessionInvalidError("responses directory must not be a symlink")
        allowed_pending = set()
        if allow_pending_retained:
            allowed_pending.add(f"{len(http_records) + 1:04d}.bin")
        unexpected = [
            entry.name
            for entry in self.responses_dir.iterdir()
            if entry.name not in referenced
            and entry.name not in allowed_pending
        ]
        if unexpected:
            raise SessionInvalidError(
                f"unexpected retained files: {', '.join(sorted(unexpected))}"
            )

        for filename, record in referenced.items():
            target = self._safe_retained_target(filename)
            if not target.is_file():
                raise SessionInvalidError(
                    f"retained body is not a regular file: {filename!r}"
                )
            try:
                st = target.lstat()
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise SessionInvalidError(
                        f"retained body must be a single-link regular file: {filename!r}"
                    )
                size = st.st_size
                body = target.read_bytes()
            except SessionInvalidError:
                raise
            except OSError as error:
                raise SessionInvalidError(
                    f"cannot read retained body {target}: {error}"
                ) from error
            if size != record.get("response_body_bytes"):
                raise SessionInvalidError(
                    f"retained body size mismatch for {filename!r}"
                )
            if sha256_bytes(body) != record.get("response_body_sha256"):
                raise SessionInvalidError(
                    f"retained body hash mismatch for {filename!r}"
                )

    def _ensure_writable(self) -> None:
        records = self.verify_session()
        if self._terminal_from_records(records):
            raise SessionFinalizedError(
                "session is finalized; refusing additional requests"
            )
        blocked_reason = self._blocked_reason_from_records(records)
        if blocked_reason:
            raise SessionFinalizedError(
                f"session is blocked; refusing additional requests: {blocked_reason}"
            )

    def _append_record(
        self,
        record: Mapping[str, Any],
        *,
        plan: SessionPlan,
    ) -> dict[str, Any]:
        records = self.verify_session(
            allow_pending_retained=True,
            allow_pending_reservation=True,
        )
        state = self._read_state()
        session_id = state.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise SessionInvalidError("session state is missing a valid session_id")
        sequence = len(records) + 1
        previous_hash = records[-1]["current_hash"] if records else ZERO_HASH
        full_record = dict(record)
        full_record.setdefault("record_type", HTTP_RECORD_TYPE)
        full_record.update(
            {
                "schema_version": SCHEMA_VERSION,
                "sequence": sequence,
                "previous_hash": previous_hash,
                "session_id": session_id,
                "plan_sha256": plan.plan_hash,
            }
        )
        full_record["current_hash"] = record_hash(full_record)
        self._append_jsonl_line(full_record)
        records.append(full_record)
        blocked_reason = self._blocked_reason_from_records(records)
        finalized = full_record.get("record_type") == TERMINAL_RECORD_TYPE
        try:
            self._write_json_atomic(
                self.state_path,
                self._new_state(
                    plan,
                    records,
                    blocked_reason=blocked_reason,
                    session_id=session_id,
                    created_utc=state.get("created_utc", utc_now()),
                    finalized=finalized,
                ),
                "session state",
            )
            self._write_json_atomic(
                self.summary_path,
                self._summary_from(
                    records,
                    plan,
                    session_id=session_id,
                    blocked_reason=blocked_reason,
                    finalized=finalized,
                ),
                "session summary",
            )
        except ProvenanceError:
            self._append_projection_failure_record(full_record)
            raise
        return full_record

    def _append_jsonl_line(self, record: Mapping[str, Any]) -> None:
        line = (
            json.dumps(
                record,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        self._validate_authoritative_path(self.log_path)
        flags = os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.log_path, flags, 0o600)
        except OSError as error:
            raise SessionInvalidError(
                f"cannot append to session log {self.log_path}: {error}"
            ) from error
        self._assert_regular_single_link_fd(fd, self.log_path, "session log")
        self._assert_fd_matches_path(fd, self.log_path, "session log")
        try:
            self._write_all_fd(fd, line, "session log")
            try:
                os.fsync(fd)
            except OSError as error:
                raise SessionInvalidError(
                    f"cannot fsync session log {self.log_path}: {error}"
                ) from error
        finally:
            os.close(fd)

    def _append_projection_failure_record(
        self,
        full_record: Mapping[str, Any],
    ) -> None:
        previous_hash = full_record.get("current_hash")
        sequence = int(full_record.get("sequence", 0)) + 1
        failure_record: dict[str, Any] = {
            "record_type": PROJECTION_FAILURE_RECORD_TYPE,
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "timestamp": utc_now(),
            "previous_hash": previous_hash,
            "session_id": full_record.get("session_id"),
            "plan_sha256": full_record.get("plan_sha256"),
            "transport_classification": "RESPONSE_STORAGE_ERROR",
            "response_body_bytes": 0,
            "body_measured": False,
            "body_complete": False,
            "plan_entry_id": full_record.get("plan_entry_id"),
        }
        failure_record["current_hash"] = record_hash(failure_record)
        try:
            self._append_jsonl_line(failure_record)
        except ProvenanceError:
            pass

    @_locked_session
    def finalize(self) -> dict[str, Any]:
        plan = self._plan_from_disk()
        records = self.verify_session()
        if self._terminal_from_records(records):
            raise SessionFinalizedError("session is already finalized")
        self._append_record(
            {
                "record_type": TERMINAL_RECORD_TYPE,
                "timestamp": utc_now(),
            },
            plan=plan,
        )
        return self._read_summary()

    def _headers_dict(self, response: Any) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        get_all = getattr(response, "get_all", None)
        getheaders = getattr(response, "getheaders", None)
        if callable(get_all) and callable(getheaders):
            names = {
                str(name).lower()
                for name, _value in getheaders()
            }
            for name in names:
                values[name] = [str(value) for value in get_all(name)]
            return values
        if callable(getheaders):
            for name, value in getheaders():
                key = str(name).lower()
                values.setdefault(key, []).append(str(value))
        return values

    def _header_values(self, response: Any, name: str) -> list[str]:
        headers = self._headers_dict(response)
        return list(headers.get(name.lower(), []))

    def _transfer_codings(self, response: Any) -> list[str] | None:
        values = self._header_values(response, "transfer-encoding")
        if not values:
            return None
        codings: list[str] = []
        for value in values:
            for part in value.split(","):
                token = part.strip().lower()
                if not token or any(ch.isspace() for ch in token):
                    return []
                codings.append(token)
        return codings

    def _parse_content_length(self, response: Any) -> int | None:
        headers = self._headers_dict(response)
        values = headers.get("content-length", [])
        if not values:
            return None
        parsed: list[int] = []
        for value in values:
            if not re.fullmatch(r"[0-9]+", value.strip()):
                raise SessionInvalidError(f"invalid Content-Length value: {value!r}")
            parsed.append(int(value, 10))
        if len(set(parsed)) != 1:
            raise SessionInvalidError("conflicting Content-Length values")
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
        filename = f"{sequence:04d}.bin"
        target = self._safe_retained_target(filename)
        self._write_exclusive_bytes(
            target,
            body,
            "retained body",
        )
        return filename

    def _read_exact(self, response: Any, amount: int) -> bytes:
        chunks: list[bytes] = []
        remaining = amount
        while remaining > 0:
            chunk = response.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_streaming(self, response: Any, remaining_bytes: int) -> tuple[bytes, bool]:
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

    def _read_exact_observed(
        self,
        response: Any,
        amount: int,
    ) -> tuple[bytes, bool, str | None]:
        chunks: list[bytes] = []
        remaining = amount
        while remaining > 0:
            try:
                chunk = response.read(min(CHUNK_SIZE, remaining))
            except (socket.timeout, TimeoutError):
                return b"".join(chunks), False, "RESPONSE_READ_TIMEOUT"
            except Exception:
                return b"".join(chunks), False, "RESPONSE_READ_ERROR"
            if not isinstance(chunk, (bytes, bytearray)):
                return b"".join(chunks), False, "RESPONSE_READ_ERROR"
            if len(chunk) > remaining:
                return b"".join(chunks), False, "RESPONSE_READ_ERROR"
            if not chunk:
                break
            chunks.append(bytes(chunk))
            remaining -= len(chunk)
        return b"".join(chunks), remaining == 0, None

    def _read_streaming_observed(
        self,
        response: Any,
        remaining_bytes: int,
    ) -> tuple[bytes, bool, str | None]:
        chunks: list[bytes] = []
        remaining = remaining_bytes
        while True:
            if remaining <= 0:
                return b"".join(chunks), False, None
            try:
                chunk = response.read(min(CHUNK_SIZE, remaining))
            except (socket.timeout, TimeoutError):
                return b"".join(chunks), False, "RESPONSE_READ_TIMEOUT"
            except Exception:
                return b"".join(chunks), False, "RESPONSE_READ_ERROR"
            if not isinstance(chunk, (bytes, bytearray)):
                return b"".join(chunks), False, "RESPONSE_READ_ERROR"
            if len(chunk) > remaining:
                return b"".join(chunks), False, "RESPONSE_READ_ERROR"
            if not chunk:
                return b"".join(chunks), True, None
            chunks.append(bytes(chunk))
            remaining -= len(chunk)

    def _record_authorizes_redirect_target(
        self,
        record: Mapping[str, Any],
        source: PlanRequest,
        target: PlanRequest,
    ) -> bool:
        status = record.get("status")
        return (
            record.get("plan_entry_id") == source.id
            and isinstance(status, int)
            and 300 <= status < 400
            and status != 304
            and record.get("redirect_authorized") is True
            and record.get("redirect_exact_match") is True
            and record.get("redirect_followed") is False
            and record.get("redirect_target_id") == target.id
            and self._is_blocking_classification(record) is None
        )

    def _authorize_next_entry(
        self,
        plan: SessionPlan,
        records: Sequence[Mapping[str, Any]],
        requested_id: str | None = None,
    ) -> PlanRequest:
        http_records = self._http_records(records)
        next_index = len(http_records)
        if next_index >= len(plan.requests):
            raise RequestPolicyError("no pending plan entries remain")
        next_request = plan.requests[next_index]
        if requested_id is not None and requested_id != next_request.id:
            raise RequestPolicyError(
                f"out-of-order request {requested_id!r}; "
                f"expected next entry {next_request.id!r}"
            )
        if next_request.redirect_from_id is not None:
            if next_index == 0:
                raise RequestPolicyError(
                    f"redirect target {next_request.id!r} has no preceding source record"
                )
            source = plan.requests[next_index - 1]
            if source.id != next_request.redirect_from_id:
                raise RequestPolicyError(
                    f"redirect target {next_request.id!r} is not linked to "
                    f"the immediate planned predecessor"
                )
            if not self._record_authorizes_redirect_target(
                http_records[-1],
                source,
                next_request,
            ):
                raise RequestPolicyError(
                    f"redirect target {next_request.id!r} is not authorized by "
                    f"the immediately preceding source record"
                )
        return next_request

    @_locked_session
    def execute(
        self,
        transport: Callable[[str, str, dict[str, str]], Any] | None = None,
    ) -> list[dict[str, Any]]:
        plan = self._plan_from_disk()
        transport = transport or _HTTPTransport()
        records = self.verify_session()
        self._ensure_writable()
        while True:
            records = self.verify_session()
            self._ensure_writable()
            completed = self._http_records(records)
            if len(completed) >= len(plan.requests):
                return self._http_records(records)
            if len(completed) >= plan.max_requests:
                raise BudgetBlockedError("request budget is exhausted")
            request = self._authorize_next_entry(plan, records)
            aggregate = sum(
                int(record.get("response_body_bytes", 0))
                for record in completed
            )
            if aggregate >= plan.max_bytes:
                raise BudgetBlockedError("response-byte budget is exhausted")
            remaining_bytes = plan.max_bytes - aggregate
            self._validate_plan_request(
                request,
                plan,
                remaining_bytes=remaining_bytes,
            )
            self._append_reservation(request, plan)
            records = self.verify_session(
                allow_pending_retained=True,
                allow_pending_reservation=True,
            )
            record = self._perform_one(
                request,
                plan,
                remaining_bytes=remaining_bytes,
                transport=transport,
                records=records,
            )
            blocked_reason = self._is_blocking_classification(record)
            self._append_record(
                record,
                plan=plan,
            )
            records = self.verify_session()
            if blocked_reason is not None:
                raise BudgetBlockedError(f"session blocked: {blocked_reason}")
        return self._http_records(self.load_records())

    @_locked_session
    def request_one(
        self,
        entry_id: str,
        transport: Callable[[str, str, dict[str, str]], Any] | None = None,
    ) -> dict[str, Any]:
        plan = self._plan_from_disk()
        transport = transport or _HTTPTransport()
        records = self.verify_session()
        self._ensure_writable()
        request = self._authorize_next_entry(plan, records, entry_id)
        completed = self._http_records(records)
        aggregate = sum(
            int(record.get("response_body_bytes", 0))
            for record in completed
        )
        if len(completed) >= plan.max_requests:
            raise BudgetBlockedError("request budget is exhausted")
        if aggregate >= plan.max_bytes:
            raise BudgetBlockedError("response-byte budget is exhausted")
        remaining_bytes = plan.max_bytes - aggregate
        self._validate_plan_request(
            request,
            plan,
            remaining_bytes=remaining_bytes,
        )
        self._append_reservation(request, plan)
        records = self.verify_session(
            allow_pending_retained=True,
            allow_pending_reservation=True,
        )
        record = self._perform_one(
            request,
            plan,
            remaining_bytes=remaining_bytes,
            transport=transport,
            records=records,
        )
        blocked_reason = self._is_blocking_classification(record)
        self._append_record(
            record,
            plan=plan,
        )
        if blocked_reason is not None:
            raise BudgetBlockedError(f"session blocked: {blocked_reason}")
        return record

    def _target_attempt_context(
        self,
        request: PlanRequest,
        plan: SessionPlan,
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if request.redirect_from_id is None or not records:
            return {
                "redirect_target_id": None,
                "redirect_exact_match": None,
                "redirect_authorized": False,
                "redirect_followed": False,
                "redirect_source_entry_id": None,
                "redirect_source_record_hash": None,
            }
        http_records = self._http_records(records)
        source = plan.requests[len(http_records) - 1]
        source_record = http_records[-1]
        return {
            "redirect_target_id": request.id,
            "redirect_exact_match": True,
            "redirect_authorized": True,
            "redirect_followed": True,
            "redirect_source_entry_id": source.id,
            "redirect_source_record_hash": source_record["current_hash"],
        }

    def _append_reservation(
        self,
        request: PlanRequest,
        plan: SessionPlan,
    ) -> dict[str, Any]:
        return self._append_record(
            {
                "record_type": RESERVATION_RECORD_TYPE,
                "timestamp": utc_now(),
                "plan_entry_id": request.id,
            },
            plan=plan,
        )

    def _perform_one(
        self,
        request: PlanRequest,
        plan: SessionPlan,
        *,
        remaining_bytes: int,
        transport: Callable[[str, str, dict[str, str]], Any],
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        sequence = len(records) + 1
        host = _normalise_host(request.url)
        target_context = self._target_attempt_context(request, plan, records)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
        }
        try:
            response = transport(request.method, request.url, headers)
        except (socket.timeout, TimeoutError):
            return self._transport_record(
                request,
                plan,
                host,
                sequence,
                remaining_bytes,
                "TIMEOUT",
                detail="timeout",
                target_context=target_context,
            )
        except Exception:
            return self._transport_record(
                request,
                plan,
                host,
                sequence,
                remaining_bytes,
                "TRANSPORT_ERROR",
                detail="transport_error",
                target_context=target_context,
            )
        try:
            return self._response_record(
                request,
                plan,
                response,
                host,
                sequence,
                remaining_bytes,
                records,
                target_context=target_context,
            )
        except (socket.timeout, TimeoutError):
            return self._transport_record(
                request,
                plan,
                host,
                sequence,
                remaining_bytes,
                "RESPONSE_READ_TIMEOUT",
                detail="response_processing_timeout",
                target_context=target_context,
            )
        except Exception:
            return self._transport_record(
                request,
                plan,
                host,
                sequence,
                remaining_bytes,
                "RESPONSE_READ_ERROR",
                detail="response_processing_error",
                target_context=target_context,
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
        target_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = dict(target_context or {})
        return {
            "record_type": HTTP_RECORD_TYPE,
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
            "redirect_resolved_url": None,
            "redirect_target_id": context.get("redirect_target_id"),
            "redirect_exact_match": context.get("redirect_exact_match"),
            "redirect_authorized": context.get("redirect_authorized", False),
            "redirect_followed": context.get("redirect_followed", False),
            "redirect_source_entry_id": context.get("redirect_source_entry_id"),
            "redirect_source_record_hash": context.get("redirect_source_record_hash"),
            "redirect_refusal_reason": None,
            "final_host": host,
            "retained_filename": None,
            "remaining_request_budget": plan.max_requests - sequence,
            "remaining_byte_budget": remaining_bytes,
            "body_measured": False,
            "body_complete": False,
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
        records: Sequence[Mapping[str, Any]],
        target_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        status = int(response.status)
        try:
            content_length = self._parse_content_length(response)
        except SessionInvalidError:
            return self._invalid_content_length_record(
                request=request,
                plan=plan,
                host=host,
                sequence=sequence,
                status=status,
                target_context=target_context,
            )
        transfer_codings = self._transfer_codings(response)
        if transfer_codings is not None:
            if content_length is not None:
                return self._invalid_content_length_record(
                    request=request,
                    plan=plan,
                    host=host,
                    sequence=sequence,
                    status=status,
                    target_context=target_context,
                )
            if transfer_codings != ["chunked"]:
                return self._invalid_content_length_record(
                    request=request,
                    plan=plan,
                    host=host,
                    sequence=sequence,
                    status=status,
                    target_context=target_context,
                )
        location_values = self._header_values(response, "location")
        redirect_location = location_values[0] if len(location_values) == 1 else None
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
                target_context=target_context,
            )

        body_complete = False
        read_classification: str | None = None
        if content_length is not None:
            body, body_complete, read_classification = self._read_exact_observed(
                response,
                content_length,
            )
            actual = len(body)
            if read_classification is not None:
                classification = read_classification
                blocked = True
            else:
                classification = None
                blocked = not body_complete
                if blocked:
                    classification = "CONTENT_LENGTH_INVALID"
        else:
            body, body_complete, read_classification = self._read_streaming_observed(
                response,
                remaining_bytes,
            )
            actual = len(body)
            if read_classification is not None:
                classification = read_classification
                blocked = True
            else:
                classification = None
                blocked = not body_complete
                if blocked:
                    classification = "BYTE_BUDGET_EXCEEDED"

        try:
            retained_sequence = len(self._http_records(records)) + 1
            retained = self._save_body(
                body,
                request=request,
                sequence=retained_sequence,
            )
        except ProvenanceError:
            retained = None
            classification = "RESPONSE_STORAGE_ERROR"
            blocked = True
        body_hash = sha256_bytes(body)

        context = dict(target_context or {})
        refusal_reason: str | None = None
        redirect_target_id = context.get("redirect_target_id")
        redirect_resolved_url: str | None = None
        redirect_exact_match = context.get("redirect_exact_match")
        redirect_authorized = context.get("redirect_authorized", False)
        redirect_followed = context.get("redirect_followed", False)
        redirect_source_entry_id = context.get("redirect_source_entry_id")
        redirect_source_record_hash = context.get("redirect_source_record_hash")
        redirect_refusal_reason: str | None = None

        if (
            classification is None
            and request.expected_statuses
            and status not in request.expected_statuses
        ):
            classification = "UNEXPECTED_STATUS"
            blocked = True
            refusal_reason = "status_not_in_expected_statuses"
        elif classification is None and request.redirect_from_id is not None:
            http_records = self._http_records(records)
            source_record = http_records[-1]
            redirect_target_id = request.id
            redirect_authorized = True
            redirect_exact_match = True
            redirect_followed = True
            redirect_source_entry_id = request.redirect_from_id
            redirect_source_record_hash = source_record["current_hash"]
        elif classification is None and 300 <= status < 400 and status != 304:
            redirect_target_id = request.redirect_target_id
            if len(location_values) != 1:
                classification = "UNEXPECTED_REDIRECT"
                blocked = True
                redirect_refusal_reason = (
                    "missing_location"
                    if not location_values
                    else "conflicting_location_values"
                )
            else:
                raw_location = location_values[0]
                redirect_location = raw_location
                try:
                    resolved = urllib.parse.urljoin(request.url, raw_location)
                except ValueError:
                    resolved = raw_location
                redirect_resolved_url = resolved
                if redirect_target_id is None:
                    classification = "UNEXPECTED_REDIRECT"
                    blocked = True
                    try:
                        _validate_public_url(
                            resolved,
                            allowed_hosts=plan.allowed_hosts,
                            allow_query=False,
                        )
                    except RequestPolicyError as error:
                        redirect_refusal_reason = str(error)
                    else:
                        redirect_refusal_reason = "redirect_target_not_planned"
                else:
                    target = plan.get(redirect_target_id)
                    try:
                        _validate_public_url(
                            resolved,
                            allowed_hosts=plan.allowed_hosts,
                            allow_query=target.allow_query,
                        )
                    except RequestPolicyError as error:
                        classification = "UNEXPECTED_REDIRECT"
                        blocked = True
                        redirect_refusal_reason = str(error)
                    else:
                        exact_match = urls_match_exactly(resolved, target.url)
                        redirect_exact_match = exact_match
                        consumed_entries = {
                            record["plan_entry_id"]
                            for record in records
                            if "plan_entry_id" in record
                        }
                        if target.id in consumed_entries:
                            classification = "UNEXPECTED_REDIRECT"
                            blocked = True
                            redirect_refusal_reason = "redirect_target_already_consumed"
                        elif exact_match:
                            redirect_authorized = True
                        else:
                            classification = "UNEXPECTED_REDIRECT"
                            blocked = True
                            redirect_refusal_reason = "redirect_target_mismatch"

        attempt_number = len(self._http_records(records)) + 1
        record = {
            "record_type": HTTP_RECORD_TYPE,
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
            "redirect_resolved_url": redirect_resolved_url,
            "redirect_target_id": redirect_target_id,
            "redirect_exact_match": redirect_exact_match,
            "redirect_authorized": redirect_authorized,
            "redirect_followed": redirect_followed,
            "redirect_source_entry_id": redirect_source_entry_id,
            "redirect_source_record_hash": redirect_source_record_hash,
            "redirect_refusal_reason": redirect_refusal_reason,
            "final_host": final_host,
            "retained_filename": retained,
            "remaining_request_budget": plan.max_requests - attempt_number,
            "remaining_byte_budget": remaining_bytes - actual,
            "body_measured": True,
            "body_complete": body_complete,
            "refusal_reason": refusal_reason,
            "plan_entry_id": request.id,
            "detail": "truncated_body" if not body_complete and not blocked else None,
        }
        return record

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
        target_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = dict(target_context or {})
        return {
            "record_type": HTTP_RECORD_TYPE,
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
            "redirect_resolved_url": None,
            "redirect_target_id": context.get("redirect_target_id"),
            "redirect_exact_match": context.get("redirect_exact_match"),
            "redirect_authorized": context.get("redirect_authorized", False),
            "redirect_followed": context.get("redirect_followed", False),
            "redirect_source_entry_id": context.get("redirect_source_entry_id"),
            "redirect_source_record_hash": context.get("redirect_source_record_hash"),
            "redirect_refusal_reason": None,
            "final_host": final_host,
            "retained_filename": None,
            "remaining_request_budget": plan.max_requests - sequence,
            "remaining_byte_budget": remaining_bytes,
            "body_measured": False,
            "body_complete": False,
            "refusal_reason": reason,
            "plan_entry_id": request.id,
            "detail": "body_not_read",
        }


    def _invalid_content_length_record(
        self,
        *,
        request: PlanRequest,
        plan: SessionPlan,
        host: str,
        sequence: int,
        status: int,
        target_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = dict(target_context or {})
        return {
            "record_type": HTTP_RECORD_TYPE,
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "timestamp": utc_now(),
            "method": request.method,
            "url": request.url,
            "host": host,
            "status": status,
            "transport_classification": "CONTENT_LENGTH_INVALID",
            "response_body_bytes": 0,
            "response_body_sha256": None,
            "no_body_identity": "body_not_read",
            "content_length": None,
            "redirect_location": None,
            "redirect_resolved_url": None,
            "redirect_target_id": context.get("redirect_target_id"),
            "redirect_exact_match": context.get("redirect_exact_match"),
            "redirect_authorized": context.get("redirect_authorized", False),
            "redirect_followed": context.get("redirect_followed", False),
            "redirect_source_entry_id": context.get("redirect_source_entry_id"),
            "redirect_source_record_hash": context.get("redirect_source_record_hash"),
            "redirect_refusal_reason": None,
            "final_host": host,
            "retained_filename": None,
            "remaining_request_budget": plan.max_requests - sequence,
            "remaining_byte_budget": None,
            "body_measured": False,
            "body_complete": False,
            "refusal_reason": "invalid_content_length",
            "plan_entry_id": request.id,
            "detail": "body_not_read",
        }


class _UsageParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        self.exit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _UsageParser(
        prog="provenance_capture",
        description="Append-only bounded offline provenance logger.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate-plan",
        help="validate an offline session plan",
    )
    validate_parser.add_argument("--plan", required=True)
    validate_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    init_parser = subparsers.add_parser("init", help="initialize a new session")
    init_parser.add_argument("--session-dir", default=DEFAULT_SESSION_DIR)
    init_parser.add_argument("--plan", required=True)
    init_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    request_parser = subparsers.add_parser(
        "request",
        help="execute exactly one planned request/hop",
    )
    request_parser.add_argument("--session-dir", default=DEFAULT_SESSION_DIR)
    request_parser.add_argument("--entry-id", required=True)
    request_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify an existing session",
    )
    verify_parser.add_argument("--session-dir", default=DEFAULT_SESSION_DIR)
    verify_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    finalize_parser = subparsers.add_parser(
        "finalize",
        help="finalize an existing session",
    )
    finalize_parser.add_argument("--session-dir", default=DEFAULT_SESSION_DIR)
    finalize_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return parser


def _structured_output(
    command: str,
    success: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "success": success,
        "detail": detail,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        if args.command == "validate-plan":
            plan = SessionPlan.from_dict(
                _load_json_file_bounded(
                    Path(args.plan),
                    "session plan",
                    MAX_PLAN_BYTES,
                )
            )
            print(json.dumps(
                _structured_output("validate-plan", True, plan.plan_hash),
                indent=2,
                sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "init":
            plan = SessionPlan.from_dict(
                _load_json_file_bounded(
                    Path(args.plan),
                    "session plan",
                    MAX_PLAN_BYTES,
                )
            )
            session = ProvenanceSession(args.session_dir, plan=plan)
            session.initialize()
            print(json.dumps(
                _structured_output("init", True, plan.plan_hash),
                indent=2,
                sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "request":
            session = ProvenanceSession(args.session_dir)
            record = session.request_one(args.entry_id)
            print(json.dumps(
                {
                    **_structured_output(
                        "request",
                        True,
                        f"record={record['sequence']}",
                    ),
                    "record": record,
                },
                indent=2,
                sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "verify":
            session = ProvenanceSession(args.session_dir)
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
        if args.command == "finalize":
            session = ProvenanceSession(args.session_dir)
            summary = session.finalize()
            print(json.dumps(
                _structured_output(
                    "finalize",
                    True,
                    summary.get("final_hash") or "empty",
                ),
                indent=2,
                sort_keys=True,
            ))
            return EXIT_OK
        raise ProvenanceError(f"unknown command {args.command!r}")
    except ProvenanceError as error:
        print(json.dumps(
            {
                **_structured_output(args.command, False, str(error)),
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
                **_structured_output(
                    args.command,
                    False,
                    "unexpected internal error",
                ),
                "classification": "INTERNAL_ERROR",
                "exit_code": EXIT_INTERNAL,
            },
            indent=2,
            sort_keys=True,
        ))
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
