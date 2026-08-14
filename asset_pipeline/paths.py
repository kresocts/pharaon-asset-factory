"""Offline path-policy and image-validation logic for shape jobs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


MAX_INPUT_IMAGE_BYTES = 32 * 1024 * 1024

_DEFAULT_INPUT_DIR = "/data/input"
_DEFAULT_OUTPUT_DIR = "/data/output"
_DEFAULT_WORKSPACE_DIR = "/workspace"

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


class RuntimeRootError(Exception):
    exit_code = 3
    status = "ERROR"
    classification = "RUNTIME_ROOT_INVALID"


class InputPolicyError(Exception):
    exit_code = 2
    status = "INVALID"
    classification = "INPUT_POLICY_REFUSAL"


class SafePathError(Exception):
    exit_code = 3
    status = "ERROR"
    classification = "SAFE_PATH_UNAVAILABLE"


@dataclass(frozen=True)
class RuntimeRoots:
    input_dir: Path
    output_dir: Path
    workspace_dir: Path


def load_runtime_roots(
    environ: Optional[Mapping[str, str]] = None,
) -> RuntimeRoots:
    """Load and validate the three runtime root directories.

    ``INPUT_DIR``, ``OUTPUT_DIR``, and ``WORKSPACE_DIR`` may override the
    repository defaults. Each root must be an absolute, existing directory.
    """

    if environ is None:
        environ = os.environ

    values = (
        ("INPUT_DIR", environ.get("INPUT_DIR", _DEFAULT_INPUT_DIR)),
        ("OUTPUT_DIR", environ.get("OUTPUT_DIR", _DEFAULT_OUTPUT_DIR)),
        ("WORKSPACE_DIR", environ.get("WORKSPACE_DIR", _DEFAULT_WORKSPACE_DIR)),
    )
    resolved: list[Path] = []
    for name, raw in values:
        if not isinstance(raw, str) or not raw:
            raise RuntimeRootError(f"{name} must be a non-empty absolute directory")
        path = Path(raw)
        if not path.is_absolute():
            raise RuntimeRootError(f"{name} must be absolute: {raw!r}")
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeRootError(
                f"{name} is not an existing directory: {raw!r}"
            ) from exc
        if not path.is_dir():
            raise RuntimeRootError(f"{name} is not a directory: {raw!r}")
        resolved.append(path)

    return RuntimeRoots(*resolved)


def _ensure_contained(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SafePathError(
            f"resolved path escapes its configured root: {str(path)!r}"
        ) from exc


def _reject_symlink_ancestors(root: Path, relative_parts: list[str]) -> None:
    current = root
    for part in relative_parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise InputPolicyError(
                f"input path ancestor is a symlink: {str(current)!r}"
            )
        resolved = current.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise InputPolicyError(
                f"input path ancestor escapes the input root: {str(current)!r}"
            ) from exc


def _image_signature_kind(path: Path) -> Optional[str]:
    try:
        with path.open("rb") as handle:
            data = handle.read(12)
    except OSError as exc:
        raise InputPolicyError(
            f"cannot read input image signature: {exc.strerror or exc}"
        ) from exc

    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _expected_image_kind(extension: str) -> str:
    if extension == ".png":
        return "png"
    if extension in {".jpg", ".jpeg"}:
        return "jpeg"
    if extension == ".webp":
        return "webp"
    raise InputPolicyError(f"unsupported input image extension: {extension}")


def resolve_input_image(document: Dict[str, Any], roots: RuntimeRoots) -> Path:
    """Validate the reference image and return its resolved absolute path."""

    reference = document["reference_image"]
    parts = reference.split("/")
    input_path = roots.input_dir.joinpath(*parts)

    _reject_symlink_ancestors(roots.input_dir, parts)

    if not input_path.exists():
        raise InputPolicyError(f"input image does not exist: {str(input_path)!r}")
    if input_path.is_symlink():
        raise InputPolicyError(f"input image is a symlink: {str(input_path)!r}")
    if not input_path.is_file():
        raise InputPolicyError(
            f"input image is not a regular file: {str(input_path)!r}"
        )

    resolved_input = input_path.resolve(strict=True)
    try:
        resolved_input.relative_to(roots.input_dir)
    except ValueError as exc:
        raise InputPolicyError(
            f"input image escapes the input root: {str(input_path)!r}"
        ) from exc

    try:
        size = resolved_input.stat().st_size
    except OSError as exc:
        raise InputPolicyError(
            f"cannot inspect input image size: {exc.strerror or exc}"
        ) from exc
    if size == 0:
        raise InputPolicyError("input image is empty")
    if size > MAX_INPUT_IMAGE_BYTES:
        raise InputPolicyError(
            f"input image exceeds the {MAX_INPUT_IMAGE_BYTES} byte limit"
        )

    extension = resolved_input.suffix.lower()
    expected_kind = _expected_image_kind(extension)
    actual_kind = _image_signature_kind(resolved_input)
    if actual_kind is None:
        raise InputPolicyError("input image has an unrecognized file signature")
    if actual_kind != expected_kind:
        raise InputPolicyError(
            f"input image extension {extension!r} does not match its "
            f"{actual_kind!r} signature"
        )

    return resolved_input


def _derive_target(root: Path, job_id: str, kind: str) -> Path:
    target = root / job_id
    if target.exists() or target.is_symlink():
        raise SafePathError(f"{kind} target already exists: {str(target)!r}")
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SafePathError(
            f"{kind} target escapes its root: {str(target)!r}"
        ) from exc
    return resolved


def build_plan(document: Dict[str, Any], roots: RuntimeRoots) -> Dict[str, Any]:
    """Build a deterministic, non-mutating execution plan."""

    input_image = resolve_input_image(document, roots)
    output_directory = _derive_target(
        roots.output_dir, document["job_id"], "output"
    )
    workspace_directory = _derive_target(
        roots.workspace_dir, document["job_id"], "workspace"
    )

    return {
        "schema_version": 1,
        "status": "VALID",
        "classification": "SHAPE_JOB_CONTRACT_READY",
        "exit_code": 0,
        "stage": "shape",
        "execution_supported": False,
        "job": {
            "schema_version": document["schema_version"],
            "job_id": document["job_id"],
            "reference_image": document["reference_image"],
            "seed": document["seed"],
            "remove_background": document["remove_background"],
        },
        "paths": {
            "input_image": os.fspath(input_image),
            "output_directory": os.fspath(output_directory),
            "workspace_directory": os.fspath(workspace_directory),
        },
        "requirements": {
            "inference_backend": "hunyuan3d-2.1-shape",
            "model_weights": "REQUIRED_BUT_NOT_CONFIGURED",
            "gpu": "REQUIRED_FOR_FUTURE_EXECUTION",
        },
    }
