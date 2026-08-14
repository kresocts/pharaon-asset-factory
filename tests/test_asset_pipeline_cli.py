"""Focused tests for the canonical ``shape plan`` command-line interface."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asset_pipeline import cli


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 9


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.input_dir = base / "input"
        self.output_dir = base / "output"
        self.workspace_dir = base / "workspace"
        for directory in (self.input_dir, self.output_dir, self.workspace_dir):
            directory.mkdir()
        self.env = {
            "INPUT_DIR": str(self.input_dir),
            "OUTPUT_DIR": str(self.output_dir),
            "WORKSPACE_DIR": str(self.workspace_dir),
        }
        (self.input_dir / "references").mkdir(parents=True)
        (self.input_dir / "references" / "pharaoh.png").write_bytes(PNG_BYTES)
        self.job_path = base / "job.json"
        self._write_job(self._valid_job_json())

    def _valid_job_json(self):
        return json.dumps(
            {
                "schema_version": 1,
                "job_id": "pharaoh-001",
                "reference_image": "references/pharaoh.png",
                "seed": 12345,
                "remove_background": True,
            }
        )

    def _write_job(self, text: str):
        self.job_path.write_text(text, encoding="utf-8")

    def _run(self, args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_valid_json_plan_is_repeatable_and_byte_identical(self):
        args = ["shape", "plan", "--job", str(self.job_path), "--json"]
        first_code, first_out, first_err = self._run(args)
        second_code, second_out, second_err = self._run(args)
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(first_out, second_out)
        self.assertEqual(first_err, "")
        self.assertEqual(second_err, "")
        payload = json.loads(first_out)
        self.assertEqual(payload["status"], "VALID")
        self.assertEqual(payload["classification"], "SHAPE_JOB_CONTRACT_READY")
        self.assertFalse(payload["execution_supported"])
        self.assertEqual(payload["requirements"]["inference_backend"], "hunyuan3d-2.1-shape")

    def test_human_readable_mode_is_available(self):
        code, stdout, stderr = self._run(
            ["shape", "plan", "--job", str(self.job_path)]
        )
        self.assertEqual(code, 0)
        self.assertIn("SHAPE_JOB_CONTRACT_READY", stdout)
        self.assertEqual(stderr, "")

    def test_malformed_json_returns_expected_error(self):
        self._write_job("{not-json")
        code, stdout, stderr = self._run(
            ["shape", "plan", "--job", str(self.job_path), "--json"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "INVALID")
        self.assertEqual(payload["classification"], "INVALID_JOB_DOCUMENT")
        self.assertNotIn("Traceback", stdout)

    def test_duplicate_key_returns_expected_error(self):
        self._write_job(
            '{"schema_version": 1, "job_id": "pharaoh-001", '
            '"job_id": "pharaoh-002", "reference_image": '
            '"references/pharaoh.png", "seed": 1, "remove_background": true}'
        )
        code, stdout, stderr = self._run(
            ["shape", "plan", "--job", str(self.job_path), "--json"]
        )
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "DUPLICATE_JOB_KEY")

    def test_missing_input_returns_policy_error(self):
        (self.input_dir / "references" / "pharaoh.png").unlink()
        code, stdout, stderr = self._run(
            ["shape", "plan", "--job", str(self.job_path), "--json"]
        )
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "INPUT_POLICY_REFUSAL")
        self.assertEqual(payload["status"], "INVALID")

    def test_wrong_image_signature_returns_policy_error(self):
        (self.input_dir / "references" / "pharaoh.png").write_bytes(JPEG_BYTES)
        code, stdout, stderr = self._run(
            ["shape", "plan", "--job", str(self.job_path), "--json"]
        )
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "INPUT_POLICY_REFUSAL")
        self.assertIn("signature", payload["message"])

    def test_traversal_path_returns_path_policy_error(self):
        self._write_job(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_id": "pharaoh-001",
                    "reference_image": "../outside.png",
                    "seed": 1,
                    "remove_background": True,
                }
            )
        )
        code, stdout, stderr = self._run(
            ["shape", "plan", "--job", str(self.job_path), "--json"]
        )
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "INPUT_PATH_POLICY_REFUSAL")

    def test_existing_output_target_returns_safe_path_error(self):
        (self.output_dir / "pharaoh-001").mkdir()
        code, stdout, stderr = self._run(
            ["shape", "plan", "--job", str(self.job_path), "--json"]
        )
        self.assertEqual(code, 3)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "SAFE_PATH_UNAVAILABLE")
        self.assertEqual(payload["status"], "ERROR")

    def test_invalid_cli_usage_exits_64(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["shape", "plan"])
        self.assertEqual(caught.exception.code, 64)

    def test_valid_plan_uses_no_network_or_writes(self):
        with (
            mock.patch("socket.create_connection", side_effect=AssertionError("network")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")),
            mock.patch("os.makedirs", side_effect=AssertionError("write")),
            mock.patch("os.mkdir", side_effect=AssertionError("write")),
        ):
            code, stdout, stderr = self._run(
                ["shape", "plan", "--job", str(self.job_path), "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "VALID")
        self.assertEqual(stderr, "")

    def test_unexpected_internal_error_is_sanitized(self):
        with mock.patch(
            "asset_pipeline.cli.build_plan",
            side_effect=RuntimeError("secret details"),
        ):
            code, stdout, stderr = self._run(
                ["shape", "plan", "--job", str(self.job_path), "--json"]
            )
        self.assertEqual(code, 70)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "INTERNAL_ERROR")
        self.assertNotIn("secret details", stdout)
        self.assertNotIn("Traceback", stderr)


    def test_deeply_nested_json_returns_invalid_document_without_traceback(self):
        self._write_job("[" * 10000 + "0" + "]" * 10000)
        code, stdout, stderr = self._run(
            ["shape", "plan", "--job", str(self.job_path), "--json"]
        )
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "INVALID")
        self.assertEqual(payload["classification"], "INVALID_JOB_DOCUMENT")
        self.assertIn("nesting depth", payload["message"])
        self.assertEqual(stderr, "")
        self.assertNotIn("Traceback", stdout)

    def test_unexpected_read_job_document_error_is_sanitized(self):
        with mock.patch(
            "asset_pipeline.cli.read_job_document",
            side_effect=RuntimeError("secret read details"),
        ):
            code, stdout, stderr = self._run(
                ["shape", "plan", "--job", str(self.job_path), "--json"]
            )
        self.assertEqual(code, 70)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "INTERNAL_ERROR")
        self.assertNotIn("secret read details", stdout)
        self.assertNotIn("Traceback", stderr)

if __name__ == "__main__":
    unittest.main()
