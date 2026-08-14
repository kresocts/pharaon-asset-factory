"""Canonical command-line interface for the offline shape-job planner."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional, Sequence

from .contract import ContractError, read_job_document
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
            "message": "internal error while planning shape job",
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    json_mode = bool(getattr(args, "json", False))
    if args.command != "shape" or args.shape_command != "plan":
        parser.error("expected 'shape plan --job JOB [--json]'")

    try:
        document = read_job_document(args.job)
        roots = load_runtime_roots()
        plan = build_plan(document, roots)
    except (
        ContractError,
        RuntimeRootError,
        InputPolicyError,
        SafePathError,
    ) as exc:
        return _emit_expected_error(exc, json_mode)
    except Exception as exc:  # pragma: no cover - defensive unexpected boundary
        return _emit_unexpected_error(exc, json_mode)

    if json_mode:
        print(json.dumps(plan, indent=2))
    else:
        _print_human_plan(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
