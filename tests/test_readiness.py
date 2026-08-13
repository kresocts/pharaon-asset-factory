import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
READINESS_PATH = ROOT / "docker" / "readiness.py"
EXPECTED_COMMIT = "82920d643c0dc2f7bfd7255f45f62d386edfe60c"


def _load_readiness():
    spec = importlib.util.spec_from_file_location("readiness", READINESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load readiness module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeHealth:
    def _configured_paths(self, environment):
        return {
            "model_cache": environment.get("MODEL_CACHE_DIR", "/models"),
            "input": environment.get("INPUT_DIR", "/data/input"),
            "output": environment.get("OUTPUT_DIR", "/data/output"),
            "workspace": environment.get("WORKSPACE_DIR", "/workspace"),
            "hunyuan_source": environment.get("HUNYUAN_SOURCE_DIR", "/opt/hunyuan3d"),
        }

    def _pytorch_diagnostics(self):
        return {
            "status": "PYTORCH_AVAILABLE",
            "versions": {"torch": "2.5.1+cu124", "torchvision": "0.20.1+cu124", "torchaudio": "2.5.1+cu124"},
            "versions_match": True,
            "cuda_build_version": "12.4",
            "cuda_wheel": True,
            "cuda_available": False,
            "device_count": 0,
            "cuda_operation": "NOT_ATTEMPTED_NO_GPU",
        }

    def _dependency_diagnostics(self):
        return {"status": "DEPENDENCY_IMPORTS_READY", "imports": {}}

    def _hunyuan_diagnostics(self, paths, environment):
        return {
            "source_path": paths["hunyuan_source"],
            "source_present": True,
            "expected_revision": EXPECTED_COMMIT,
            "revision": EXPECTED_COMMIT,
            "revision_matches": True,
            "custom_rasterizer": {"status": "CUSTOM_RASTERIZER_BUILT", "compiled_artifacts": ["rasterizer.so"]},
            "differentiable_renderer": {"status": "DIFFERENTIABLE_RENDERER_BUILT", "compiled_artifacts": ["renderer.so"]},
        }

    def _model_diagnostics(self, paths):
        return {"status": "MODEL_WEIGHTS_NOT_PRESENT_EXPECTED", "detected_files": [], "download_attempted": False}

    def _nvidia_diagnostics(self):
        return {"smi_available": False, "status": "GPU_NOT_AVAILABLE", "devices": []}


class _GpuHealth(_FakeHealth):
    def _pytorch_diagnostics(self):
        report = super()._pytorch_diagnostics()
        report.update(
            cuda_available=True,
            device_count=1,
            device_name="NVIDIA GeForce RTX 4060 Laptop GPU",
            cuda_operation="PYTORCH_CUDA_OPERATION_OK",
            cuda_operation_result=12.0,
        )
        return report

    def _nvidia_diagnostics(self):
        return {
            "smi_available": True,
            "status": "GPU_AVAILABLE",
            "devices": ["0, NVIDIA GeForce RTX 4060 Laptop GPU, 576.02, 8188 MiB"],
        }


class _FakeNativeSmoke:
    def __init__(self, gpu=False):
        self.gpu = gpu

    def collect_native_smoke(self, run_gpu_operation=False):
        operation = (
            {"status": "CUSTOM_RASTERIZER_CUDA_OPERATION_OK", "covered_pixels": 18}
            if self.gpu and run_gpu_operation
            else {"status": "NOT_ATTEMPTED_NO_GPU" if run_gpu_operation else "NOT_ATTEMPTED_IMPORT_ONLY"}
        )
        return {
            "status": "HUNYUAN_NATIVE_EXTENSIONS_READY",
            "custom_rasterizer": {"status": "IMPORT_OK", "module_path": "/site/custom_rasterizer/__init__.py"},
            "custom_rasterizer_kernel": {"status": "IMPORT_OK", "module_path": "/site/custom_rasterizer_kernel.so"},
            "differentiable_renderer": {"status": "IMPORT_OK", "module_path": "/site/mesh_inpaint_processor.so"},
            "renderer_operation": {"status": "RENDERER_NATIVE_OPERATION_OK", "color_shape": [3, 3]},
            "custom_rasterizer_operation": operation,
        }


class _TempPaths:
    def __enter__(self):
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.paths = {
            "MODEL_CACHE_DIR": str(root / "models"),
            "INPUT_DIR": str(root / "input"),
            "OUTPUT_DIR": str(root / "output"),
            "WORKSPACE_DIR": str(root / "workspace"),
            "HUNYUAN_SOURCE_DIR": str(root / "hunyuan"),
        }
        for key in self.paths:
            Path(self.paths[key]).mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._temp.cleanup()
        return False


class ReadinessContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_readiness()

    def _cpu_ready_report(self):
        with (
            _TempPaths() as tmp,
            mock.patch.object(self.module.platform, "python_version", return_value="3.10.16"),
            mock.patch.object(self.module, "_health_module", return_value=_FakeHealth()),
            mock.patch.object(self.module, "_native_smoke_module", return_value=_FakeNativeSmoke(False)),
        ):
            return self.module.collect_readiness("cpu", dict(tmp.paths))

    def test_json_contract_is_stable_and_valid(self):
        report = self._cpu_ready_report()
        self.assertEqual(1, report["schema_version"])
        self.assertEqual("cpu", report["profile"])
        self.assertIsInstance(report["ready"], bool)
        self.assertTrue(report["ready"])
        self.assertEqual("READY", report["classification"])
        self.assertEqual(0, report["exit_code"])
        self.assertIn("checks", report)
        self.assertIn("failure_summary", report)
        expected_ids = {
            "python.version",
            "runtime.config",
            "torch.import",
            "torch.version",
            "hunyuan.source",
            "hunyuan.revision",
            "dependencies.imports",
            "native.artifacts",
            "native.custom_rasterizer.import",
            "native.renderer.import",
            "native.renderer.operation",
            "paths.models.exists",
            "paths.models.writable",
            "paths.input.writable",
            "paths.output.writable",
            "paths.workspace.writable",
            "paths.hunyuan_source.exists",
            "model.cache.external",
            "weights.present",
            "inference.full_ready",
        }
        actual_ids = {check["id"] for check in report["checks"]}
        self.assertTrue(expected_ids.issubset(actual_ids))
        for check in report["checks"]:
            self.assertIn(check["status"], ("PASS", "FAIL", "SKIP"))
            self.assertTrue(check["message"])
        json.dumps(report)

    def test_cpu_profile_is_ready_without_gpu_and_weights_absent(self):
        report = self._cpu_ready_report()
        self.assertEqual("READY", report["status"])
        self.assertFalse(report["facts"]["gpu"]["visible"])
        self.assertEqual("ABSENT", report["facts"]["weights"]["state"])
        self.assertFalse(report["facts"]["inference"]["full_ready"])
        self.assertEqual("PASS", self._check_status(report, "weights.present"))
        self.assertEqual("PASS", self._check_status(report, "inference.full_ready"))

    def test_main_emits_valid_json_for_cpu_ready(self):
        with (
            _TempPaths() as tmp,
            mock.patch.object(self.module.platform, "python_version", return_value="3.10.16"),
            mock.patch.object(self.module, "_health_module", return_value=_FakeHealth()),
            mock.patch.object(self.module, "_native_smoke_module", return_value=_FakeNativeSmoke(False)),
            mock.patch.dict(self.module.os.environ, tmp.paths),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = self.module.main(["--profile", "cpu", "--json"])
        self.assertEqual(0, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("cpu", payload["profile"])
        self.assertTrue(payload["ready"])
        self.assertNotIn("SECRET", output.getvalue())

    def test_native_gpu_without_gpu_is_not_ready_and_has_no_unhandled_exception(self):
        with (
            _TempPaths() as tmp,
            mock.patch.object(self.module.platform, "python_version", return_value="3.10.16"),
            mock.patch.object(self.module, "_health_module", return_value=_FakeHealth()),
            mock.patch.object(self.module, "_native_smoke_module", return_value=_FakeNativeSmoke(False)),
        ):
            report = self.module.collect_readiness("native-gpu", dict(tmp.paths))
        self.assertFalse(report["ready"])
        self.assertEqual("NOT_READY", report["classification"])
        self.assertEqual(2, report["exit_code"])
        self.assertEqual("FAIL", self._check_status(report, "gpu.visible"))
        self.assertEqual("FAIL", self._check_status(report, "torch.cuda.available"))
        self.assertEqual("FAIL", self._check_status(report, "native.custom_rasterizer.operation"))
        self.assertTrue(report["failure_summary"])

    def test_native_gpu_ready_passes_all_gpu_checks(self):
        fake_cuda = types.SimpleNamespace(
            is_available=lambda: True,
            get_device_capability=lambda index: (8, 9),
        )
        fake_torch = types.SimpleNamespace(cuda=fake_cuda)
        with (
            _TempPaths() as tmp,
            mock.patch.object(self.module.platform, "python_version", return_value="3.10.16"),
            mock.patch.object(self.module, "_health_module", return_value=_GpuHealth()),
            mock.patch.object(self.module, "_native_smoke_module", return_value=_FakeNativeSmoke(True)),
            mock.patch.dict(sys.modules, {"torch": fake_torch}),
        ):
            report = self.module.collect_readiness("native-gpu", dict(tmp.paths))
        self.assertTrue(report["ready"])
        self.assertEqual("READY", report["classification"])
        for check_id in ("gpu.visible", "torch.cuda.available", "torch.cuda.operation", "native.custom_rasterizer.operation"):
            self.assertEqual("PASS", self._check_status(report, check_id), check_id)
        self.assertEqual("8.9", report["facts"]["gpu"].get("compute_capability"))
        self.assertIn("RTX 4060", report["facts"]["gpu"]["device_name"])

    def test_diagnostic_error_is_distinct_from_not_ready(self):
        broken = mock.Mock()
        broken._configured_paths.side_effect = RuntimeError("broken diagnostic")
        output = io.StringIO()
        with (
            mock.patch.object(self.module, "_health_module", return_value=broken),
            mock.patch.object(self.module, "_native_smoke_module", return_value=_FakeNativeSmoke(False)),
            redirect_stdout(output),
        ):
            exit_code = self.module.main(["--profile", "cpu", "--json"])
        self.assertEqual(3, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("DIAGNOSTIC_ERROR", payload["classification"])
        self.assertFalse(payload["ready"])
        self.assertIn("broken diagnostic", payload["detail"])

    def test_path_failure_produces_not_ready_and_no_probe_left_behind(self):
        with (
            _TempPaths() as tmp,
            mock.patch.object(self.module.platform, "python_version", return_value="3.10.16"),
            mock.patch.object(self.module, "_health_module", return_value=_FakeHealth()),
            mock.patch.object(self.module, "_native_smoke_module", return_value=_FakeNativeSmoke(False)),
        ):
            missing = str(Path(tmp._temp.name) / "missing-models")
            env = dict(tmp.paths)
            env["MODEL_CACHE_DIR"] = missing
            report = self.module.collect_readiness("cpu", env)
        self.assertFalse(report["ready"])
        self.assertEqual("FAIL", self._check_status(report, "paths.models.exists"))
        self.assertEqual("FAIL", self._check_status(report, "paths.models.writable"))
        self.assertIn("missing", self._check(report, "paths.models.exists")["message"].lower())
        self.assertFalse(list(Path(tmp._temp.name).glob(".readiness-probe-*")))

    def test_invalid_profile_is_invalid_request(self):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = self.module.main(["--profile", "bogus", "--json"])
        self.assertEqual(64, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("INVALID_REQUEST", payload["classification"])

    def _check(self, report, check_id):
        return next(check for check in report["checks"] if check["id"] == check_id)

    def _check_status(self, report, check_id):
        return self._check(report, check_id)["status"]


if __name__ == "__main__":
    unittest.main()

class ReadinessDockerPolicyTests(unittest.TestCase):
    def test_entrypoint_exposes_ready_command_before_fallback(self):
        entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertLess(entrypoint.index("ready)"), entrypoint.index("*)"))
        self.assertIn("exec python /app/readiness.py", entrypoint)

    def test_dockerfile_copies_readiness_and_keeps_external_model_cache(self):
        dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY --chown=app:app docker/readiness.py /app/readiness.py", dockerfile)
        self.assertIn('VOLUME ["/models", "/data/input", "/data/output"]', dockerfile)
        self.assertNotIn("/models/", dockerfile.lower())
