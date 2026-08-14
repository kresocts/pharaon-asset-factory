"""Focused tests for immutable shape execution-request construction."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from asset_pipeline import backends, contract, execution, models, paths


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def _document() -> dict:
    return {
        "schema_version": 1,
        "job_id": "pharaoh-001",
        "reference_image": "references/pharaoh.png",
        "seed": 12345,
        "remove_background": True,
    }


class ExecutionRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.input_dir = base / "input"
        self.output_dir = base / "output"
        self.workspace_dir = base / "workspace"
        for directory in (self.input_dir, self.output_dir, self.workspace_dir):
            directory.mkdir()
        (self.input_dir / "references").mkdir(parents=True)
        (self.input_dir / "references" / "pharaoh.png").write_bytes(PNG_BYTES)
        self.roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(self.input_dir),
                "OUTPUT_DIR": str(self.output_dir),
                "WORKSPACE_DIR": str(self.workspace_dir),
            }
        )
        self.backend = backends.DEFAULT_REGISTRY.resolve(
            backends.CANONICAL_BACKEND_ID
        )

    def _plan(self):
        document = contract.validate_job_document(_document())
        return document, paths.build_plan(document, self.roots)

    def test_request_is_derived_from_validated_plan_and_backend(self):
        document, plan = self._plan()
        request = execution.build_execution_request(document, plan, self.backend)
        self.assertEqual(request.schema_version, 1)
        self.assertEqual(request.job_id, "pharaoh-001")
        self.assertEqual(request.backend_id, "hunyuan3d-2.1-shape")
        self.assertEqual(request.seed, 12345)
        self.assertIs(request.remove_background, True)
        self.assertEqual(request.input_image, plan["paths"]["input_image"])
        self.assertEqual(request.output_directory, plan["paths"]["output_directory"])
        self.assertEqual(
            request.workspace_directory, plan["paths"]["workspace_directory"]
        )

    def test_request_is_immutable(self):
        document, plan = self._plan()
        request = execution.build_execution_request(document, plan, self.backend)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            request.job_id = "other"  # type: ignore[misc]

    def test_envelope_is_deterministic_and_contains_blockers(self):
        document, plan = self._plan()
        first = execution.build_preparation_envelope(document, plan, self.backend)
        second = execution.build_preparation_envelope(document, plan, self.backend)
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["status"], "VALID")
        self.assertEqual(
            first["classification"], "SHAPE_EXECUTION_REQUEST_READY"
        )
        self.assertEqual(first["exit_code"], 0)
        self.assertTrue(first["preparation_supported"])
        self.assertFalse(first["execution_supported"])
        self.assertEqual(
            {blocker["code"] for blocker in first["blockers"]},
            {
                "PRODUCTION_MODEL_MANIFEST_NOT_BOUND",
                "MODEL_CACHE_NOT_VERIFIED",
                "GPU_EXECUTION_NOT_IMPLEMENTED",
            },
        )
        self.assertEqual(first["backend"]["backend_id"], "hunyuan3d-2.1-shape")
        self.assertEqual(first["backend"]["source_revision"], backends.CANONICAL_SOURCE_REVISION)

    def test_mutating_emitted_dictionaries_does_not_mutate_request_or_backend(self):
        document, plan = self._plan()
        request = execution.build_execution_request(document, plan, self.backend)
        emitted = request.to_dict()
        emitted["job_id"] = "mutated"
        self.assertEqual(request.job_id, "pharaoh-001")

        backend_emitted = self.backend.to_dict()
        backend_emitted["backend_id"] = "mutated"
        self.assertEqual(self.backend.backend_id, "hunyuan3d-2.1-shape")

    def test_envelope_json_has_no_nondeterministic_fields(self):
        document, plan = self._plan()
        envelope = execution.build_preparation_envelope(document, plan, self.backend)
        raw = json.dumps(envelope)
        self.assertNotIn("timestamp", raw.lower())
        self.assertNotIn("uuid", raw.lower())
        self.assertNotIn("pid", raw.lower())
        self.assertNotIn("hostname", raw.lower())
        self.assertNotIn("username", raw.lower())


class PreflightEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.input_dir = base / "input"
        self.output_dir = base / "output"
        self.workspace_dir = base / "workspace"
        for directory in (self.input_dir, self.output_dir, self.workspace_dir):
            directory.mkdir()
        (self.input_dir / "references").mkdir(parents=True)
        (self.input_dir / "references" / "pharaoh.png").write_bytes(PNG_BYTES)
        self.roots = paths.load_runtime_roots(
            {
                "INPUT_DIR": str(self.input_dir),
                "OUTPUT_DIR": str(self.output_dir),
                "WORKSPACE_DIR": str(self.workspace_dir),
            }
        )
        self.backend = backends.DEFAULT_REGISTRY.resolve(
            backends.CANONICAL_BACKEND_ID
        )
        document = contract.validate_job_document(_document())
        self.plan = paths.build_plan(document, self.roots)
        self.document = document

    def _binding(self):
        revision = "a" * 40
        files = (
            models.ModelFileBinding("config/model.json", "shape-config", 1, "0" * 64),
            models.ModelFileBinding(
                "weights/model.safetensors", "shape-weights", 2, "1" * 64
            ),
        )
        return models.ModelBinding(
            schema_version=1,
            backend_id="hunyuan3d-2.1-shape",
            artifact_set="hunyuan3d-2.1-shape",
            revision=revision,
            namespace=f"hunyuan3d-2.1-shape/{revision}",
            plan_id="2" * 64,
            model_root="/models/hunyuan3d-2.1-shape/" + revision,
            file_count=2,
            total_expected_bytes=3,
            files=files,
        )

    def _verification(self):
        return {
            "schema_version": 1,
            "command": "verify",
            "success": True,
            "classification": "OK",
            "exit_code": 0,
            "artifact_set": "hunyuan3d-2.1-shape",
            "revision": "a" * 40,
            "namespace": "hunyuan3d-2.1-shape/" + "a" * 40,
            "plan_id": "2" * 64,
            "cache_root": "/models",
            "file_count": 2,
            "file_counts": {
                "ABSENT": 0,
                "PARTIAL": 0,
                "CORRUPTED": 0,
                "VERIFIED": 2,
            },
            "bytes": {"total_expected": 3, "required": 0, "max_bytes": None},
            "fully_cached": True,
            "files": [],
        }

    def test_preflight_envelope_is_deterministic_and_complete(self):
        binding = self._binding()
        verification = self._verification()
        first = execution.build_preflight_envelope(
            self.document, self.plan, self.backend, binding, verification
        )
        second = execution.build_preflight_envelope(
            self.document, self.plan, self.backend, binding, verification
        )
        self.assertEqual(first, second)
        self.assertEqual(first["classification"], "SHAPE_MODEL_PREFLIGHT_READY")
        self.assertEqual(first["exit_code"], 0)
        self.assertTrue(first["preparation_supported"])
        self.assertTrue(first["model_binding_supported"])
        self.assertTrue(first["model_cache_verified"])
        self.assertFalse(first["execution_supported"])
        self.assertEqual(
            [blocker["code"] for blocker in first["blockers"]],
            ["GPU_EXECUTION_NOT_IMPLEMENTED"],
        )
        self.assertEqual(first["cache_verification"]["required_bytes"], 0)
        self.assertTrue(first["cache_verification"]["fully_cached"])
        self.assertNotIn("url", json.dumps(first["model_binding"]))

    def test_preflight_refuses_mismatched_binding_and_verification(self):
        binding = self._binding()
        verification = self._verification()
        verification["plan_id"] = "9" * 64
        with self.assertRaises(models.ModelCacheVerificationError) as raised:
            execution.build_preflight_envelope(
                self.document, self.plan, self.backend, binding, verification
            )
        self.assertIn("plan_id", str(raised.exception))

    def test_preflight_emitted_dictionaries_are_defensive(self):
        binding = self._binding()
        envelope = execution.build_preflight_envelope(
            self.document, self.plan, self.backend, binding, self._verification()
        )
        envelope["model_binding"]["files"].append({"path": "mutated"})
        envelope["cache_verification"]["state_counts"]["VERIFIED"] = 999
        self.assertEqual(len(binding.files), 2)
        self.assertEqual(binding.files[0].path, "config/model.json")


if __name__ == "__main__":
    unittest.main()
