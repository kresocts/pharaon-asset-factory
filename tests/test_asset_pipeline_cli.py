"""Focused tests for the canonical shape plan and prepare command-line interface."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from asset_pipeline import cli
from docker import model_cache as model_cache_module


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 9


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.base = base
        self.input_dir = base / "input"
        self.output_dir = base / "output"
        self.workspace_dir = base / "workspace"
        for directory in (self.input_dir, self.output_dir, self.workspace_dir):
            directory.mkdir()
        self.model_cache_dir = base / "models"
        self.model_cache_dir.mkdir()
        self.env = {
            "INPUT_DIR": str(self.input_dir),
            "OUTPUT_DIR": str(self.output_dir),
            "WORKSPACE_DIR": str(self.workspace_dir),
            "MODEL_CACHE_DIR": str(self.model_cache_dir),
        }
        (self.input_dir / "references").mkdir(parents=True)
        (self.input_dir / "references" / "pharaoh.png").write_bytes(PNG_BYTES)
        self.job_path = base / "job.json"
        self.manifest_path = base / "manifest.json"
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

    def _model_contents(self):
        return {
            "config/model.json": b'{"shape_config": true}',
            "weights/model.safetensors": b"shape-weights",
            "auxiliary/tokenizer.json": b"auxiliary-data",
        }

    def _model_manifest_data(self, contents=None):
        revision = "a" * 40
        prefix = (
            f"https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/{revision}/"
            "hunyuan3d-dit-v2-1/"
        )
        contents = contents if contents is not None else self._model_contents()
        files = []
        for rel_path, data in contents.items():
            if "/" in rel_path and rel_path.endswith("model.json"):
                role = "shape-config"
            elif "/" in rel_path and rel_path.endswith("model.safetensors"):
                role = "shape-weights"
            else:
                role = "shape-auxiliary"
            files.append(
                {
                    "path": rel_path,
                    "url": prefix + rel_path,
                    "size": len(data),
                    "sha256": __import__("hashlib").sha256(data).hexdigest(),
                    "role": role,
                }
            )
        return {
            "schema_version": 1,
            "artifact_set": "hunyuan3d-2.1-shape",
            "revision": revision,
            "namespace": f"hunyuan3d-2.1-shape/{revision}",
            "description": "synthetic immutable model fixture",
            "files": files,
        }

    def _write_model_manifest(self, data=None):
        self.manifest_path.write_text(
            json.dumps(data if data is not None else self._model_manifest_data()),
            encoding="utf-8",
        )

    def _write_model_cache(self, contents=None):
        contents = contents if contents is not None else self._model_contents()
        revision = "a" * 40
        root = self.model_cache_dir / "hunyuan3d-2.1-shape" / revision
        for rel_path, data in contents.items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def _preflight_args(self, backend="hunyuan3d-2.1-shape", json_mode=True):
        args = [
            "shape",
            "preflight",
            "--job",
            str(self.job_path),
            "--backend",
            backend,
            "--model-manifest",
            str(self.manifest_path),
        ]
        if json_mode:
            args.append("--json")
        return args

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

    def test_human_readable_plan_mode_is_available(self):
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

    def test_valid_json_prepare_is_repeatable_and_byte_identical(self):
        args = [
            "shape",
            "prepare",
            "--job",
            str(self.job_path),
            "--backend",
            "hunyuan3d-2.1-shape",
            "--json",
        ]
        first_code, first_out, first_err = self._run(args)
        second_code, second_out, second_err = self._run(args)
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(first_out, second_out)
        self.assertEqual(first_err, "")
        self.assertEqual(second_err, "")
        payload = json.loads(first_out)
        self.assertEqual(payload["classification"], "SHAPE_EXECUTION_REQUEST_READY")
        self.assertTrue(payload["preparation_supported"])
        self.assertFalse(payload["execution_supported"])
        self.assertEqual(payload["backend"]["backend_id"], "hunyuan3d-2.1-shape")
        self.assertEqual(
            payload["execution_request"]["job_id"], "pharaoh-001"
        )
        self.assertEqual(len(payload["blockers"]), 3)

    def test_human_readable_prepare_mode_is_available(self):
        code, stdout, stderr = self._run(
            [
                "shape",
                "prepare",
                "--job",
                str(self.job_path),
                "--backend",
                "hunyuan3d-2.1-shape",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("SHAPE_EXECUTION_REQUEST_READY", stdout)
        self.assertEqual(stderr, "")

    def test_prepare_requires_backend(self):
        with self.assertRaises(SystemExit) as caught:
            cli.main(["shape", "prepare", "--job", str(self.job_path)])
        self.assertEqual(caught.exception.code, 64)

    def test_prepare_unknown_backend_returns_structured_error(self):
        code, stdout, stderr = self._run(
            [
                "shape",
                "prepare",
                "--job",
                str(self.job_path),
                "--backend",
                "unknown-shape",
                "--json",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "UNKNOWN_SHAPE_BACKEND")
        self.assertNotIn("Traceback", stdout)

    def test_prepare_malformed_case_variant_and_padded_backend_are_refused(self):
        for backend in ("Hunyuan3D-2.1-shape", " hunyuan3d-2.1-shape", "hunyuan3d-2.1-shape "):
            with self.subTest(backend=backend):
                code, stdout, stderr = self._run(
                    [
                        "shape",
                        "prepare",
                        "--job",
                        str(self.job_path),
                        "--backend",
                        backend,
                        "--json",
                    ]
                )
                self.assertEqual(code, 2)
                payload = json.loads(stdout)
                self.assertEqual(
                    payload["classification"], "MALFORMED_SHAPE_BACKEND"
                )
                self.assertEqual(stderr, "")

    def test_prepare_uses_no_network_writes_or_model_cache(self):
        with (
            mock.patch("socket.create_connection", side_effect=AssertionError("network")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")),
            mock.patch("os.makedirs", side_effect=AssertionError("write")),
            mock.patch("os.mkdir", side_effect=AssertionError("write")),
            mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("cache")),
            mock.patch("pathlib.Path.glob", side_effect=AssertionError("cache")),
        ):
            code, stdout, stderr = self._run(
                [
                    "shape",
                    "prepare",
                    "--job",
                    str(self.job_path),
                    "--backend",
                    "hunyuan3d-2.1-shape",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["classification"], "SHAPE_EXECUTION_REQUEST_READY")
        self.assertEqual(stderr, "")

    def test_prepare_unexpected_internal_error_is_sanitized(self):
        registry_mock = mock.Mock()
        registry_mock.resolve.side_effect = RuntimeError("secret backend failure")
        with mock.patch("asset_pipeline.cli.DEFAULT_REGISTRY", registry_mock):
            code, stdout, stderr = self._run(
                [
                    "shape",
                    "prepare",
                    "--job",
                    str(self.job_path),
                    "--backend",
                    "hunyuan3d-2.1-shape",
                    "--json",
                ]
            )
        self.assertEqual(code, 70)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "INTERNAL_ERROR")
        self.assertNotIn("secret backend failure", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_prepare_missing_input_preserves_policy_error(self):
        (self.input_dir / "references" / "pharaoh.png").unlink()
        code, stdout, stderr = self._run(
            [
                "shape",
                "prepare",
                "--job",
                str(self.job_path),
                "--backend",
                "hunyuan3d-2.1-shape",
                "--json",
            ]
        )
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "INPUT_POLICY_REFUSAL")

    def test_prepare_deeply_nested_json_preserves_contract_error(self):
        self._write_job("[" * 10000 + "0" + "]" * 10000)
        code, stdout, stderr = self._run(
            [
                "shape",
                "prepare",
                "--job",
                str(self.job_path),
                "--backend",
                "hunyuan3d-2.1-shape",
                "--json",
            ]
        )
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "INVALID_JOB_DOCUMENT")
        self.assertNotIn("Traceback", stdout)
        self.assertEqual(stderr, "")


    def test_prepare_fresh_process_does_not_import_heavy_runtime_modules(self):
        script = r"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    input_dir = base / "input"
    output_dir = base / "output"
    workspace_dir = base / "workspace"
    for item in (input_dir, output_dir, workspace_dir):
        item.mkdir()
    (input_dir / "references").mkdir(parents=True)
    (input_dir / "references" / "pharaoh.png").write_bytes(PNG_BYTES)
    job = base / "job.json"
    job.write_text(
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
    os.environ["INPUT_DIR"] = str(input_dir)
    os.environ["OUTPUT_DIR"] = str(output_dir)
    os.environ["WORKSPACE_DIR"] = str(workspace_dir)

    from asset_pipeline.cli import main

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "shape",
                "prepare",
                "--job",
                str(job),
                "--backend",
                "hunyuan3d-2.1-shape",
                "--json",
            ]
        )
    payload = json.loads(stdout.getvalue())
    forbidden_roots = (
        "torch",
        "torchvision",
        "torchaudio",
        "diffusers",
        "transformers",
        "accelerate",
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
    print(
        json.dumps(
            {
                "exit_code": exit_code,
                "classification": payload.get("classification"),
                "forbidden_modules": forbidden_modules,
            }
        )
    )
    if exit_code != 0 or forbidden_modules:
        raise SystemExit(1)
"""
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(
            payload["classification"], "SHAPE_EXECUTION_REQUEST_READY"
        )
        self.assertEqual(payload["forbidden_modules"], [])

    def test_valid_preflight_is_successful_and_deterministic_in_process(self):
        self._write_model_manifest()
        self._write_model_cache()
        args = self._preflight_args()
        first_code, first_out, first_err = self._run(args)
        second_code, second_out, second_err = self._run(args)
        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(first_out, second_out)
        self.assertEqual(first_err, "")
        self.assertEqual(second_err, "")
        payload = json.loads(first_out)
        self.assertEqual(payload["classification"], "SHAPE_MODEL_PREFLIGHT_READY")
        self.assertTrue(payload["model_binding_supported"])
        self.assertTrue(payload["model_cache_verified"])
        self.assertFalse(payload["execution_supported"])
        self.assertEqual(
            payload["cache_verification"]["state_counts"]["VERIFIED"], 3
        )
        self.assertEqual(payload["cache_verification"]["required_bytes"], 0)
        self.assertTrue(payload["cache_verification"]["fully_cached"])
        self.assertEqual(
            [blocker["code"] for blocker in payload["blockers"]],
            ["GPU_EXECUTION_NOT_IMPLEMENTED"],
        )
        self.assertNotIn("url", json.dumps(payload["model_binding"]))

    def test_human_readable_preflight_is_available(self):
        self._write_model_manifest()
        self._write_model_cache()
        code, stdout, stderr = self._run(self._preflight_args(json_mode=False))
        self.assertEqual(code, 0)
        self.assertIn("SHAPE_MODEL_PREFLIGHT_READY", stdout)
        self.assertEqual(stderr, "")

    def test_preflight_requires_all_arguments(self):
        for args in (
            ["shape", "preflight", "--job", str(self.job_path), "--backend", "hunyuan3d-2.1-shape"],
            ["shape", "preflight", "--job", str(self.job_path), "--model-manifest", str(self.manifest_path)],
            ["shape", "preflight", "--backend", "hunyuan3d-2.1-shape", "--model-manifest", str(self.manifest_path)],
        ):
            with self.subTest(args=args):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(args)
                self.assertEqual(caught.exception.code, 64)

    def test_preflight_unknown_malformed_case_variant_and_padded_backend(self):
        self._write_model_manifest()
        for backend in ("unknown-shape", "Hunyuan3D-2.1-shape", " hunyuan3d-2.1-shape", "hunyuan3d-2.1-shape "):
            with self.subTest(backend=backend):
                code, stdout, stderr = self._run(
                    self._preflight_args(backend=backend)
                )
                self.assertEqual(code, 2)
                payload = json.loads(stdout)
                self.assertIn(
                    payload["classification"],
                    {"UNKNOWN_SHAPE_BACKEND", "MALFORMED_SHAPE_BACKEND"},
                )
                self.assertNotIn("Traceback", stdout)
                self.assertEqual(stderr, "")

    def test_preflight_missing_manifest(self):
        code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 3)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "MODEL_MANIFEST_INVALID")
        self.assertNotIn("Traceback", stdout)
        self.assertEqual(stderr, "")

    def test_preflight_invalid_manifest(self):
        self.manifest_path.write_text("{}", encoding="utf-8")
        code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 3)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "MODEL_MANIFEST_INVALID")
        self.assertNotIn("Traceback", stdout)
        self.assertEqual(stderr, "")

    def test_preflight_binding_mismatch(self):
        data = self._model_manifest_data()
        data["artifact_set"] = "wrong-set"
        self._write_model_manifest(data)
        code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 2)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "MODEL_BINDING_REFUSAL")
        self.assertNotIn("Traceback", stdout)
        self.assertEqual(stderr, "")

    def test_preflight_cache_failures_return_not_verified(self):
        # Absent cache.
        self._write_model_manifest()
        code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 4)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "MODEL_CACHE_NOT_VERIFIED")
        self.assertFalse(payload["message"].startswith("secret"))
        self.assertEqual(stderr, "")

        # Partial cache.
        contents = self._model_contents()
        root = self.model_cache_dir / "hunyuan3d-2.1-shape" / ("a" * 40)
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "model.json").with_name("_acq-a1.part").write_bytes(b"partial")
        code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 4)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "MODEL_CACHE_NOT_VERIFIED")

        # Corrupted cache: same size, wrong hash.
        (root / "config").mkdir(parents=True, exist_ok=True)
        target = root / "config" / "model.json"
        target.write_bytes(b"X" * len(contents["config/model.json"]))
        code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 4)
        payload = json.loads(stdout)
        self.assertEqual(payload["classification"], "MODEL_CACHE_NOT_VERIFIED")

    def test_preflight_success_retains_only_execution_blocker(self):
        self._write_model_manifest()
        self._write_model_cache()
        code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(
            payload["classification"], "SHAPE_MODEL_PREFLIGHT_READY"
        )
        self.assertTrue(payload["model_cache_verified"])
        self.assertFalse(payload["execution_supported"])
        self.assertEqual(
            [blocker["code"] for blocker in payload["blockers"]],
            ["GPU_EXECUTION_NOT_IMPLEMENTED"],
        )

    def test_preflight_uses_no_network_writes_or_cache_repair(self):
        self._write_model_manifest()
        self._write_model_cache()
        with (
            mock.patch("socket.create_connection", side_effect=AssertionError("network")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")),
            mock.patch("os.replace", side_effect=AssertionError("write")),
            mock.patch("os.mkdir", side_effect=AssertionError("write")),
            mock.patch("os.makedirs", side_effect=AssertionError("write")),
        ):
            code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["classification"], "SHAPE_MODEL_PREFLIGHT_READY")
        self.assertEqual(stderr, "")

    def test_preflight_unexpected_boundary_errors_are_sanitized(self):
        self._write_model_manifest()
        self._write_model_cache()
        boundaries = [
            ("asset_pipeline.cli.read_job_document", RuntimeError("job secret")),
            ("asset_pipeline.cli.load_runtime_roots", RuntimeError("root secret")),
            ("asset_pipeline.cli.build_plan", RuntimeError("plan secret")),
            ("asset_pipeline.backends.ShapeBackendRegistry.resolve", RuntimeError("backend secret")),
            ("asset_pipeline.cli.bind_parsed_model_manifest", RuntimeError("binding secret")),
            ("asset_pipeline.cli.model_cache.verify_parsed_manifest_cache", RuntimeError("cache secret")),
        ]
        for target, error in boundaries:
            with self.subTest(target=target):
                with mock.patch(target, side_effect=error):
                    code, stdout, stderr = self._run(self._preflight_args())
                self.assertEqual(code, 70)
                payload = json.loads(stdout)
                self.assertEqual(payload["classification"], "INTERNAL_ERROR")
                self.assertNotIn("secret", stdout)
                self.assertNotIn("Traceback", stderr)

    def test_preflight_fresh_process_does_not_import_heavy_runtime_modules(self):
        script = r"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

PNG_BYTES = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(8)
REVISION = "a" * 40
PREFIX = f"https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/{REVISION}/hunyuan3d-dit-v2-1/"
FILES = {
    "config/model.json": (b'{"shape_config": true}', "shape-config"),
    "weights/model.safetensors": (b"shape-weights", "shape-weights"),
    "auxiliary/tokenizer.json": (b"auxiliary-data", "shape-auxiliary"),
}

with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    input_dir = base / "input"
    output_dir = base / "output"
    workspace_dir = base / "workspace"
    model_dir = base / "models"
    for item in (input_dir, output_dir, workspace_dir, model_dir):
        item.mkdir()
    (input_dir / "references").mkdir(parents=True)
    (input_dir / "references" / "pharaoh.png").write_bytes(PNG_BYTES)
    job = base / "job.json"
    job.write_text(
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
    manifest_files = []
    cache_root = model_dir / "hunyuan3d-2.1-shape" / REVISION
    for rel_path, (data, role) in FILES.items():
        target = cache_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest_files.append(
            {
                "path": rel_path,
                "url": PREFIX + rel_path,
                "size": len(data),
                "sha256": __import__("hashlib").sha256(data).hexdigest(),
                "role": role,
            }
        )
    manifest = base / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_set": "hunyuan3d-2.1-shape",
                "revision": REVISION,
                "namespace": f"hunyuan3d-2.1-shape/{REVISION}",
                "files": manifest_files,
            }
        ),
        encoding="utf-8",
    )
    os.environ["INPUT_DIR"] = str(input_dir)
    os.environ["OUTPUT_DIR"] = str(output_dir)
    os.environ["WORKSPACE_DIR"] = str(workspace_dir)
    os.environ["MODEL_CACHE_DIR"] = str(model_dir)

    from asset_pipeline.cli import main

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "shape",
                "preflight",
                "--job",
                str(job),
                "--backend",
                "hunyuan3d-2.1-shape",
                "--model-manifest",
                str(manifest),
                "--json",
            ]
        )
    payload = json.loads(stdout.getvalue())
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
    print(
        json.dumps(
            {
                "exit_code": exit_code,
                "classification": payload.get("classification"),
                "forbidden_modules": forbidden_modules,
            }
        )
    )
    if exit_code != 0 or forbidden_modules:
        raise SystemExit(1)
"""
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["classification"], "SHAPE_MODEL_PREFLIGHT_READY")
        self.assertEqual(payload["forbidden_modules"], [])

    def test_preflight_fresh_process_output_is_byte_identical(self):
        self._write_model_manifest()
        self._write_model_cache()
        script = r"""
import json
import os
import subprocess
import sys
from pathlib import Path
repo_root = Path(os.environ["REPO_ROOT"])
args = [
    sys.executable,
    "-m",
    "asset_pipeline.cli",
    "shape",
    "preflight",
    "--job",
    os.environ["JOB"],
    "--backend",
    "hunyuan3d-2.1-shape",
    "--model-manifest",
    os.environ["MANIFEST"],
    "--json",
]
env = os.environ.copy()
env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
first = subprocess.run(args, cwd=repo_root, env=env, text=True, capture_output=True, check=False)
second = subprocess.run(args, cwd=repo_root, env=env, text=True, capture_output=True, check=False)
print(
    json.dumps(
        {
            "first_code": first.returncode,
            "second_code": second.returncode,
            "identical": first.stdout == second.stdout,
            "first_stdout": first.stdout,
            "stderr": first.stderr + second.stderr,
        }
    )
)
if first.returncode != 0 or second.returncode != 0 or first.stdout != second.stdout:
    raise SystemExit(1)
"""
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.update(self.env)
        env["REPO_ROOT"] = str(repo_root)
        env["JOB"] = str(self.job_path)
        env["MANIFEST"] = str(self.manifest_path)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["first_code"], 0)
        self.assertEqual(payload["second_code"], 0)
        self.assertTrue(payload["identical"])


    def test_successful_preflight_identity_fields_agree(self):
        self._write_model_manifest()
        self._write_model_cache()
        code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        binding = payload["model_binding"]
        cache = payload["cache_verification"]
        self.assertEqual(binding["plan_id"], cache["plan_id"])
        self.assertEqual(binding["artifact_set"], cache["artifact_set"])
        self.assertEqual(binding["revision"], cache["revision"])
        self.assertEqual(binding["namespace"], cache["namespace"])
        self.assertEqual(binding["file_count"], cache["file_count"])
        self.assertEqual(
            binding["total_expected_bytes"], cache["total_expected_bytes"]
        )

    def test_preflight_parses_manifest_exactly_once_and_uses_parsed_identity(self):
        self._write_model_manifest()
        self._write_model_cache()
        data_a = self._model_manifest_data()
        data_b = self._model_manifest_data()
        revision_b = "b" * 40
        data_b["revision"] = revision_b
        data_b["namespace"] = f"hunyuan3d-2.1-shape/{revision_b}"
        prefix_b = (
            f"https://huggingface.co/tencent/Hunyuan3D-2.1/resolve/{revision_b}/"
            "hunyuan3d-dit-v2-1/"
        )
        for file in data_b["files"]:
            file["url"] = prefix_b + file["path"]

        plan_id_a = model_cache_module.manifest_plan_id(data_a)
        plan_id_b = model_cache_module.manifest_plan_id(data_b)
        original_parse = cli.model_cache.parse_manifest
        calls = []

        def parse_once(path):
            calls.append(str(path))
            parsed = original_parse(path)
            Path(path).write_text(json.dumps(data_b), encoding="utf-8")
            return parsed

        with mock.patch.object(
            cli.model_cache, "parse_manifest", side_effect=parse_once
        ) as parse_mock:
            code, stdout, stderr = self._run(self._preflight_args())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(parse_mock.call_count, 1)
        self.assertEqual(len(calls), 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["model_binding"]["plan_id"], plan_id_a)
        self.assertEqual(payload["cache_verification"]["plan_id"], plan_id_a)
        self.assertNotEqual(payload["model_binding"]["plan_id"], plan_id_b)
        self.assertEqual(
            payload["model_binding"]["revision"], "a" * 40
        )
        self.assertEqual(
            payload["cache_verification"]["revision"], "a" * 40
        )


if __name__ == "__main__":
    unittest.main()
