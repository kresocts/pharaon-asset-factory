#!/usr/bin/env python3
"""Prove that the pinned PyTorch CUDA build performs a tiny GPU operation."""

from __future__ import annotations

import json


def main() -> int:
    try:
        import torch
    except Exception as error:
        print(json.dumps({"status": "PYTORCH_IMPORT_ERROR", "detail": str(error)}))
        return 1

    report = {
        "status": "GPU_NOT_AVAILABLE",
        "torch_version": torch.__version__,
        "torch_cuda_version": torch.version.cuda,
        "cuda_available": bool(torch.cuda.is_available()),
    }
    if not torch.cuda.is_available():
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2

    try:
        source = torch.tensor([1.0, 2.0, 3.0], device="cuda")
        result = (source * 2).sum()
        torch.cuda.synchronize()
        value = float(result.cpu().item())
        if value != 12.0:
            raise RuntimeError(f"unexpected CUDA result: {value}")
        report.update(
            status="PYTORCH_CUDA_OPERATION_OK",
            device_name=torch.cuda.get_device_name(0),
            device_count=torch.cuda.device_count(),
            operation_result=value,
        )
    except Exception as error:
        report.update(status="PYTORCH_CUDA_OPERATION_FAILED", detail=str(error))
        print(json.dumps(report, indent=2, sort_keys=True))
        return 3

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
