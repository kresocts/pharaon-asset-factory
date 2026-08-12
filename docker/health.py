#!/usr/bin/env python3
"""Report base-container runtime health without requiring a GPU or PyTorch."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from typing import Any, Sequence


DEFAULT_PATHS = {
    "model_cache": "/models",
    "input": "/data/input",
    "output": "/data/output",
    "workspace": "/workspace",
}


def _command_output(command: Sequence[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    output = (result.stdout or result.stderr).strip()
    return result.returncode == 0, output


def _configured_paths(environment: dict[str, str]) -> dict[str, str]:
    return {
        "model_cache": environment.get("MODEL_CACHE_DIR", DEFAULT_PATHS["model_cache"]),
        "input": environment.get("INPUT_DIR", DEFAULT_PATHS["input"]),
        "output": environment.get("OUTPUT_DIR", DEFAULT_PATHS["output"]),
        "workspace": environment.get("WORKSPACE_DIR", DEFAULT_PATHS["workspace"]),
    }


def _nvidia_diagnostics() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return {"smi_available": False, "status": "GPU_NOT_AVAILABLE", "devices": []}

    ok, output = _command_output(
        (executable, "--query-gpu=index,name,driver_version,memory.total", "--format=csv,noheader")
    )
    if not ok:
        return {
            "smi_available": True,
            "status": "GPU_RUNTIME_ERROR",
            "devices": [],
            "detail": output,
        }
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
        result["nvcc_status"] = "AVAILABLE" if ok else "RUNTIME_ERROR"
        result["nvcc_output"] = output
    return result


def _pytorch_diagnostics() -> dict[str, Any]:
    if importlib.util.find_spec("torch") is None:
        return {"status": "PYTORCH_NOT_INSTALLED"}
    try:
        import torch  # type: ignore[import-not-found]
    except Exception as error:  # Import failures are diagnostic data.
        return {"status": "PYTORCH_IMPORT_ERROR", "detail": str(error)}
    return {
        "status": "PYTORCH_AVAILABLE",
        "version": torch.__version__,
        "cuda_build_version": torch.version.cuda,
        "cuda_available": bool(torch.cuda.is_available()),
        "device_count": int(torch.cuda.device_count()),
    }


def collect_health(environment: dict[str, str] | None = None) -> dict[str, Any]:
    """Collect JSON-serializable health information."""
    env = dict(os.environ if environment is None else environment)
    nvidia = _nvidia_diagnostics()
    return {
        "status": nvidia["status"],
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "paths": _configured_paths(env),
        "cuda": _cuda_diagnostics(env),
        "nvidia": nvidia,
        "pytorch": _pytorch_diagnostics(),
    }


def _format_human(report: dict[str, Any]) -> str:
    paths = report["paths"]
    cuda = report["cuda"]
    nvidia = report["nvidia"]
    pytorch = report["pytorch"]
    lines = [
        f"STATUS={report['status']}",
        f"PYTHON_VERSION={report['python']['version']}",
        f"PLATFORM={report['platform']['system']} {report['platform']['release']} ({report['platform']['machine']})",
        f"MODEL_CACHE_DIR={paths['model_cache']}",
        f"INPUT_DIR={paths['input']}",
        f"OUTPUT_DIR={paths['output']}",
        f"WORKSPACE_DIR={paths['workspace']}",
        f"CUDA_VERSION={cuda['version_environment'] or 'UNKNOWN'}",
        f"CUDA_HOME={cuda['home']}",
        f"NVCC_AVAILABLE={'YES' if cuda['nvcc_available'] else 'NO'}",
        f"NVIDIA_SMI_AVAILABLE={'YES' if nvidia['smi_available'] else 'NO'}",
        f"GPU_STATUS={nvidia['status']}",
        f"PYTORCH_STATUS={pytorch['status']}",
    ]
    lines.extend(f"GPU_DEVICE={device}" for device in nvidia["devices"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--require-gpu",
        action="store_true",
        help="return non-zero unless a GPU is visible through nvidia-smi",
    )
    args = parser.parse_args(argv)
    report = collect_health()
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _format_human(report))
    return 2 if args.require_gpu and report["status"] != "GPU_AVAILABLE" else 0


if __name__ == "__main__":
    raise SystemExit(main())
