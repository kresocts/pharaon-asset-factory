import importlib.util
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
SMOKE_PATH = ROOT / "docker" / "native_smoke.py"
EXPECTED_COMMIT = "82920d643c0dc2f7bfd7255f45f62d386edfe60c"


def _load_smoke():
    spec = importlib.util.spec_from_file_location("native_smoke", SMOKE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load native smoke")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeBuildPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCKERFILE.read_text(encoding="utf-8")
        cls.lower = cls.text.lower()

    def test_pinned_source_and_architecture_contract_are_unchanged(self):
        self.assertIn(f"ARG HUNYUAN_COMMIT={EXPECTED_COMMIT}", self.text)
        self.assertIn("ARG TORCH_CUDA_ARCH_LIST=8.6;8.9", self.text)
        self.assertIn("TORCH_CUDA_ARCH_LIST=8.6;8.9", self.text)
        for pin in ("PYTORCH_VERSION=2.5.1", "TORCHVISION_VERSION=0.20.1", "TORCHAUDIO_VERSION=2.5.1"):
            self.assertIn(pin, self.text)

    def test_both_native_builds_are_deterministic_docker_layers(self):
        self.assertIn("python3.10-dev python3-dev", self.text)
        self.assertIn("pip install --no-cache-dir --no-build-isolation .", self.text)
        self.assertNotIn("pip install -e", self.lower)
        self.assertIn("bash compile_mesh_painter.sh", self.text)
        self.assertIn("custom_rasterizer_kernel*.so", self.text)
        self.assertIn("mesh_inpaint_processor*.so", self.text)

    def test_entrypoint_exposes_native_smoke_before_fallback(self):
        entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertLess(entrypoint.index("native-smoke)"), entrypoint.index("*)"))

    def test_build_verifies_toolchain_and_has_no_asset_download(self):
        for value in ("nvcc --version", "python --version", "c++ --version", "import ninja, torch"):
            self.assertIn(value, self.text)
        for forbidden in ("wget ", "curl ", "from_pretrained", "realesrgan_x4plus.pth", ".safetensors"):
            self.assertNotIn(forbidden, self.lower)


class NativeSmokeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.smoke = _load_smoke()

    def test_import_smoke_reports_actual_module_paths_and_renderer_operation(self):
        custom = types.SimpleNamespace(__file__="/site/custom_rasterizer/__init__.py")
        kernel = types.SimpleNamespace(__file__="/site/custom_rasterizer_kernel.so")
        renderer = types.SimpleNamespace(
            __file__="/opt/hunyuan3d/hy3dpaint/DifferentiableRenderer/mesh_inpaint_processor.so",
            meshVerticeInpaint=object(),
            meshVerticeColor=object(),
        )
        modules = {"torch": types.SimpleNamespace(__file__="/site/torch/__init__.py"), "custom_rasterizer": custom, "custom_rasterizer_kernel": kernel,
                   "hy3dpaint.DifferentiableRenderer.mesh_inpaint_processor": renderer}
        with (
            mock.patch.object(self.smoke.importlib, "import_module", side_effect=lambda name: modules[name]),
            mock.patch.object(self.smoke, "_renderer_operation", return_value={"status": "RENDERER_NATIVE_OPERATION_OK"}),
        ):
            report = self.smoke.collect_native_smoke(False)
        self.assertEqual("HUNYUAN_NATIVE_EXTENSIONS_READY", report["status"])
        self.assertTrue(report["custom_rasterizer_kernel"]["module_path"].endswith(".so"))
        self.assertEqual("NOT_ATTEMPTED_IMPORT_ONLY", report["custom_rasterizer_operation"]["status"])
        self.assertEqual("1", report["offline_guards"]["HF_HUB_OFFLINE"])

    def test_gpu_fixture_reports_native_operation_without_full_model_claim(self):
        modules = {name: types.SimpleNamespace(__file__="/tmp/module.so") for name in (
            "torch",
            "custom_rasterizer", "custom_rasterizer_kernel",
            "hy3dpaint.DifferentiableRenderer.mesh_inpaint_processor")}
        with (
            mock.patch.object(self.smoke.importlib, "import_module", side_effect=lambda name: modules[name]),
            mock.patch.object(self.smoke, "_renderer_operation", return_value={"status": "RENDERER_NATIVE_OPERATION_OK"}),
            mock.patch.object(self.smoke, "_rasterizer_gpu_operation", return_value={"status": "CUSTOM_RASTERIZER_CUDA_OPERATION_OK"}),
        ):
            report = self.smoke.collect_native_smoke(True)
        self.assertEqual("CUSTOM_RASTERIZER_CUDA_OPERATION_OK", report["custom_rasterizer_operation"]["status"])
        self.assertNotIn("full_hunyuan_ready", report)


if __name__ == "__main__":
    unittest.main()
