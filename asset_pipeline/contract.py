"""Strict, standard-library-only shape-job document contract.

The request schema is intentionally tiny: ``schema_version``, ``job_id``,
``reference_image``, ``seed``, and ``remove_background``. No prompt text, texture
options, model identifiers, cloud settings, or opaque extension fields are accepted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict


SCHEMA_VERSION = 1
MAX_JOB_BYTES = 64 * 1024
MAX_SEED = 4294967295

_REQUIRED_FIELDS = frozenset(
    {"schema_version", "job_id", "reference_image", "seed", "remove_background"}
)
_JOB_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_REFERENCE_IMAGE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$")


class ContractError(Exception):
    """Base class for expected shape-job document failures."""

    exit_code = 2
    status = "INVALID"
    classification = "INVALID_JOB_DOCUMENT"


class JobFileUnavailableError(ContractError):
    classification = "JOB_FILE_UNAVAILABLE"


class JobDocumentTooLargeError(ContractError):
    classification = "JOB_DOCUMENT_TOO_LARGE"


class JobDocumentDecodeError(ContractError):
    classification = "INVALID_JOB_DOCUMENT"


class DuplicateKeyError(ContractError):
    classification = "DUPLICATE_JOB_KEY"


class InvalidJobFieldError(ContractError):
    classification = "INVALID_JOB_DOCUMENT"


class InputPathPolicyError(ContractError):
    """Lexical reference-image path policy failure."""

    classification = "INPUT_PATH_POLICY_REFUSAL"


def _reject_constant(value: str) -> Any:
    """Refuse non-JSON constants such as NaN, Infinity, and -Infinity."""

    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    """Build a JSON object while refusing duplicate object keys."""

    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate job object key: {key}")
        result[key] = value
    return result


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidJobFieldError(f"{field} must be a string")
    return value


def validate_job_id(value: Any) -> str:
    """Validate the deterministic, path-safe job identifier."""

    value = _require_str(value, "job_id")
    if len(value) > 64:
        raise InvalidJobFieldError("job_id must be 1-64 characters")
    if not _JOB_ID_RE.fullmatch(value):
        raise InvalidJobFieldError(
            "job_id must start and end with an ASCII letter or digit and may contain "
            "only lowercase ASCII letters, digits, '.', '_', and '-'"
        )
    return value


def validate_reference_image(value: Any) -> str:
    """Validate and return a canonical relative POSIX-style input path.

    The canonical separator is ``/``. Backslashes are rejected rather than being
    silently reinterpreted as safe separators, and no absolute, drive-qualified,
    traversal, empty, NUL-containing, or percent-encoded escaping path is accepted.
    """

    value = _require_str(value, "reference_image")
    if not value:
        raise InputPathPolicyError("reference_image must not be empty")
    if "\\" in value:
        raise InputPathPolicyError(
            "reference_image must use '/' separators; backslashes are rejected"
        )
    if "\x00" in value:
        raise InputPathPolicyError("reference_image must not contain NUL")
    if "%" in value:
        raise InputPathPolicyError(
            "reference_image must not contain percent-encoded path escapes"
        )
    if not _REFERENCE_IMAGE_RE.fullmatch(value):
        raise InputPathPolicyError(
            "reference_image must be a relative path using only ASCII letters, "
            "digits, '.', '_', '-', and '/'"
        )
    segments = value.split("/")
    if any(not segment for segment in segments):
        raise InputPathPolicyError(
            "reference_image must not contain empty path segments"
        )
    if any(segment in (".", "..") for segment in segments):
        raise InputPathPolicyError(
            "reference_image must not contain '.' or '..' path segments"
        )
    return value


def validate_schema_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidJobFieldError("schema_version must be an integer")
    if value != SCHEMA_VERSION:
        raise InvalidJobFieldError(f"schema_version must equal {SCHEMA_VERSION}")
    return value


def validate_seed(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidJobFieldError("seed must be an integer, not a boolean")
    if value < 0 or value > MAX_SEED:
        raise InvalidJobFieldError(f"seed must be between 0 and {MAX_SEED}")
    return value


def validate_remove_background(value: Any) -> bool:
    if not isinstance(value, bool):
        raise InvalidJobFieldError("remove_background must be a boolean")
    return value


def validate_job_document(document: Any) -> Dict[str, Any]:
    """Validate a parsed job document and return its normalized field values."""

    if not isinstance(document, dict):
        raise InvalidJobFieldError("job document must be a JSON object")

    missing = _REQUIRED_FIELDS - document.keys()
    if missing:
        names = ", ".join(sorted(missing))
        raise InvalidJobFieldError(f"missing required field(s): {names}")

    unknown = document.keys() - _REQUIRED_FIELDS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise InvalidJobFieldError(f"unknown field(s): {names}")

    return {
        "schema_version": validate_schema_version(document["schema_version"]),
        "job_id": validate_job_id(document["job_id"]),
        "reference_image": validate_reference_image(document["reference_image"]),
        "seed": validate_seed(document["seed"]),
        "remove_background": validate_remove_background(
            document["remove_background"]
        ),
    }


def read_job_document(job_path: str | Path) -> Dict[str, Any]:
    """Read and strictly parse one job document from disk.

    The function reads at most ``MAX_JOB_BYTES + 1`` bytes, requires UTF-8 JSON,
    rejects duplicate keys and non-JSON constants, and rejects any trailing
    non-whitespace data.
    """

    path = Path(job_path)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_JOB_BYTES + 1)
    except OSError as exc:
        raise JobFileUnavailableError(
            f"cannot read job file {str(path)!r}: {exc.strerror or exc}"
        ) from exc

    if len(data) > MAX_JOB_BYTES:
        raise JobDocumentTooLargeError(
            f"job file exceeds the {MAX_JOB_BYTES} byte limit"
        )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JobDocumentDecodeError("job file is not valid UTF-8") from exc

    try:
        document = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (DuplicateKeyError,) as exc:
        raise exc
    except RecursionError as exc:
        raise JobDocumentDecodeError(
            "job file exceeds the supported JSON nesting depth"
        ) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise JobDocumentDecodeError(f"job file is not valid JSON: {exc}") from exc

    return validate_job_document(document)
