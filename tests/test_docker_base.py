import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
HEALTH_PATH = ROOT / "docker" / "health.py"


def _load_health_module():
    spec = importlib.util.spec_from_file_location("docker_health", HEALTH_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Docker health module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DockerfilePolicyTests(unittest.TestCase):
    def setUp(self):
        self.text = DOCKERFILE.read_text(encoding="utf-8")

    def test_uses_pinned_official_cuda_development_base(self):
        self.assertIn("FROM nvidia/cuda:12.4.1-devel-ubuntu22.04", self.text)
        self.assertNotIn("nvidia/cuda:latest", self.text.lower())

    def test_defines_python_non_root_user_and_stable_layout(self):
        self.assertIn("python3.10 -m venv /opt/venv", self.text)
        self.assertIn("USER app:app", self.text)
        for value in (
            "MODEL_CACHE_DIR=/models",
            "INPUT_DIR=/data/input",
            "OUTPUT_DIR=/data/output",
            "WORKSPACE_DIR=/workspace",
            "WORKDIR /app",
        ):
            self.assertIn(value, self.text)

    def test_context_has_no_model_copy_or_obvious_secret_material(self):
        copy_lines = [
            line.strip().lower()
            for line in self.text.splitlines()
            if line.strip().lower().startswith("copy ")
        ]
        self.assertTrue(copy_lines)
        for line in copy_lines:
            self.assertNotIn("model", line)
            self.assertNotIn("weight", line)
            self.assertNotIn(".env", line)
            self.assertNotIn("credential", line)
        for marker in ("private key", "api_key=", "token=", "password="):
            self.assertNotIn(marker, self.text.lower())

    def test_dockerignore_excludes_sensitive_and_large_local_content(self):
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for entry in (
            ".git",
            ".venv",
            ".cache",
            "artifacts",
            "models",
            "weights",
            ".env",
            ".env.*",
            ".idea",
            ".vscode",
        ):
            self.assertIn(entry, ignored)


class HealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.health = _load_health_module()

    @mock.patch("shutil.which", return_value=None)
    def test_cpu_only_health_is_successful_and_truthful(self, _which):
        report = self.health.collect_health({})
        self.assertEqual("GPU_NOT_AVAILABLE", report["status"])
        self.assertEqual("GPU_NOT_AVAILABLE", report["nvidia"]["status"])
        self.assertFalse(report["nvidia"]["smi_available"])

        output = io.StringIO()
        with mock.patch.object(self.health, "collect_health", return_value=report), redirect_stdout(output):
            self.assertEqual(0, self.health.main(["--json"]))
        self.assertEqual("GPU_NOT_AVAILABLE", json.loads(output.getvalue())["status"])

    @mock.patch("shutil.which", return_value=None)
    def test_strict_mode_rejects_missing_gpu(self, _which):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(2, self.health.main(["--require-gpu"]))
        self.assertIn("GPU_STATUS=GPU_NOT_AVAILABLE", output.getvalue())

    @mock.patch("shutil.which", return_value=None)
    def test_path_defaults_and_environment_overrides(self, _which):
        defaults = self.health.collect_health({})["paths"]
        self.assertEqual(self.health.DEFAULT_PATHS, defaults)
        overridden = self.health.collect_health(
            {
                "MODEL_CACHE_DIR": "/cache/models",
                "INPUT_DIR": "/job/in",
                "OUTPUT_DIR": "/job/out",
                "WORKSPACE_DIR": "/job/work",
            }
        )["paths"]
        self.assertEqual(
            {
                "model_cache": "/cache/models",
                "input": "/job/in",
                "output": "/job/out",
                "workspace": "/job/work",
            },
            overridden,
        )

    def test_nvidia_runtime_error_is_not_reported_as_no_gpu(self):
        def executable(name):
            return "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None

        with mock.patch("shutil.which", side_effect=executable):
            with mock.patch.object(
                self.health, "_command_output", return_value=(False, "driver unavailable")
            ):
                report = self.health.collect_health({})
        self.assertEqual("GPU_RUNTIME_ERROR", report["status"])
        self.assertEqual("driver unavailable", report["nvidia"]["detail"])


if __name__ == "__main__":
    unittest.main()
