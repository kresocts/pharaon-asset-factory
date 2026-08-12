import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock


from pathlib import Path


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
        self.assertIn("FROM nvidia/cuda:12.4.1-devel-ubuntu22.04@sha256:", self.text)
        self.assertNotIn("nvidia/cuda:latest", self.text.lower())

    def test_defines_python_non_root_user_and_stable_layout(self):
        self.assertIn("python3.10 -m venv /opt/venv", self.text)
        self.assertIn("USER app:app", self.text)
        for value in (
            "MODEL_CACHE_DIR=/models",
            "INPUT_DIR=/data/input",
            "OUTPUT_DIR=/data/output",
            "WORKSPACE_DIR=/workspace",
            "HUNYUAN_SOURCE_DIR=/opt/hunyuan3d",
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

    def _ready_cpu_report(self):
        with (
            mock.patch.object(self.health.platform, "python_version", return_value="3.10.16"),
            mock.patch.object(
                self.health,
                "_nvidia_diagnostics",
                return_value={"smi_available": False, "status": "GPU_NOT_AVAILABLE", "devices": []},
            ),
            mock.patch.object(self.health, "_cuda_diagnostics", return_value={"version_environment": "12.4", "home": "/usr/local/cuda", "nvcc_available": True}),
            mock.patch.object(self.health, "_pytorch_diagnostics", return_value={"status": "PYTORCH_AVAILABLE", "cuda_available": False, "cuda_operation": "NOT_ATTEMPTED_NO_GPU"}),
            mock.patch.object(self.health, "_dependency_diagnostics", return_value={"status": "DEPENDENCY_IMPORTS_READY", "imports": {}}),
            mock.patch.object(self.health, "_hunyuan_diagnostics", return_value={
                "source_path": "/opt/hunyuan3d",
                "source_present": True,
                "expected_revision": self.health.DEFAULT_HUNYUAN_COMMIT,
                "revision": self.health.DEFAULT_HUNYUAN_COMMIT,
                "revision_matches": True,
                "custom_rasterizer": {"status": "CUSTOM_RASTERIZER_BUILT", "compiled_artifacts": []},
                "differentiable_renderer": {"status": "DIFFERENTIABLE_RENDERER_BUILT", "compiled_artifacts": []},
            }),
            mock.patch.object(self.health, "_native_diagnostics", return_value={"status": "NATIVE_IMPORTS_READY", "gpu_operation": {"status": "NOT_ATTEMPTED"}, "cuda_architectures": "8.6;8.9", "modules": {}}),
            mock.patch.object(self.health, "_model_diagnostics", return_value={"status": "MODEL_WEIGHTS_NOT_PRESENT_EXPECTED", "detected_files": [], "download_attempted": False}),
        ):
            return self.health.collect_health({})

    def test_cpu_only_health_is_successful_and_truthful(self):
        report = self._ready_cpu_report()
        self.assertEqual("HUNYUAN_NATIVE_EXTENSIONS_READY", report["status"])
        self.assertEqual("GPU_NOT_AVAILABLE", report["gpu_status"])
        self.assertFalse(report["full_hunyuan_ready"])
        self.assertFalse(report["pytorch"]["cuda_available"])

        output = io.StringIO()
        with mock.patch.object(self.health, "collect_health", return_value=report), redirect_stdout(output):
            self.assertEqual(0, self.health.main(["--json"]))
        self.assertEqual("HUNYUAN_NATIVE_EXTENSIONS_READY", json.loads(output.getvalue())["status"])

    def test_strict_mode_rejects_missing_pytorch_gpu(self):
        report = self._ready_cpu_report()
        output = io.StringIO()
        with mock.patch.object(self.health, "collect_health", return_value=report), redirect_stdout(output):
            self.assertEqual(2, self.health.main(["--require-gpu"]))
        self.assertIn("GPU_STATUS=GPU_NOT_AVAILABLE", output.getvalue())

    @mock.patch("shutil.which", return_value=None)
    def test_path_defaults_and_environment_overrides(self, _which):
        defaults = self.health._configured_paths({})
        self.assertEqual(self.health.DEFAULT_PATHS, defaults)
        overridden = self.health._configured_paths(
            {
                "MODEL_CACHE_DIR": "/cache/models",
                "INPUT_DIR": "/job/in",
                "OUTPUT_DIR": "/job/out",
                "WORKSPACE_DIR": "/job/work",
                "HUNYUAN_SOURCE_DIR": "/third-party/hunyuan",
            }
        )
        self.assertEqual(
            {
                "model_cache": "/cache/models",
                "input": "/job/in",
                "output": "/job/out",
                "workspace": "/job/work",
                "hunyuan_source": "/third-party/hunyuan",
            },
            overridden,
        )

    def test_nvidia_runtime_error_is_not_reported_as_no_gpu(self):
        def executable(name):
            return "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None

        with mock.patch("shutil.which", side_effect=executable):
            with mock.patch.object(self.health, "_command_output", return_value=(False, "driver unavailable")):
                report = self.health._nvidia_diagnostics()
        self.assertEqual("GPU_RUNTIME_ERROR", report["status"])
        self.assertEqual("driver unavailable", report["detail"])


if __name__ == "__main__":
    unittest.main()
