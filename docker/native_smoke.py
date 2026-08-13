#!/usr/bin/env python3
"""Load both pinned native extensions and optionally exercise the CUDA rasterizer."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
from typing import Sequence


OFFLINE_ENVIRONMENT = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "DIFFUSERS_OFFLINE": "1"}


def _module_path(module: object) -> str | None:
    value = getattr(module, "__file__", None)
    return str(Path(value).resolve()) if value else None


def _renderer_operation(module: object) -> dict[str, object]:
    import numpy as np

    texture = np.ones((2, 2, 3), dtype=np.float32)
    mask = np.full((2, 2), 255, dtype=np.uint8)
    vertices = np.array([[-1, -1, 0], [1, -1, 0], [0, 1, 0]], dtype=np.float32)
    uv = np.array([[0, 0], [1, 0], [0.5, 1]], dtype=np.float32)
    faces = np.array([[0, 1, 2]], dtype=np.int32)
    colors, vertex_mask = module.meshVerticeColor(texture, mask, vertices, uv, faces, faces)
    if colors.shape != (3, 3) or vertex_mask.shape != (3,):
        raise RuntimeError(f"unexpected renderer output: {colors.shape}, {vertex_mask.shape}")
    if not np.isfinite(colors).all():
        raise RuntimeError("renderer returned non-finite values")
    return {"status": "RENDERER_NATIVE_OPERATION_OK", "color_shape": list(colors.shape)}


def _rasterizer_gpu_operation(module: object) -> dict[str, object]:
    import torch

    if not torch.cuda.is_available():
        return {"status": "NOT_ATTEMPTED_NO_GPU"}
    positions = torch.tensor(
        [[[-0.75, -0.75, 0.0, 1.0], [0.75, -0.75, 0.0, 1.0], [0.0, 0.75, 0.0, 1.0]]],
        dtype=torch.float32,
        device="cuda",
    )
    triangles = torch.tensor([[0, 1, 2]], dtype=torch.int32, device="cuda")
    face_indices, barycentric = module.rasterize(positions, triangles, (8, 8))
    torch.cuda.synchronize()
    if tuple(face_indices.shape) != (8, 8) or tuple(barycentric.shape) != (8, 8, 3):
        raise RuntimeError(f"unexpected rasterizer shapes: {face_indices.shape}, {barycentric.shape}")
    covered = int((face_indices > 0).sum().item())
    if not torch.isfinite(barycentric).all() or covered == 0:
        raise RuntimeError("synthetic triangle produced no finite covered pixels")
    return {"status": "CUSTOM_RASTERIZER_CUDA_OPERATION_OK", "covered_pixels": covered, "device_name": torch.cuda.get_device_name(0)}


def collect_native_smoke(run_gpu_operation: bool = False) -> dict[str, object]:
    os.environ.update(OFFLINE_ENVIRONMENT)
    report: dict[str, object] = {"offline_guards": dict(OFFLINE_ENVIRONMENT)}
    try:
        importlib.import_module("torch")
        custom = importlib.import_module("custom_rasterizer")
        kernel = importlib.import_module("custom_rasterizer_kernel")
        renderer = importlib.import_module("hy3dpaint.DifferentiableRenderer.mesh_inpaint_processor")
        report.update(
            custom_rasterizer={"status": "IMPORT_OK", "module_path": _module_path(custom)},
            custom_rasterizer_kernel={"status": "IMPORT_OK", "module_path": _module_path(kernel)},
            differentiable_renderer={"status": "IMPORT_OK", "module_path": _module_path(renderer), "symbols": ["meshVerticeInpaint", "meshVerticeColor"]},
        )
        report["renderer_operation"] = _renderer_operation(renderer)
        report["custom_rasterizer_operation"] = _rasterizer_gpu_operation(custom) if run_gpu_operation else {"status": "NOT_ATTEMPTED_IMPORT_ONLY"}
    except Exception as error:
        report.update(status="HUNYUAN_NATIVE_EXTENSIONS_FAILED", detail=str(error))
        return report
    report["status"] = "HUNYUAN_NATIVE_EXTENSIONS_READY"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-gpu-operation", action="store_true")
    args = parser.parse_args(argv)
    report = collect_native_smoke(args.require_gpu_operation)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["status"] != "HUNYUAN_NATIVE_EXTENSIONS_READY":
        return 1
    operation = report["custom_rasterizer_operation"]
    if args.require_gpu_operation and operation["status"] != "CUSTOM_RASTERIZER_CUDA_OPERATION_OK":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
