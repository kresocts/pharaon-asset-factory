#!/usr/bin/env python3
"""Canonical pre-weights runtime readiness gate for the Hunyuan container.

The gate reuses the existing health and native-smoke diagnostics but exposes
one stable, scriptable contract:

    ready --profile cpu --json
    ready --profile native-gpu --json

Exit codes:
    0   requested profile is ready
    2   expected readiness requirements are not met
    3   diagnostic/internal execution error
    64  invalid command-line usage

No check downloads model assets, accesses a model hub, or otherwise requires
network access.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


SCHEMA_VERSION = 1
EXIT_READY = 0
EXIT_NOT_READY = 2
EXIT_DIAGNOSTIC_ERROR = 3
EXIT_INVALID_REQUEST = 64

PROFILES = ("cpu", "native-gpu")
EXPECTED_PYTHON_PREFIX = "3.10"
EXPECTED_TORCH = "2.5.1"
EXPECTED_TORCH_CUDA = "12.4"
EXPECTED_HUNYUAN_COMMIT = "82920d643c0dc2f7bfd7255f45f62d386edfe60c"
OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "DIFFUSERS_OFFLINE": "1",
}


class ReadinessDiagnosticError(Exception):
    """Raised when diagnostic logic itself fails, not when a check is not-ready."""


class ReadinessUsageError(Exception):
    """Raised for invalid command-line usage."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ReadinessUsageError(message)


def _health_module() -> Any:
    """Return the shared health module lazily so host tests can inject a fake."""
    return importlib.import_module("health")


def _native_smoke_module() -> Any:
    """Return the shared native-smoke module lazily so host tests can inject a fake."""
    return importlib.import_module("native_smoke")


def _check(check_id: str, status: str, message: str, **facts: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"id": check_id, "status": status, "message": message}
    result.update(facts)
    return result


def _pass(check_id: str, message: str, **facts: Any) -> dict[str, Any]:
    return _check(check_id, "PASS", message, **facts)


def _fail(check_id: str, message: str, **facts: Any) -> dict[str, Any]:
    return _check(check_id, "FAIL", message, **facts)


def _normalized_version(value: object) -> str:
    return str(value).split("+")[0]


def _python_version_check() -> dict[str, Any]:
    actual = platform.python_version()
    expected = EXPECTED_PYTHON_PREFIX
    if actual.startswith(expected):
        return _pass("python.version", f"Python {actual} matches expected {expected}.*", expected=expected, actual=actual)
    return _fail("python.version", f"Python {actual} does not match expected {expected}.*", expected=expected, actual=actual)


def _runtime_config_check(paths: dict[str, str]) -> dict[str, Any]:
    missing = [key for key, value in paths.items() if not value]
    if missing:
        return _fail(
            "runtime.config",
            f"required runtime path configuration is missing or empty: {', '.join(sorted(missing))}",
            missing=sorted(missing),
            paths=paths,
        )
    return _pass("runtime.config", "required runtime path configuration is present", paths=paths)


def _torch_checks(pytorch: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    status = pytorch.get("status")
    detail = pytorch.get("detail")
    if status in ("PYTORCH_NOT_INSTALLED", "PYTORCH_IMPORT_ERROR"):
        checks.append(
            _fail(
                "torch.import",
                f"PyTorch is not importable: {status}" + (f": {detail}" if detail else ""),
                torch_status=status,
            )
        )
    else:
        checks.append(_pass("torch.import", "PyTorch imported successfully", torch_status=status))

    versions_match = bool(pytorch.get("versions_match"))
    cuda_build = pytorch.get("cuda_build_version")
    if versions_match and cuda_build == EXPECTED_TORCH_CUDA:
        checks.append(
            _pass(
                "torch.version",
                f"pinned PyTorch contract satisfied (torch {EXPECTED_TORCH}, CUDA {EXPECTED_TORCH_CUDA})",
                versions=pytorch.get("versions"),
                cuda_build=cuda_build,
            )
        )
    else:
        checks.append(
            _fail(
                "torch.version",
                (
                    f"pinned PyTorch contract not satisfied: versions_match={versions_match}, "
                    f"torch_version={pytorch.get('versions', {}).get('torch')}, "
                    f"cuda_build={cuda_build}"
                ),
                versions=pytorch.get("versions"),
                cuda_build=cuda_build,
                versions_match=versions_match,
            )
        )
    return checks


def _hunyuan_checks(hunyuan: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if hunyuan.get("source_present"):
        checks.append(_pass("hunyuan.source", "Hunyuan source directory is present", source_path=hunyuan.get("source_path")))
    else:
        checks.append(
            _fail(
                "hunyuan.source",
                f"Hunyuan source directory is missing: {hunyuan.get('source_path')}",
                source_path=hunyuan.get("source_path"),
            )
        )

    if hunyuan.get("revision_matches"):
        checks.append(
            _pass(
                "hunyuan.revision",
                f"Hunyuan revision matches {EXPECTED_HUNYUAN_COMMIT}",
                expected_revision=EXPECTED_HUNYUAN_COMMIT,
                revision=hunyuan.get("revision"),
            )
        )
    else:
        checks.append(
            _fail(
                "hunyuan.revision",
                (
                    f"Hunyuan revision does not match expected {EXPECTED_HUNYUAN_COMMIT}; "
                    f"actual={hunyuan.get('revision')}"
                ),
                expected_revision=EXPECTED_HUNYUAN_COMMIT,
                revision=hunyuan.get("revision"),
            )
        )
    return checks


def _dependency_import_check(dependencies: dict[str, Any]) -> dict[str, Any]:
    status = dependencies.get("status")
    if status == "DEPENDENCY_IMPORTS_READY":
        return _pass("dependencies.imports", "representative Hunyuan dependencies import successfully")
    failed = [
        name
        for name, item in dependencies.get("imports", {}).items()
        if item.get("status") != "AVAILABLE"
    ]
    return _fail(
        "dependencies.imports",
        f"one or more representative dependencies failed to import: {', '.join(sorted(failed))}",
        failed=failed,
    )


def _native_artifact_check(hunyuan: dict[str, Any]) -> dict[str, Any]:
    rasterizer = hunyuan.get("custom_rasterizer", {}).get("status")
    renderer = hunyuan.get("differentiable_renderer", {}).get("status")
    if rasterizer == "CUSTOM_RASTERIZER_BUILT" and renderer == "DIFFERENTIABLE_RENDERER_BUILT":
        return _pass("native.artifacts", "compiled native extension artifacts are present")
    return _fail(
        "native.artifacts",
        (
            f"compiled native extension artifacts are incomplete: "
            f"custom_rasterizer={rasterizer}, differentiable_renderer={renderer}"
        ),
        custom_rasterizer=rasterizer,
        differentiable_renderer=renderer,
    )


def _native_checks(native: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    rasterizer = native.get("custom_rasterizer", {})
    if rasterizer.get("status") == "IMPORT_OK":
        checks.append(_pass("native.custom_rasterizer.import", "custom_rasterizer imports successfully", module_path=rasterizer.get("module_path")))
    else:
        checks.append(
            _fail(
                "native.custom_rasterizer.import",
                f"custom_rasterizer failed to import: {rasterizer.get('status')}",
                detail=rasterizer.get("detail"),
            )
        )

    renderer = native.get("differentiable_renderer", {})
    if renderer.get("status") == "IMPORT_OK":
        checks.append(_pass("native.renderer.import", "DifferentiableRenderer imports successfully", module_path=renderer.get("module_path")))
    else:
        checks.append(
            _fail(
                "native.renderer.import",
                f"DifferentiableRenderer failed to import: {renderer.get('status')}",
                detail=renderer.get("detail"),
            )
        )

    renderer_operation = native.get("renderer_operation", {})
    if renderer_operation.get("status") == "RENDERER_NATIVE_OPERATION_OK":
        checks.append(
            _pass(
                "native.renderer.operation",
                "renderer native smoke operation succeeded",
                color_shape=renderer_operation.get("color_shape"),
            )
        )
    else:
        checks.append(
            _fail(
                "native.renderer.operation",
                f"renderer native smoke operation failed: {renderer_operation.get('status')}",
                detail=renderer_operation.get("detail"),
            )
        )

    return checks


def _path_exists_check(check_id: str, path: Path) -> dict[str, Any]:
    if path.is_dir():
        return _pass(check_id, f"path exists: {path}", path=str(path))
    return _fail(check_id, f"path is missing: {path}", path=str(path))


def _probe_writable(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"path does not exist: {path}"
    if not path.is_dir():
        return False, f"path is not a directory: {path}"
    try:
        descriptor, probe = tempfile.mkstemp(prefix=".readiness-probe-", dir=str(path))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write("ok")
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass
        return True, ""
    except OSError as error:
        return False, str(error)


def _path_writable_check(check_id: str, path: Path) -> dict[str, Any]:
    writable, detail = _probe_writable(path)
    if writable:
        return _pass(check_id, f"path is writable: {path}", path=str(path))
    return _fail(check_id, f"path is not writable: {path}" + (f": {detail}" if detail else ""), path=str(path), detail=detail)


def _path_checks(paths: dict[str, str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    model = Path(paths["model_cache"])
    checks.append(_path_exists_check("paths.models.exists", model))
    checks.append(_path_writable_check("paths.models.writable", model))
    for key, check_id in (
        ("input", "paths.input.writable"),
        ("output", "paths.output.writable"),
        ("workspace", "paths.workspace.writable"),
    ):
        checks.append(_path_writable_check(check_id, Path(paths[key])))
    checks.append(_path_exists_check("paths.hunyuan_source.exists", Path(paths["hunyuan_source"])))
    return checks


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _model_cache_external_check(paths: dict[str, str]) -> dict[str, Any]:
    model = Path(paths["model_cache"])
    app = Path("/app")
    hunyuan = Path(paths["hunyuan_source"])
    if _is_within(model, app) or _is_within(model, hunyuan):
        return _fail(
            "model.cache.external",
            "model cache must remain outside application and Hunyuan source paths",
            model_cache=str(model),
            application_source=str(app),
            hunyuan_source=str(hunyuan),
        )
    return _pass("model.cache.external", "model cache is external to application source", model_cache=str(model))


def _weights_state(model: dict[str, Any]) -> str:
    if model.get("status") == "MODEL_WEIGHTS_NOT_PRESENT_EXPECTED":
        return "ABSENT"
    return "PRESENT_UNVERIFIED"


def _weights_check(model: dict[str, Any]) -> dict[str, Any]:
    state = _weights_state(model)
    if state == "ABSENT":
        message = "model weights are absent (expected before the model-acquisition stage)"
    else:
        message = "model weights are present but not verified by an authoritative manifest"
    return _pass("weights.present", message, state=state, detected_files=model.get("detected_files", []))


def _inference_check() -> dict[str, Any]:
    return _pass(
        "inference.full_ready",
        "full Hunyuan inference readiness is false because model weights and inference are intentionally absent",
        full_ready=False,
    )


def _gpu_facts(nvidia: dict[str, Any], pytorch: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "visible": bool(pytorch.get("cuda_available") or nvidia.get("status") == "GPU_AVAILABLE"),
        "nvidia_status": nvidia.get("status"),
        "devices": nvidia.get("devices", []),
        "torch_device_count": pytorch.get("device_count", 0),
        "torch_device_name": pytorch.get("device_name"),
    }
    devices = nvidia.get("devices") or []
    if devices:
        parts = [part.strip() for part in devices[0].split(", ")]
        if len(parts) >= 4:
            facts["device_index"] = parts[0]
            facts["device_name"] = parts[1]
            facts["driver_version"] = parts[2]
            facts["memory_total"] = parts[3]
    capability = None
    try:
        import torch  # type: ignore[import-not-found]

        if torch.cuda.is_available():
            major, minor = torch.cuda.get_device_capability(0)
            capability = f"{major}.{minor}"
    except Exception:
        capability = None
    if capability:
        facts["compute_capability"] = capability
    return facts


def _gpu_checks(nvidia: dict[str, Any], pytorch: dict[str, Any], native: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    visible = bool(pytorch.get("cuda_available") or nvidia.get("status") == "GPU_AVAILABLE")
    if visible:
        checks.append(_pass("gpu.visible", "an NVIDIA GPU is visible", devices=nvidia.get("devices", [])))
    else:
        checks.append(_fail("gpu.visible", "no NVIDIA GPU is visible; run with --gpus all"))

    if pytorch.get("cuda_available"):
        checks.append(_pass("torch.cuda.available", "torch.cuda.is_available() is true"))
    else:
        checks.append(_fail("torch.cuda.available", "torch.cuda.is_available() is false"))

    if pytorch.get("cuda_operation") == "PYTORCH_CUDA_OPERATION_OK":
        checks.append(
            _pass(
                "torch.cuda.operation",
                "PyTorch CUDA tensor operation succeeded and synchronized",
                operation_result=pytorch.get("cuda_operation_result"),
            )
        )
    else:
        checks.append(
            _fail(
                "torch.cuda.operation",
                f"PyTorch CUDA tensor operation failed: {pytorch.get('cuda_operation')}",
                detail=pytorch.get("detail"),
            )
        )

    operation = native.get("custom_rasterizer_operation", {})
    if operation.get("status") == "CUSTOM_RASTERIZER_CUDA_OPERATION_OK":
        checks.append(
            _pass(
                "native.custom_rasterizer.operation",
                "native custom rasterizer CUDA operation succeeded",
                covered_pixels=operation.get("covered_pixels"),
            )
        )
    else:
        checks.append(
            _fail(
                "native.custom_rasterizer.operation",
                f"native custom rasterizer CUDA operation failed: {operation.get('status')}",
                detail=operation.get("detail"),
            )
        )
    return checks


def _build_report(profile: str, checks: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    failed = [check for check in checks if check.get("status") == "FAIL"]
    ready = not failed
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "status": "READY" if ready else "NOT_READY",
        "ready": ready,
        "classification": "READY" if ready else "NOT_READY",
        "exit_code": EXIT_READY if ready else EXIT_NOT_READY,
        "checks": checks,
        "facts": facts,
        "failure_summary": [
            {"id": check["id"], "message": check["message"]} for check in failed
        ],
    }


def collect_readiness(profile: str, environment: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect the readiness report for *profile* without accessing the network."""
    if profile not in PROFILES:
        raise ReadinessUsageError(f"unknown profile {profile!r}; expected one of {', '.join(PROFILES)}")

    env = dict(os.environ if environment is None else environment)
    for key, value in OFFLINE_ENVIRONMENT.items():
        env.setdefault(key, value)
        os.environ[key] = value

    health = _health_module()
    native_smoke = _native_smoke_module()
    paths = health._configured_paths(env)
    pytorch = health._pytorch_diagnostics()
    dependencies = health._dependency_diagnostics()
    hunyuan = health._hunyuan_diagnostics(paths, env)
    models = health._model_diagnostics(paths)
    nvidia = health._nvidia_diagnostics()
    native = native_smoke.collect_native_smoke(run_gpu_operation=profile == "native-gpu")

    checks: list[dict[str, Any]] = []
    checks.append(_python_version_check())
    checks.append(_runtime_config_check(paths))
    checks.extend(_torch_checks(pytorch))
    checks.extend(_hunyuan_checks(hunyuan))
    checks.append(_dependency_import_check(dependencies))
    checks.append(_native_artifact_check(hunyuan))
    checks.extend(_native_checks(native))
    checks.extend(_path_checks(paths))
    checks.append(_model_cache_external_check(paths))
    checks.append(_weights_check(models))
    checks.append(_inference_check())
    if profile == "native-gpu":
        checks.extend(_gpu_checks(nvidia, pytorch, native))

    facts = {
        "python": {"version": platform.python_version(), "expected": EXPECTED_PYTHON_PREFIX + ".*", "executable": sys.executable},
        "paths": paths,
        "torch": {
            "version": pytorch.get("versions", {}).get("torch"),
            "cuda_build": pytorch.get("cuda_build_version"),
            "cuda_available": bool(pytorch.get("cuda_available")),
            "cuda_operation": pytorch.get("cuda_operation"),
        },
        "gpu": _gpu_facts(nvidia, pytorch),
        "hunyuan": {
            "revision": hunyuan.get("revision"),
            "expected_revision": EXPECTED_HUNYUAN_COMMIT,
            "source_path": hunyuan.get("source_path"),
        },
        "native": {
            "cuda_architectures": env.get("TORCH_CUDA_ARCH_LIST", os.environ.get("TORCH_CUDA_ARCH_LIST", "UNKNOWN")),
            "import_status": native.get("status"),
        },
        "weights": {
            "state": _weights_state(models),
            "detected_files": models.get("detected_files", []),
        },
        "inference": {"full_ready": False},
    }
    return _build_report(profile, checks, facts)


def _format_human(report: dict[str, Any]) -> str:
    lines = [
        f"SCHEMA_VERSION={report['schema_version']}",
        f"PROFILE={report['profile']}",
        f"STATUS={report['status']}",
        f"READY={'YES' if report['ready'] else 'NO'}",
        f"CLASSIFICATION={report['classification']}",
        f"EXIT_CODE={report['exit_code']}",
    ]
    lines.append("CHECKS:")
    for check in report["checks"]:
        lines.append(f"- {check['id']}: {check['status']} ({check['message']})")
    return "\n".join(lines)


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _usage_error_report(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "INVALID_REQUEST",
        "ready": False,
        "classification": "INVALID_REQUEST",
        "exit_code": EXIT_INVALID_REQUEST,
        "checks": [],
        "facts": {},
        "failure_summary": [],
        "detail": message,
    }


def _diagnostic_error_report(profile: str | None, detail: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "status": "DIAGNOSTIC_ERROR",
        "ready": False,
        "classification": "DIAGNOSTIC_ERROR",
        "exit_code": EXIT_DIAGNOSTIC_ERROR,
        "checks": [],
        "facts": {},
        "failure_summary": [],
        "detail": detail,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="cpu", help="readiness profile to validate")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    try:
        args = parser.parse_args(argv)
    except ReadinessUsageError as error:
        _print_json(_usage_error_report(str(error)))
        return EXIT_INVALID_REQUEST

    try:
        report = collect_readiness(args.profile)
    except ReadinessUsageError as error:
        _print_json(_usage_error_report(str(error)))
        return EXIT_INVALID_REQUEST
    except Exception as error:
        _print_json(_diagnostic_error_report(args.profile, f"{type(error).__name__}: {error}"))
        return EXIT_DIAGNOSTIC_ERROR

    _print_json(report) if args.json else print(_format_human(report))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
