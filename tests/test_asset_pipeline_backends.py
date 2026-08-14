"""Focused tests for the fixed shape-backend registry."""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from asset_pipeline import backends


def _docker_pin() -> tuple[str, str]:
    dockerfile = (
        Path(__file__).resolve().parents[1] / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")
    repository = ""
    revision = ""
    for line in dockerfile.splitlines():
        if line.startswith("ARG HUNYUAN_REPOSITORY="):
            repository = line.split("=", 1)[1]
        if line.startswith("ARG HUNYUAN_COMMIT="):
            revision = line.split("=", 1)[1]
    if not repository or not revision:
        raise AssertionError("Dockerfile Hunyuan pin not found")
    return repository, revision


class BackendDescriptorTests(unittest.TestCase):
    def test_canonical_descriptor_has_expected_fields(self):
        descriptor = backends.DEFAULT_REGISTRY.resolve(
            backends.CANONICAL_BACKEND_ID
        )
        self.assertEqual(descriptor.schema_version, 1)
        self.assertEqual(descriptor.backend_id, "hunyuan3d-2.1-shape")
        self.assertEqual(descriptor.stage, "shape")
        self.assertEqual(descriptor.implementation, "hunyuan3d-2.1")
        self.assertEqual(
            descriptor.source_repository,
            "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1.git",
        )
        self.assertEqual(
            descriptor.source_revision,
            "82920d643c0dc2f7bfd7255f45f62d386edfe60c",
        )
        self.assertEqual(descriptor.capabilities, ("image-to-shape-preparation",))
        self.assertIn(
            "VERIFIED_PRODUCTION_MODEL_MANIFEST_REQUIRED",
            descriptor.prerequisites,
        )
        self.assertIn("VERIFIED_EXTERNAL_MODEL_CACHE_REQUIRED", descriptor.prerequisites)
        self.assertIn("CUDA_CAPABLE_GPU_RUNTIME_REQUIRED", descriptor.prerequisites)
        self.assertIn("HUNYUAN_RUNTIME_IMPORTS_REQUIRED", descriptor.prerequisites)

    def test_canonical_descriptor_matches_docker_pin(self):
        descriptor = backends.DEFAULT_REGISTRY.resolve(
            backends.CANONICAL_BACKEND_ID
        )
        repository, revision = _docker_pin()
        self.assertEqual(descriptor.source_repository, repository)
        self.assertEqual(descriptor.source_revision, revision)

    def test_descriptor_is_immutable(self):
        descriptor = backends.DEFAULT_REGISTRY.resolve(
            backends.CANONICAL_BACKEND_ID
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            descriptor.backend_id = "other"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            descriptor.capabilities = descriptor.capabilities + ("other",)  # type: ignore[misc]


class BackendRegistryTests(unittest.TestCase):
    def test_default_registry_has_deterministic_order(self):
        self.assertEqual(
            backends.DEFAULT_REGISTRY.backend_ids,
            ("hunyuan3d-2.1-shape",),
        )
        self.assertEqual(
            backends.DEFAULT_REGISTRY.descriptors[0].backend_id,
            "hunyuan3d-2.1-shape",
        )

    def test_registry_sorts_multiple_descriptors_deterministically(self):
        def descriptor(backend_id: str) -> backends.ShapeBackendDescriptor:
            return backends.ShapeBackendDescriptor(
                schema_version=1,
                backend_id=backend_id,
                stage="shape",
                implementation="test",
                source_repository="https://example.invalid/repo",
                source_revision="a" * 40,
                capabilities=("test",),
                prerequisites=("TEST_REQUIRED",),
            )

        registry = backends.ShapeBackendRegistry(
            (descriptor("b-shape"), descriptor("a-shape"))
        )
        self.assertEqual(registry.backend_ids, ("a-shape", "b-shape"))

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(backends.UnknownShapeBackendError) as caught:
            backends.DEFAULT_REGISTRY.resolve("unknown-shape")
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(caught.exception.classification, "UNKNOWN_SHAPE_BACKEND")

    def test_empty_malformed_case_variant_and_padded_backend_are_rejected(self):
        cases = [
            "",
            " ",
            "Hunyuan3D-2.1-shape",
            " hunyuan3d-2.1-shape",
            "hunyuan3d-2.1-shape ",
            "bad backend",
        ]
        for backend_id in cases:
            with self.subTest(backend_id=backend_id):
                with self.assertRaises(backends.MalformedBackendIdError) as caught:
                    backends.DEFAULT_REGISTRY.resolve(backend_id)
                self.assertEqual(
                    caught.exception.classification, "MALFORMED_SHAPE_BACKEND"
                )

    def test_duplicate_registration_is_refused(self):
        descriptor = backends.DEFAULT_REGISTRY.descriptors[0]
        with self.assertRaises(backends.DuplicateBackendRegistrationError):
            backends.ShapeBackendRegistry((descriptor, descriptor))

    def test_registry_descriptors_are_defensively_exposed(self):
        descriptor = backends.DEFAULT_REGISTRY.resolve(
            backends.CANONICAL_BACKEND_ID
        )
        emitted = descriptor.to_dict()
        emitted["capabilities"].append("other")
        self.assertEqual(
            backends.DEFAULT_REGISTRY.resolve(
                backends.CANONICAL_BACKEND_ID
            ).capabilities,
            ("image-to-shape-preparation",),
        )


if __name__ == "__main__":
    unittest.main()


