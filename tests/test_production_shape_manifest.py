"""Focused tests for the T-0025 production Hunyuan shape provenance."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asset_pipeline import cli, models
from docker import model_cache
from validation import validate_production_shape_manifest as validator


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DIR = ROOT / "model-manifests" / "production"
MANIFEST_PATH = PRODUCTION_DIR / "hunyuan3d-2.1-shape.json"
PROVENANCE_PATH = PRODUCTION_DIR / "hunyuan3d-2.1-shape.provenance.json"
REVISION = "0b94677654c57bb9a6b6845cd7b704ccf551d327"
SOURCE_REVISION = "82920d643c0dc2f7bfd7255f45f62d386edfe60c"
PLAN_ID = "5b6005ace3fa63b9719da75d1fc10a0793c41718e4f15666c8e527e16ff41cd8"
TOTAL_BYTES = 7366391846
PNG_BYTES = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(8)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot(directory: Path):
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            entries.append((str(path.relative_to(directory)).replace("\\", "/"), path.stat().st_size))
    return entries


class ProductionShapeManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.input_dir = base / "input"
        self.output_dir = base / "output"
        self.workspace_dir = base / "workspace"
        self.model_cache_dir = base / "models"
        for directory in (self.input_dir, self.output_dir, self.workspace_dir, self.model_cache_dir):
            directory.mkdir()
        (self.input_dir / "references").mkdir(parents=True)
        (self.input_dir / "references" / "pharaoh.png").write_bytes(PNG_BYTES)
        self.job_path = base / "job.json"
        self.job_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "job_id": "pharaoh-001",
                    "reference_image": "references/pharaoh.png",
                    "seed": 12345,
                    "remove_background": True,
                }
            ),
            encoding="utf-8",
        )

    def _env(self, *, include_pythonpath=True):
        env = os.environ.copy()
        env["INPUT_DIR"] = str(self.input_dir)
        env["OUTPUT_DIR"] = str(self.output_dir)
        env["WORKSPACE_DIR"] = str(self.workspace_dir)
        env["MODEL_CACHE_DIR"] = str(self.model_cache_dir)
        if include_pythonpath:
            env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def _run_models(self, command):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "docker" / "model_cache.py"),
                command,
                "--manifest",
                str(MANIFEST_PATH),
                "--json",
            ],
            cwd=ROOT,
            env=self._env(include_pythonpath=False),
            text=True,
            capture_output=True,
            check=False,
        )

    def _run_shape_preflight(self):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "asset_pipeline.cli",
                "shape",
                "preflight",
                "--job",
                str(self.job_path),
                "--backend",
                "hunyuan3d-2.1-shape",
                "--model-manifest",
                str(MANIFEST_PATH),
                "--json",
            ],
            cwd=ROOT,
            env=self._env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def _copy_production(self):
        temporary = Path(tempfile.mkdtemp(prefix="t0025-"))
        self.addCleanup(shutil.rmtree, temporary, ignore_errors=True)
        production = temporary / "model-manifests" / "production"
        production.mkdir(parents=True)
        shutil.copy2(MANIFEST_PATH, production / MANIFEST_PATH.name)
        shutil.copy2(PROVENANCE_PATH, production / PROVENANCE_PATH.name)
        docker_dir = temporary / "docker"
        docker_dir.mkdir()
        shutil.copy2(ROOT / "docker" / "Dockerfile", docker_dir / "Dockerfile")
        return temporary

    def _assert_validator_rejects(self, mutate_manifest=None, mutate_provenance=None):
        temporary = self._copy_production()
        manifest_path = temporary / "model-manifests" / "production" / "hunyuan3d-2.1-shape.json"
        provenance_path = temporary / "model-manifests" / "production" / "hunyuan3d-2.1-shape.provenance.json"
        if mutate_manifest is not None:
            data = _load(manifest_path)
            mutate_manifest(data)
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
        if mutate_provenance is not None:
            data = _load(provenance_path)
            mutate_provenance(data)
            provenance_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(validator.ValidationFailure):
            validator.validate(temporary)

    def test_manifest_parses_and_binds(self):
        manifest = model_cache.parse_manifest(MANIFEST_PATH)
        binding = models.bind_parsed_model_manifest(
            manifest,
            backend_id=models.CANONICAL_ARTIFACT_SET,
            cache_root=self.model_cache_dir,
        )
        self.assertEqual(binding.backend_id, models.CANONICAL_ARTIFACT_SET)
        self.assertEqual(binding.revision, REVISION)
        self.assertEqual(binding.file_count, 2)
        self.assertEqual(binding.total_expected_bytes, TOTAL_BYTES)
        self.assertEqual(binding.plan_id, PLAN_ID)

    def test_exact_immutable_revision_shape_and_namespace(self):
        manifest = _load(MANIFEST_PATH)
        self.assertRegex(manifest["revision"], r"^[0-9a-f]{40}$")
        self.assertEqual(manifest["revision"], REVISION)
        self.assertEqual(manifest["namespace"], f"hunyuan3d-2.1-shape/{REVISION}")

    def test_exact_approved_file_set_and_role_mapping(self):
        manifest = _load(MANIFEST_PATH)
        self.assertEqual([file["path"] for file in manifest["files"]], ["config.yaml", "model.fp16.ckpt"])
        self.assertEqual([file["role"] for file in manifest["files"]], ["shape-config", "shape-weights"])

    def test_exact_sizes_and_lowercase_sha256_match_provenance(self):
        manifest = _load(MANIFEST_PATH)
        provenance = _load(PROVENANCE_PATH)
        by_path = {entry["path"]: entry for entry in provenance["files"]}
        for file in manifest["files"]:
            record = by_path[file["path"]]
            self.assertEqual(file["size"], record["size"])
            self.assertRegex(file["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(file["sha256"], record["sha256"])

    def test_canonical_plan_id_file_count_and_total_bytes_match(self):
        manifest = model_cache.parse_manifest(MANIFEST_PATH)
        provenance = _load(PROVENANCE_PATH)
        self.assertEqual(model_cache.manifest_plan_id(manifest), PLAN_ID)
        self.assertEqual(provenance["plan_id"], PLAN_ID)
        self.assertEqual(provenance["file_count"], 2)
        self.assertEqual(provenance["total_expected_bytes"], TOTAL_BYTES)

    def test_no_mutable_reference_query_fragment_credentials_mirror_or_signed_url(self):
        manifest = _load(MANIFEST_PATH)
        forbidden = ("main", "master", "latest", "head")
        for file in manifest["files"]:
            url = file["url"]
            self.assertTrue(url.startswith("https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/"))
            self.assertNotIn("?", url)
            self.assertNotIn("#", url)
            self.assertNotIn("@", url)
            self.assertNotIn("cdn", url.lower())
            self.assertNotIn("mirror", url.lower())
            self.assertNotIn("signed", url.lower())
            self.assertFalse(any(word in url.lower() for word in forbidden))

    def test_provenance_is_defensive_and_matches_manifest(self):
        provenance = _load(PROVENANCE_PATH)
        self.assertEqual(provenance["schema_version"], 1)
        self.assertEqual(provenance["artifact_set"], "hunyuan3d-2.1-shape")
        self.assertEqual(provenance["model_revision"], REVISION)
        self.assertEqual(provenance["plan_id"], PLAN_ID)
        self.assertEqual(provenance["file_count"], 2)
        self.assertEqual(provenance["total_expected_bytes"], TOTAL_BYTES)
        self.assertTrue(provenance["operator_review_required"])
        self.assertFalse(provenance["acquisition_authorized"])
        self.assertFalse(provenance["legal_conclusion"])
        self.assertEqual(provenance["network_session_summary"]["request_count"], 9)
        self.assertEqual(provenance["network_session_summary"]["total_response_body_bytes"], 92313)
        manifest = _load(MANIFEST_PATH)
        self.assertEqual(provenance["model_revision"], manifest["revision"])
        self.assertEqual(len(provenance["files"]), len(manifest["files"]))

    def test_source_revision_matches_dockerfile(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        match = re.search(r"^ARG HUNYUAN_COMMIT=([0-9a-f]{40})$", dockerfile, flags=re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), SOURCE_REVISION)
        self.assertEqual(_load(PROVENANCE_PATH)["source_code_revision"], SOURCE_REVISION)

    def test_loader_references_have_immutable_urls_hashes_symbols_and_line_ranges(self):
        provenance = _load(PROVENANCE_PATH)
        by_path = {ref["path"]: ref for ref in provenance["source_references"]}
        self.assertEqual(set(by_path), {"model_worker.py", "hy3dshape/hy3dshape/pipelines.py", "hy3dshape/hy3dshape/utils/utils.py"})
        for path, ref in by_path.items():
            self.assertTrue(ref["url"].startswith(f"https://raw.githubusercontent.com/Tencent-Hunyuan/Hunyuan3D-2.1/{SOURCE_REVISION}/"))
            self.assertRegex(ref["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(ref["function"])
            self.assertTrue(ref["line_ranges"])
            for item in ref["line_ranges"]:
                self.assertIsInstance(item["start"], int)
                self.assertIsInstance(item["end"], int)
                self.assertGreater(item["start"], 0)
                self.assertGreaterEqual(item["end"], item["start"])
                self.assertTrue(item["claim"])

    def test_validator_rejects_old_flat_model_worker_line_list(self):
        def mutate(data):
            for ref in data["source_references"]:
                if ref.get("path") == "model_worker.py":
                    ref["lines"] = [61, 62, 99]
        self._assert_validator_rejects(mutate_provenance=mutate)

    def test_validator_rejects_missing_or_blocked_loader_evidence(self):
        def remove_pipelines(data):
            data["source_references"] = [
                ref for ref in data["source_references"]
                if ref.get("path") != "hy3dshape/hy3dshape/pipelines.py"
            ]
        self._assert_validator_rejects(mutate_provenance=remove_pipelines)

        def block_utils(data):
            for ref in data["source_references"]:
                if ref.get("path") == "hy3dshape/hy3dshape/utils/utils.py":
                    ref["sha256"] = "BLOCKED"
        self._assert_validator_rejects(mutate_provenance=block_utils)

    def test_validator_rejects_mutable_or_wrong_source_revision(self):
        def mutate_url(data):
            for ref in data["source_references"]:
                if ref.get("path") == "hy3dshape/hy3dshape/pipelines.py":
                    ref["url"] = ref["url"].replace(SOURCE_REVISION, "main")
        self._assert_validator_rejects(mutate_provenance=mutate_url)

        def mutate_revision(data):
            data["source_code_revision"] = "0" * 40
            data["source_revision"] = "0" * 40
        self._assert_validator_rejects(mutate_provenance=mutate_revision)

    def test_request_accounting_is_exact_and_internally_summed(self):
        provenance = _load(PROVENANCE_PATH)
        summary = provenance["network_session_summary"]
        self.assertEqual(summary["request_count"], len(summary["requests"]))
        self.assertEqual(summary["total_response_body_bytes"], sum(item["body_bytes"] for item in summary["requests"]))
        self.assertFalse(summary["weight_body_requested"])
        for request in summary["requests"]:
            self.assertIsInstance(request["body_bytes"], int)
            self.assertGreaterEqual(request["body_bytes"], 0)
            self.assertRegex(request["body_sha256"], r"^[0-9a-f]{64}$")
            self.assertIsInstance(request["status"], int)
            self.assertIsNone(request["redirect_location"])

    def test_validator_rejects_bad_request_accounting(self):
        def unknown_bytes(data):
            del data["network_session_summary"]["requests"][0]["body_bytes"]
        self._assert_validator_rejects(mutate_provenance=unknown_bytes)

        def placeholder_total(data):
            data["network_session_summary"]["total_response_body_bytes"] = "BLOCKED"
        self._assert_validator_rejects(mutate_provenance=placeholder_total)

        def omitted_redirect(data):
            data["network_session_summary"]["requests"][0]["redirect_location"] = "https://huggingface.co/not-logged"
        self._assert_validator_rejects(mutate_provenance=omitted_redirect)

        def too_many_requests(data):
            data["network_session_summary"]["request_count"] = 11
        self._assert_validator_rejects(mutate_provenance=too_many_requests)

        def too_many_bytes(data):
            data["network_session_summary"]["total_response_body_bytes"] = 2 * 1024 * 1024 + 1
        self._assert_validator_rejects(mutate_provenance=too_many_bytes)

    def test_validator_rejects_weight_body_request_urls(self):
        def mutate(data):
            data["network_session_summary"]["requests"][0]["url"] = (
                "https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/"
                "0b94677654c57bb9a6b6845cd7b704ccf551d327/hunyuan3d-dit-v2-1/model.fp16.ckpt"
            )
        self._assert_validator_rejects(mutate_provenance=mutate)

    def test_license_status_flags_and_metadata_only_files(self):
        provenance = _load(PROVENANCE_PATH)
        self.assertTrue(provenance["operator_review_required"])
        self.assertFalse(provenance["acquisition_authorized"])
        self.assertFalse(provenance["legal_conclusion"])
        license_data = provenance["license"]
        self.assertEqual(license_data["license_name"], "tencent-hunyuan-community")
        self.assertFalse(license_data["gated"])
        self.assertFalse(license_data["private"])
        self.assertFalse(license_data["disabled"])
        self.assertTrue(license_data["extra_gated_eu_disallowed"])
        self.assertTrue(license_data["operator_review_required"])
        self.assertFalse(license_data["acquisition_authorized"])
        self.assertFalse(license_data["legal_conclusion"])
        for entry in license_data["files"]:
            self.assertFalse(entry["direct_body_hash"])
            self.assertTrue(entry["url"].startswith("https://huggingface.co/"))

    def test_manifest_directory_contains_no_model_payload_or_large_file(self):
        validator._validate_no_model_payload(ROOT)

    def test_models_plan_offline_reports_reviewed_total_bytes(self):
        result = self._run_models("plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["file_count"], 2)
        self.assertEqual(report["bytes"]["total_expected"], TOTAL_BYTES)
        self.assertEqual(report["network"]["requests_attempted"], 0)
        self.assertEqual(report["plan_id"], PLAN_ID)

    def test_models_status_and_verify_empty_cache_report_not_verified_without_writes(self):
        before = _snapshot(self.model_cache_dir)
        status = self._run_models("status")
        after_status = _snapshot(self.model_cache_dir)
        self.assertEqual(status.returncode, 0, status.stderr)
        status_report = json.loads(status.stdout)
        self.assertTrue(status_report["success"])
        self.assertFalse(status_report["fully_cached"])
        self.assertEqual(status_report["file_counts"]["ABSENT"], 2)
        self.assertEqual(after_status, before)

        verify = self._run_models("verify")
        after_verify = _snapshot(self.model_cache_dir)
        self.assertEqual(verify.returncode, 4, verify.stdout + verify.stderr)
        verify_report = json.loads(verify.stdout)
        self.assertFalse(verify_report["success"])
        self.assertEqual(verify_report["classification"], "NOT_VERIFIED")
        self.assertEqual(verify_report["network"]["requests_attempted"], 0)
        self.assertEqual(after_verify, before)

    def test_shape_preflight_empty_cache_returns_model_cache_not_verified(self):
        before_output = _snapshot(self.output_dir)
        before_workspace = _snapshot(self.workspace_dir)
        before_cache = _snapshot(self.model_cache_dir)
        result = self._run_shape_preflight()
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["classification"], "MODEL_CACHE_NOT_VERIFIED")
        self.assertEqual(_snapshot(self.output_dir), before_output)
        self.assertEqual(_snapshot(self.workspace_dir), before_workspace)
        self.assertEqual(_snapshot(self.model_cache_dir), before_cache)

    def test_production_paths_do_not_open_network_or_import_heavy_modules(self):
        forbidden_roots = (
            "torch",
            "torchvision",
            "torchaudio",
            "diffusers",
            "transformers",
            "accelerate",
            "huggingface_hub",
            "hunyuan3d",
            "hy3dgen",
            "cuda",
            "cupy",
        )
        forbidden_modules = [
            name
            for name in sys.modules
            if any(name == root or name.startswith(root + ".") for root in forbidden_roots)
        ]
        self.assertEqual(forbidden_modules, [])
        with (
            mock.patch("socket.create_connection", side_effect=AssertionError("network")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")),
        ):
            manifest = model_cache.parse_manifest(MANIFEST_PATH)
            models.bind_parsed_model_manifest(
                manifest,
                backend_id=models.CANONICAL_ARTIFACT_SET,
                cache_root=self.model_cache_dir,
            )
            validator.validate(ROOT)

    def test_validator_is_deterministic_and_successful(self):
        env = self._env()
        args = [sys.executable, str(ROOT / "validation" / "validate_production_shape_manifest.py")]
        first = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        second = subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.stdout.strip(), "PRODUCTION_SHAPE_MANIFEST_VALID")


if __name__ == "__main__":
    unittest.main()
