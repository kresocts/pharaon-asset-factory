"""Immutable shape execution-request construction.

This module turns an already-validated T-0021 job document, the already-built
offline plan, and one resolved backend descriptor into a deterministic
execution-request envelope. It performs no validation of the document or paths
itself; the CLI must call the T-0021 contract and path-policy functions first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .backends import ShapeBackendDescriptor


EXECUTION_REQUEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ShapeExecutionRequest:
    """Normalized, immutable values for a future shape execution handoff."""

    schema_version: int
    job_id: str
    backend_id: str
    input_image: str
    output_directory: str
    workspace_directory: str
    seed: int
    remove_background: bool

    def to_dict(self) -> dict[str, object]:
        """Return a new JSON-compatible dictionary."""

        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "backend_id": self.backend_id,
            "input_image": self.input_image,
            "output_directory": self.output_directory,
            "workspace_directory": self.workspace_directory,
            "seed": self.seed,
            "remove_background": self.remove_background,
        }


def build_execution_request(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    backend: ShapeBackendDescriptor,
) -> ShapeExecutionRequest:
    """Derive an immutable execution request from a validated T-0021 plan.

    The values come only from the normalized job document, the plan's
    policy-checked paths, and the resolved backend identity.
    """

    job = plan["job"]
    paths = plan["paths"]
    return ShapeExecutionRequest(
        schema_version=EXECUTION_REQUEST_SCHEMA_VERSION,
        job_id=job["job_id"],
        backend_id=backend.backend_id,
        input_image=paths["input_image"],
        output_directory=paths["output_directory"],
        workspace_directory=paths["workspace_directory"],
        seed=job["seed"],
        remove_background=job["remove_background"],
    )


def _blockers() -> tuple[dict[str, str], ...]:
    return (
        {
            "code": "PRODUCTION_MODEL_MANIFEST_NOT_BOUND",
            "message": (
                "A verified production model manifest has not been bound by "
                "this preparation contract."
            ),
        },
        {
            "code": "MODEL_CACHE_NOT_VERIFIED",
            "message": (
                "External model-cache verification is not implemented by this "
                "preparation contract."
            ),
        },
        {
            "code": "GPU_EXECUTION_NOT_IMPLEMENTED",
            "message": (
                "Hunyuan GPU execution is not implemented by this preparation "
                "contract."
            ),
        },
    )


def build_preparation_envelope(
    document: Mapping[str, Any],
    plan: Mapping[str, Any],
    backend: ShapeBackendDescriptor,
) -> dict[str, Any]:
    """Build the deterministic shape-preparation JSON envelope.

    This function creates new dictionaries and lists, so mutating the result
    cannot mutate the immutable backend descriptor or the execution request.
    """

    execution_request = build_execution_request(document, plan, backend)
    return {
        "schema_version": 1,
        "status": "VALID",
        "classification": "SHAPE_EXECUTION_REQUEST_READY",
        "exit_code": 0,
        "stage": "shape",
        "preparation_supported": True,
        "execution_supported": False,
        "job": {
            "schema_version": plan["job"]["schema_version"],
            "job_id": plan["job"]["job_id"],
            "reference_image": plan["job"]["reference_image"],
            "seed": plan["job"]["seed"],
            "remove_background": plan["job"]["remove_background"],
        },
        "paths": {
            "input_image": plan["paths"]["input_image"],
            "output_directory": plan["paths"]["output_directory"],
            "workspace_directory": plan["paths"]["workspace_directory"],
        },
        "backend": backend.to_dict(),
        "execution_request": execution_request.to_dict(),
        "blockers": _blockers(),
    }
