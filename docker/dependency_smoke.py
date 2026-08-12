#!/usr/bin/env python3
"""Import representative Hunyuan dependencies without accessing model hubs."""

from __future__ import annotations

import importlib
import json
import os


EXPECTED = {"torch": "2.5.1", "torchvision": "0.20.1", "torchaudio": "2.5.1"}
REPRESENTATIVE_IMPORTS = (
    "torch",
    "torchvision",
    "torchaudio",
    "transformers",
    "diffusers",
    "accelerate",
    "trimesh",
    "numpy",
)
OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "DIFFUSERS_OFFLINE": "1",
}


def collect_imports() -> dict[str, object]:
    """Return a stable import/version report without loading any model."""
    os.environ.update(OFFLINE_ENVIRONMENT)
    imports: dict[str, dict[str, object]] = {}
    ready = True
    for name in REPRESENTATIVE_IMPORTS:
        try:
            module = importlib.import_module(name)
        except Exception as error:
            imports[name] = {"status": "IMPORT_ERROR", "detail": str(error)}
            ready = False
            continue
        version = str(getattr(module, "__version__", "UNKNOWN"))
        expected = EXPECTED.get(name)
        version_matches = expected is None or version.split("+")[0] == expected
        imports[name] = {
            "status": "AVAILABLE" if version_matches else "VERSION_MISMATCH",
            "version": version,
        }
        ready = ready and version_matches
    return {
        "status": "DEPENDENCY_IMPORTS_READY" if ready else "DEPENDENCY_IMPORTS_FAILED",
        "offline_guards": dict(OFFLINE_ENVIRONMENT),
        "imports": imports,
    }


def main() -> int:
    report = collect_imports()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "DEPENDENCY_IMPORTS_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
