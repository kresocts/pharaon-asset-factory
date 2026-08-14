"""Focused tests for immutable shape execution-request construction."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from asset_pipeline import backends, contract, execution, paths


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


if __name__ == "__main__":
    unittest.main()
