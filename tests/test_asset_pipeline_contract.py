"""Focused tests for the strict shape-job document contract."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from asset_pipeline import contract


class ContractTests(unittest.TestCase):
    def _write_job(self, text: str, directory: str) -> Path:
        path = Path(directory) / "job.json"
        path.write_text(text, encoding="utf-8")
        return path

    def _read(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            return contract.read_job_document(self._write_job(text, directory))

    def test_valid_document_is_normalized(self):
        document = self._read(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_id": "pharaoh-001",
                    "reference_image": "references/pharaoh.png",
                    "seed": 12345,
                    "remove_background": True,
                }
            )
        )
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["job_id"], "pharaoh-001")
        self.assertEqual(document["reference_image"], "references/pharaoh.png")
        self.assertEqual(document["seed"], 12345)
        self.assertIs(document["remove_background"], True)

    def test_missing_field_is_rejected(self):
        with self.assertRaises(contract.InvalidJobFieldError):
            self._read(
                json.dumps(
                    {
                        "schema_version": 1,
                        "job_id": "pharaoh-001",
                        "reference_image": "references/pharaoh.png",
                        "seed": 12345,
                    }
                )
            )

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(contract.InvalidJobFieldError):
            self._read(
                json.dumps(
                    {
                        "schema_version": 1,
                        "job_id": "pharaoh-001",
                        "reference_image": "references/pharaoh.png",
                        "seed": 12345,
                        "remove_background": True,
                        "prompt": "extra",
                    }
                )
            )

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(contract.DuplicateKeyError):
            self._read(
                '{"schema_version": 1, "job_id": "pharaoh-001", '
                '"job_id": "pharaoh-002", "reference_image": "a.png", '
                '"seed": 1, "remove_background": true}'
            )

    def test_wrong_scalar_types_are_rejected(self):
        cases = [
            {"schema_version": "1"},
            {"job_id": 12},
            {"reference_image": 123},
            {"seed": True},
            {"remove_background": "true"},
        ]
        for replacement in cases:
            base = {
                "schema_version": 1,
                "job_id": "pharaoh-001",
                "reference_image": "references/pharaoh.png",
                "seed": 1,
                "remove_background": True,
            }
            base.update(replacement)
            with self.subTest(replacement=replacement):
                with self.assertRaises(contract.ContractError):
                    self._read(json.dumps(base))

    def test_seed_range_is_enforced(self):
        for seed in (-1, 4294967296):
            base = {
                "schema_version": 1,
                "job_id": "pharaoh-001",
                "reference_image": "references/pharaoh.png",
                "seed": seed,
                "remove_background": True,
            }
            with self.subTest(seed=seed):
                with self.assertRaises(contract.InvalidJobFieldError):
                    self._read(json.dumps(base))

    def test_schema_version_must_equal_one(self):
        base = {
            "schema_version": 2,
            "job_id": "pharaoh-001",
            "reference_image": "references/pharaoh.png",
            "seed": 1,
            "remove_background": True,
        }
        with self.assertRaises(contract.InvalidJobFieldError):
            self._read(json.dumps(base))

    def test_job_id_rules_are_enforced(self):
        invalid_ids = [
            "",
            "Pharaoh",
            "-start",
            "end-",
            "a" * 65,
            "has space",
        ]
        for job_id in invalid_ids:
            base = {
                "schema_version": 1,
                "job_id": job_id,
                "reference_image": "references/pharaoh.png",
                "seed": 1,
                "remove_background": True,
            }
            with self.subTest(job_id=job_id):
                with self.assertRaises(contract.InvalidJobFieldError):
                    self._read(json.dumps(base))

    def test_reference_image_path_policy_is_enforced(self):
        invalid_paths = [
            "",
            "/absolute.png",
            "C:\\image.png",
            "references\\pharaoh.png",
            "references/../pharaoh.png",
            "references/./pharaoh.png",
            "references//pharaoh.png",
            "references/%2e%2e/pharaoh.png",
            "references/\x00pharaoh.png",
        ]
        for reference_image in invalid_paths:
            base = {
                "schema_version": 1,
                "job_id": "pharaoh-001",
                "reference_image": reference_image,
                "seed": 1,
                "remove_background": True,
            }
            with self.subTest(reference_image=reference_image):
                with self.assertRaises(contract.InputPathPolicyError):
                    self._read(json.dumps(base))

    def test_non_json_and_trailing_data_are_rejected(self):
        for text in ("not-json", '{"schema_version": 1} trailing'):
            with self.subTest(text=text):
                with self.assertRaises(contract.ContractError):
                    self._read(text)

    def test_non_finite_json_constants_are_rejected(self):
        text = (
            '{"schema_version": 1, "job_id": "pharaoh-001", '
            '"reference_image": "references/pharaoh.png", "seed": NaN, '
            '"remove_background": true}'
        )
        with self.assertRaises(contract.JobDocumentDecodeError):
            self._read(text)

    def test_job_file_size_is_capped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_bytes(b" " * (contract.MAX_JOB_BYTES + 1))
            with self.assertRaises(contract.JobDocumentTooLargeError):
                contract.read_job_document(path)

    def test_missing_job_file_is_expected_failure(self):
        with self.assertRaises(contract.JobFileUnavailableError):
            contract.read_job_document("missing-job.json")


    def test_deeply_nested_json_below_limit_raises_decode_error(self):
        payload = "[" * 10000 + "0" + "]" * 10000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.json"
            path.write_text(payload, encoding="utf-8")
            with self.assertRaises(contract.JobDocumentDecodeError) as caught:
                contract.read_job_document(path)
        self.assertIn("nesting depth", str(caught.exception))

if __name__ == "__main__":
    unittest.main()
