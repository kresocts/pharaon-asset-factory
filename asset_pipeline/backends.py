"""Explicit, versioned shape-backend registry.

The asset worker intentionally uses a small fixed registry instead of plugin
discovery. Backend identities are plain local strings; they are never used to
import modules, resolve entry points, crawl the filesystem, or load code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


BACKEND_SCHEMA_VERSION = 1
CANONICAL_BACKEND_ID = "hunyuan3d-2.1-shape"
CANONICAL_IMPLEMENTATION = "hunyuan3d-2.1"
CANONICAL_SOURCE_REPOSITORY = (
    "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git"
)
CANONICAL_SOURCE_REVISION = "82920d643c0dc2f7bfd7255f45f62d386edfe60c"

_BACKEND_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_MAX_BACKEND_ID_LENGTH = 64


class BackendError(Exception):
    """Base class for expected shape-backend request failures."""

    exit_code = 2
    status = "INVALID"
    classification = "INVALID_SHAPE_BACKEND"


class MalformedBackendIdError(BackendError):
    classification = "MALFORMED_SHAPE_BACKEND"


class UnknownShapeBackendError(BackendError):
    classification = "UNKNOWN_SHAPE_BACKEND"


class DuplicateBackendRegistrationError(ValueError):
    """Raised when a registry is constructed with duplicate backend IDs."""


@dataclass(frozen=True, slots=True)
class ShapeBackendDescriptor:
    """Immutable metadata for one registered shape backend."""

    schema_version: int
    backend_id: str
    stage: str
    implementation: str
    source_repository: str
    source_revision: str
    capabilities: tuple[str, ...]
    prerequisites: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a new JSON-compatible dictionary without exposing internals."""

        return {
            "schema_version": self.schema_version,
            "backend_id": self.backend_id,
            "stage": self.stage,
            "implementation": self.implementation,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "capabilities": list(self.capabilities),
            "prerequisites": list(self.prerequisites),
        }


def validate_backend_id(backend_id: object) -> str:
    """Validate a backend ID and return the exact registered form.

    Validation deliberately does not normalize case or trim whitespace: an
    unknown, case-variant, empty, malformed, or whitespace-padded ID is refused.
    """

    if not isinstance(backend_id, str):
        raise MalformedBackendIdError("backend must be a string")
    if not backend_id:
        raise MalformedBackendIdError("backend must not be empty")
    if len(backend_id) > _MAX_BACKEND_ID_LENGTH:
        raise MalformedBackendIdError(
            f"backend must be at most {_MAX_BACKEND_ID_LENGTH} characters"
        )
    if backend_id != backend_id.strip():
        raise MalformedBackendIdError("backend must not be whitespace-padded")
    if not _BACKEND_ID_RE.fullmatch(backend_id):
        raise MalformedBackendIdError(
            "backend must start and end with a lowercase ASCII letter or digit "
            "and may contain only lowercase ASCII letters, digits, '.', '_', and '-'"
        )
    return backend_id


def _canonical_descriptor() -> ShapeBackendDescriptor:
    return ShapeBackendDescriptor(
        schema_version=BACKEND_SCHEMA_VERSION,
        backend_id=CANONICAL_BACKEND_ID,
        stage="shape",
        implementation=CANONICAL_IMPLEMENTATION,
        source_repository=CANONICAL_SOURCE_REPOSITORY,
        source_revision=CANONICAL_SOURCE_REVISION,
        capabilities=("image-to-shape-preparation",),
        prerequisites=(
            "VERIFIED_PRODUCTION_MODEL_MANIFEST_REQUIRED",
            "VERIFIED_EXTERNAL_MODEL_CACHE_REQUIRED",
            "CUDA_CAPABLE_GPU_RUNTIME_REQUIRED",
            "HUNYUAN_RUNTIME_IMPORTS_REQUIRED",
        ),
    )


class ShapeBackendRegistry:
    """A fixed, deterministic, read-only registry of shape backends."""

    __slots__ = ("_descriptors", "_by_id")

    def __init__(self, descriptors: Iterable[ShapeBackendDescriptor]) -> None:
        ordered = tuple(
            sorted(descriptors, key=lambda descriptor: descriptor.backend_id)
        )
        by_id: dict[str, ShapeBackendDescriptor] = {}
        for descriptor in ordered:
            backend_id = validate_backend_id(descriptor.backend_id)
            if backend_id in by_id:
                raise DuplicateBackendRegistrationError(
                    f"duplicate backend registration: {backend_id}"
                )
            by_id[backend_id] = descriptor
        self._descriptors = ordered
        self._by_id = by_id

    @property
    def descriptors(self) -> tuple[ShapeBackendDescriptor, ...]:
        return self._descriptors

    @property
    def backend_ids(self) -> tuple[str, ...]:
        return tuple(descriptor.backend_id for descriptor in self._descriptors)

    def get(self, backend_id: str) -> ShapeBackendDescriptor:
        """Resolve one registered backend, refusing malformed or unknown IDs."""

        canonical_id = validate_backend_id(backend_id)
        try:
            return self._by_id[canonical_id]
        except KeyError as exc:
            raise UnknownShapeBackendError(
                f"unknown shape backend: {canonical_id}"
            ) from exc

    def resolve(self, backend_id: str) -> ShapeBackendDescriptor:
        """Alias for :meth:`get` for clear caller intent."""

        return self.get(backend_id)


def build_default_registry() -> ShapeBackendRegistry:
    """Build the production registry containing only explicitly known backends."""

    return ShapeBackendRegistry((_canonical_descriptor(),))


DEFAULT_REGISTRY = build_default_registry()
