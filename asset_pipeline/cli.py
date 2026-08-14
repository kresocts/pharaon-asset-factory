"""Canonical command-line interface for the offline shape-job planner."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional, Sequence

from .backends import BackendError, DEFAULT_REGISTRY
from .contract import ContractError, read_job_document
from .execution import build_preflight_envelope, build_preparation_envelope
from .models import (
    ModelBindingError,
    ModelCacheVerificationError,
    ModelManifestError,
    bind_model_manifest,
)
from docker import model_cache
from .paths import (
    InputPolicyError,
    RuntimeRootError,
    SafePathError,
    build_plan,
    load_runtime_roots,
)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(64, f"{self.prog}: error: {message}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="python -m asset_pipeline.cli",
        description=(
            "Validate one shape job and emit a deterministic offline execution plan. "
            "This command performs no writes, downloads, or network requests."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    shape = subparsers.add_parser("shape", help="shape-worker operations")
    shape_subparsers = shape.add_subparsers(dest="shape_command", required=True)

    plan = shape_subparsers.add_parser(
        "plan", help="validate a shape job and print its plan"
    )
    plan.add_argument(
        "--job",
        required=True,
        help="path to the shape-job JSON document",
    )
    plan.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON (the authoritative output)",
    )

    prepare = shape_subparsers.add_parser(
        "prepare",
        help="validate a shape job, resolve a backend, and print an execution request",
    )
    prepare.add_argument(
        "--job",
        required=True,
        help="path to the shape-job JSON document",
    )
    prepare.add_argument(
        "--backend",
        required=True,
        help="explicit shape backend ID from the fixed local registry",
    )
    prepare.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON (the authoritative output)",
    )

    preflight = shape_subparsers.add_parser(
        "preflight",
        help="bind one immutable shape model manifest and verify the offline cache",
    )
    preflight.add_argument(
        "--job",
        required=True,
        help="path to the shape-job JSON document",
    )
    preflight.add_argument(
        "--backend",
        required=True,
        help="explicit shape backend ID from the fixed local registry",
    )
    preflight.add_argument(
        "--model-manifest",
        required=True,
        help="path to the operator-supplied immutable model manifest",
    )
    preflight.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON (the authoritative output)",
    )
    return parser


def _emit_expected_error(exc: Exception, json_mode: bool) -> int:
    payload = {
        "schema_version": 1,
        "status": exc.status,
        "classification": exc.classification,
        "exit_code": exc.exit_code,
        "message": str(exc),
    }
    if json_mode:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{exc.classification}: {exc}", file=sys.stderr)
    return exc.exit_code


def _emit_unexpected_error(exc: Exception, json_mode: bool) -> int:
    if json_mode:
        payload = {
            "schema_version": 1,
            "status": "ERROR",
            "classification": "INTERNAL_ERROR",
            "exit_code": 70,
            "message": "internal error while handling shape command",
        }
        print(json.dumps(payload, indent=2))
        print(f"internal error: {type(exc).__name__}", file=sys.stderr)
    else:
        print(f"internal error: {type(exc).__name__}", file=sys.stderr)
    return 70


def _print_human_plan(plan: dict[str, Any]) -> None:
    job = plan["job"]
    paths = plan["paths"]
    requirements = plan["requirements"]
    print("SHAPE_JOB_CONTRACT_READY")
    print(f"job_id: {job['job_id']}")
    print(f"reference_image: {job['reference_image']}")
    print(f"seed: {job['seed']}")
    print(f"remove_background: {job['remove_background']}")
    print(f"input_image: {paths['input_image']}")
    print(f"output_directory: {paths['output_directory']}")
    print(f"workspace_directory: {paths['workspace_directory']}")
    print(f"execution_supported: false")
    print(f"inference_backend: {requirements['inference_backend']}")
    print(f"model_weights: {requirements['model_weights']}")
    print(f"gpu: {requirements['gpu']}")


def _print_human_preflight(envelope: dict[str, Any]) -> None:
    job = envelope["job"]
    paths = envelope["paths"]
    backend = envelope["backend"]
    binding = envelope["model_binding"]
    cache = envelope["cache_verification"]
    print("SHAPE_MODEL_PREFLIGHT_READY")
    print(f"job_id: {job['job_id']}")
    print(f"reference_image: {job['reference_image']}")
    print(f"seed: {job['seed']}")
    print(f"remove_background: {job['remove_background']}")
    print(f"backend_id: {backend['backend_id']}")
    print(f"input_image: {paths['input_image']}")
    print(f"output_directory: {paths['output_directory']}")
    print(f"workspace_directory: {paths['workspace_directory']}")
    print(f"model_manifest_plan_id: {binding['plan_id']}")
    print(f"model_cache_verified: {cache['fully_cached']}")
    print("execution_supported: false")
    for blocker in envelope["blockers"]:
        print(f"blocker: {blocker['code']}")


def _print_human_prepare(envelope: dict[str, Any]) -> None:
    job = envelope["job"]
    paths = envelope["paths"]
    backend = envelope["backend"]
    request = envelope["execution_request"]
    print("SHAPE_EXECUTION_REQUEST_READY")
    print(f"job_id: {job['job_id']}")
    print(f"reference_image: {job['reference_image']}")
    print(f"seed: {job['seed']}")
    print(f"remove_background: {job['remove_background']}")
    print(f"backend_id: {backend['backend_id']}")
    print(f"source_repository: {backend['source_repository']}")
    print(f"source_revision: {backend['source_revision']}")
    print(f"input_image: {paths['input_image']}")
    print(f"output_directory: {paths['output_directory']}")
    print(f"workspace_directory: {paths['workspace_directory']}")
    print(f"execution_request.job_id: {request['job_id']}")
    print("execution_supported: false")
    for blocker in envelope["blockers"]:
        print(f"blocker: {blocker['code']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    json_mode = bool(getattr(args, "json", False))
    if args.command != "shape" or args.shape_command not in {"plan", "prepare", "preflight"}:
        parser.error(
            "expected 'shape plan --job JOB [--json]', "
            "'shape prepare --job JOB --backend BACKEND [--json]', or "
            "'shape preflight --job JOB --backend BACKEND "
            "--model-manifest MANIFEST [--json]'"
        )

    try:
        document = read_job_document(args.job)
        roots = load_runtime_roots()
        plan = build_plan(document, roots)
        if args.shape_command == "prepare":
            backend = DEFAULT_REGISTRY.resolve(args.backend)
            result = build_preparation_envelope(document, plan, backend)
        elif args.shape_command == "preflight":
            backend = DEFAULT_REGISTRY.resolve(args.backend)
            cache_root = model_cache.cache_root_from_environment()
            binding = bind_model_manifest(
                args.model_manifest,
                backend_id=backend.backend_id,
                cache_root=cache_root,
            )
            verification = model_cache.verify_manifest_cache(
                args.model_manifest, cache_root
            )
            if not verification["fully_cached"]:
                raise ModelCacheVerificationError(
                    "one or more model cache artifacts are not verified"
                )
            result = build_preflight_envelope(
                document, plan, backend, binding, verification
            )
        else:
            result = plan
    except (
        ContractError,
        RuntimeRootError,
        InputPolicyError,
        SafePathError,
        BackendError,
        ModelBindingError,
        ModelManifestError,
        ModelCacheVerificationError,
        model_cache.ManifestValidationError,
    ) as exc:
        return _emit_expected_error(exc, json_mode)
    except Exception as exc:  # pragma: no cover - defensive unexpected boundary
        return _emit_unexpected_error(exc, json_mode)

    if json_mode:
        print(json.dumps(result, indent=2))
    elif args.shape_command == "prepare":
        _print_human_prepare(result)
    elif args.shape_command == "preflight":
        _print_human_preflight(result)
    else:
        _print_human_plan(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
