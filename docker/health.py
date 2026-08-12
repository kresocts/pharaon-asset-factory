#!/usr/bin/env python3
"""Report Hunyuan dependency-layer health without downloading model assets."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


EXPECTED_PYTHON = "3.10"
EXPECTED_PACKAGES = {"torch": "2.5.1", "torchvision": "0.20.1", "torchaudio": "2.5.1"}
REPRESENTATIVE_IMPORTS = ("transformers", "diffusers", "accelerate", "trimesh", "numpy")
DEFAULT_HUNYUAN_COMMIT = "82920d643c0dc2f7bfd7255f45f62d386edfe60c"
DEFAULT_PATHS = {
    "model_cache": "/models",
    "input": "/data/input",
    "output": "/data/output",
    "workspace": "/workspace",
    "hunyuan_source": "/opt/hunyuan3d",
}
MODEL_EXTENSIONS = {".bin", ".ckpt", ".onnx", ".pt", ".pth", ".safetensors"}


def _command_output(command: Sequence[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    return result.returncode == 0, (result.stdout or result.stderr).strip()


def _configured_paths(environment: dict[str, str]) -> dict[str, str]:
    return {
        "model_cache": environment.get("MODEL_CACHE_DIR", DEFAULT_PATHS["model_cache"]),
        "input": environment.get("INPUT_DIR", DEFAULT_PATHS["input"]),
        "output": environment.get("OUTPUT_DIR", DEFAULT_PATHS["output"]),
        "workspace": environment.get("WORKSPACE_DIR", DEFAULT_PATHS["workspace"]),
        "hunyuan_source": environment.get("HUNYUAN_SOURCE_DIR", DEFAULT_PATHS["hunyuan_source"]),
    }


def _nvidia_diagnostics() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {"smi_available": False, "status": "GPU_NOT_AVAILABLE", "devices": []}
    ok, output = _command_output(
        (executable, "--query-gpu=index,name,driver_version,memory.total", "--format=csv,noheader")
    )
    if not ok:
        return {"smi_available": True, "status": "GPU_RUNTIME_ERROR", "devices": [], "detail": output}
    devices = [line.strip() for line in output.splitlines() if line.strip()]
    return {
        "smi_available": True,
        "status": "GPU_AVAILABLE" if devices else "GPU_NOT_AVAILABLE",
        "devices": devices,
    }


def _cuda_diagnostics(environment: dict[str, str]) -> dict[str, Any]:
    executable = shutil.which("nvcc")
    result: dict[str, Any] = {
        "version_environment": environment.get("CUDA_VERSION"),
        "home": environment.get("CUDA_HOME", "/usr/local/cuda"),
        "nvcc_available": executable is not None,
    }
    if executable is not None:
        ok, output = _command_output((executable, "--version"))
        result.update(nvcc_status="AVAILABLE" if ok else "RUNTIME_ERROR", nvcc_output=output)
    return result


def _normalized_version(value: object) -> str:
    return str(value).split("+")[0]


def _pytorch_diagnostics() -> dict[str, Any]:
    if importlib.util.find_spec("torch") is None:
        return {"status": "PYTORCH_NOT_INSTALLED", "cuda_operation": "NOT_ATTEMPTED"}
    try:
        import torch  # type: ignore[import-not-found]
        import torchaudio  # type: ignore[import-not-found]
        import torchvision  # type: ignore[import-not-found]
    except Exception as error:
        return {"status": "PYTORCH_IMPORT_ERROR", "detail": str(error), "cuda_operation": "NOT_ATTEMPTED"}

    versions = {
        "torch": str(torch.__version__),
        "torchvision": str(torchvision.__version__),
        "torchaudio": str(torchaudio.__version__),
    }
    versions_match = all(
        _normalized_version(versions[name]) == expected for name, expected in EXPECTED_PACKAGES.items()
    )
    cuda_build = torch.version.cuda
    cuda_available = bool(torch.cuda.is_available())
    result: dict[str, Any] = {
        "status": "PYTORCH_AVAILABLE" if versions_match and cuda_build == "12.4" else "PYTORCH_CONTRACT_MISMATCH",
        "versions": versions,
        "expected_versions": dict(EXPECTED_PACKAGES),
        "versions_match": versions_match,
        "cuda_build_version": cuda_build,
        "cuda_wheel": cuda_build is not None,
        "cuda_available": cuda_available,
        "device_count": int(torch.cuda.device_count()),
        "cuda_operation": "NOT_ATTEMPTED_NO_GPU",
    }
    if cuda_available:
        try:
            tensor = torch.tensor([1.0, 2.0, 3.0], device="cuda")
            value = float((tensor * 2).sum().cpu().item())
            torch.cuda.synchronize()
            if value != 12.0:
                raise RuntimeError(f"unexpected CUDA result: {value}")
            result.update(
                device_name=torch.cuda.get_device_name(0),
                cuda_operation="PYTORCH_CUDA_OPERATION_OK",
                cuda_operation_result=value,
            )
        except Exception as error:
            result.update(cuda_operation="PYTORCH_CUDA_OPERATION_FAILED", detail=str(error))
    return result


def _dependency_diagnostics() -> dict[str, Any]:
    imports: dict[str, dict[str, str]] = {}
    for name in REPRESENTATIVE_IMPORTS:
        try:
            module = importlib.import_module(name)
        except Exception as error:
            imports[name] = {"status": "IMPORT_ERROR", "detail": str(error)}
        else:
            imports[name] = {"status": "AVAILABLE", "version": str(getattr(module, "__version__", "UNKNOWN"))}
    ready = all(item["status"] == "AVAILABLE" for item in imports.values())
    return {"status": "DEPENDENCY_IMPORTS_READY" if ready else "DEPENDENCY_IMPORTS_FAILED", "imports": imports}


def _compiled_artifacts(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in {".so", ".pyd"}
    )


def _hunyuan_diagnostics(paths: dict[str, str], environment: dict[str, str]) -> dict[str, Any]:
    source = Path(paths["hunyuan_source"])
    expected_commit = environment.get("HUNYUAN_COMMIT", DEFAULT_HUNYUAN_COMMIT)
    revision_file = Path(environment.get("HUNYUAN_REVISION_FILE", "/opt/hunyuan3d.commit"))
    try:
        revision = revision_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        revision = None
    custom_artifacts = _compiled_artifacts(source / "hy3dpaint" / "custom_rasterizer")
    renderer_artifacts = _compiled_artifacts(source / "hy3dpaint" / "DifferentiableRenderer")
    return {
        "source_path": str(source),
        "source_present": source.is_dir(),
        "expected_revision": expected_commit,
        "revision": revision,
        "revision_matches": revision == expected_commit,
        "custom_rasterizer": {
            "status": "CUSTOM_RASTERIZER_BUILT_UNEXPECTED" if custom_artifacts else "CUSTOM_RASTERIZER_NOT_BUILT_EXPECTED",
            "compiled_artifacts": custom_artifacts,
        },
        "differentiable_renderer": {
            "status": "DIFFERENTIABLE_RENDERER_BUILT_UNEXPECTED" if renderer_artifacts else "DIFFERENTIABLE_RENDERER_NOT_BUILT_EXPECTED",
            "compiled_artifacts": renderer_artifacts,
        },
    }


def _model_diagnostics(paths: dict[str, str]) -> dict[str, Any]:
    roots = [Path(paths["model_cache"]), Path(paths["hunyuan_source"]) / "hy3dpaint" / "ckpt"]
    files = sorted(
        str(path)
        for root in roots
        if root.is_dir()
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in MODEL_EXTENSIONS
    )
    return {
        "status": "MODEL_WEIGHTS_PRESENT" if files else "MODEL_WEIGHTS_NOT_PRESENT_EXPECTED",
        "detected_files": files,
        "download_attempted": False,
    }


def collect_health(environment: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect JSON-serializable dependency health information."""
    env = dict(os.environ if environment is None else environment)
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "DIFFUSERS_OFFLINE"):
        env.setdefault(key, "1")
        os.environ[key] = env[key]
    paths = _configured_paths(env)
    nvidia = _nvidia_diagnostics()
    pytorch = _pytorch_diagnostics()
    dependencies = _dependency_diagnostics()
    hunyuan = _hunyuan_diagnostics(paths, env)
    models = _model_diagnostics(paths)
    dependencies_ready = all(
        (
            platform.python_version().startswith(EXPECTED_PYTHON),
            pytorch["status"] == "PYTORCH_AVAILABLE",
            dependencies["status"] == "DEPENDENCY_IMPORTS_READY",
            hunyuan["source_present"],
            hunyuan["revision_matches"],
            hunyuan["custom_rasterizer"]["status"] == "CUSTOM_RASTERIZER_NOT_BUILT_EXPECTED",
            hunyuan["differentiable_renderer"]["status"] == "DIFFERENTIABLE_RENDERER_NOT_BUILT_EXPECTED",
            models["status"] == "MODEL_WEIGHTS_NOT_PRESENT_EXPECTED",
        )
    )
    return {
        "status": "HUNYUAN_DEPENDENCIES_READY" if dependencies_ready else "HUNYUAN_DEPENDENCIES_NOT_READY",
        "full_hunyuan_ready": False,
        "gpu_status": nvidia["status"],
        "python": {"version": platform.python_version(), "expected": EXPECTED_PYTHON, "executable": sys.executable},
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "paths": paths,
        "cuda": _cuda_diagnostics(env),
        "nvidia": nvidia,
        "pytorch": pytorch,
        "dependencies": dependencies,
        "hunyuan": hunyuan,
        "models": models,
    }


def _format_human(report: dict[str, Any]) -> str:
    paths, cuda, nvidia = report["paths"], report["cuda"], report["nvidia"]
    pytorch, hunyuan = report["pytorch"], report["hunyuan"]
    lines = [
        f"STATUS={report['status']}",
        f"GPU_STATUS={report['gpu_status']}",
        f"PYTHON_VERSION={report['python']['version']}",
        f"PLATFORM={report['platform']['system']} {report['platform']['release']} ({report['platform']['machine']})",
        f"MODEL_CACHE_DIR={paths['model_cache']}",
        f"INPUT_DIR={paths['input']}",
        f"OUTPUT_DIR={paths['output']}",
        f"WORKSPACE_DIR={paths['workspace']}",
        f"HUNYUAN_SOURCE_DIR={paths['hunyuan_source']}",
        f"HUNYUAN_REVISION={hunyuan['revision'] or 'UNKNOWN'}",
        f"CUDA_VERSION={cuda['version_environment'] or 'UNKNOWN'}",
        f"CUDA_HOME={cuda['home']}",
        f"NVCC_AVAILABLE={'YES' if cuda['nvcc_available'] else 'NO'}",
        f"NVIDIA_SMI_AVAILABLE={'YES' if nvidia['smi_available'] else 'NO'}",
        f"PYTORCH_STATUS={pytorch['status']}",
        f"PYTORCH_CUDA_VERSION={pytorch.get('cuda_build_version') or 'NONE'}",
        f"PYTORCH_CUDA_AVAILABLE={'YES' if pytorch.get('cuda_available') else 'NO'}",
        f"CUSTOM_RASTERIZER_STATUS={hunyuan['custom_rasterizer']['status']}",
        f"DIFFERENTIABLE_RENDERER_STATUS={hunyuan['differentiable_renderer']['status']}",
        f"MODEL_WEIGHTS_STATUS={report['models']['status']}",
        "FULL_HUNYUAN_READY=NO",
    ]
    if pytorch.get("device_name"):
        lines.append(f"PYTORCH_GPU_DEVICE={pytorch['device_name']}")
    lines.extend(f"GPU_DEVICE={device}" for device in nvidia["devices"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--require-gpu", action="store_true", help="require a successful PyTorch CUDA operation")
    args = parser.parse_args(argv)
    report = collect_health()
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _format_human(report))
    if args.require_gpu and report["pytorch"].get("cuda_operation") != "PYTORCH_CUDA_OPERATION_OK":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
