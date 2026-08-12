import importlib.util
import re
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
LOCKFILE = ROOT / "docker" / "hunyuan-requirements.lock.txt"
HEALTH_PATH = ROOT / "docker" / "health.py"
SMOKE_PATH = ROOT / "docker" / "dependency_smoke.py"
EXPECTED_COMMIT = "82920d643c0dc2f7bfd7255f45f62d386edfe60c"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DependencyPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        cls.lockfile = LOCKFILE.read_text(encoding="utf-8")

    def test_pytorch_contract_is_explicit_and_uses_cu124_index(self):
        for value in ("PYTORCH_VERSION=2.5.1", "TORCHVISION_VERSION=0.20.1", "TORCHAUDIO_VERSION=2.5.1"):
            self.assertIn(value, self.dockerfile)
        self.assertIn("https://download.pytorch.org/whl/cu124", self.dockerfile)
        self.assertIn("assert torch.version.cuda == '12.4'", self.dockerfile)

    def test_hunyuan_source_and_requirements_are_immutable(self):
        self.assertIn(f"HUNYUAN_COMMIT={EXPECTED_COMMIT}", self.dockerfile)
        self.assertRegex(self.dockerfile, r"HUNYUAN_COMMIT=[0-9a-f]{40}")
        self.assertIn("git -C /opt/hunyuan3d rev-parse HEAD", self.dockerfile)
        self.assertIn("sha256sum --check --strict", self.dockerfile)
        self.assertNotRegex(self.dockerfile, r"checkout\s+(origin/)?main")

    def test_upstream_unpinned_requirements_are_frozen(self):
        for requirement in ("timm==1.0.15", "pythreejs==2.4.2", "torchdiffeq==0.2.5", "deepspeed==0.17.1"):
            self.assertIn(requirement, self.lockfile)
        self.assertNotIn("mirrors.cloud.tencent.com", self.lockfile)
        self.assertNotIn("mirrors.aliyun.com", self.lockfile)

    def test_no_model_download_and_non_editable_native_install(self):
        lower = self.dockerfile.lower()
        for forbidden in (
            "wget ",
            "curl ",
            "from_pretrained",
            "pip install -e",

            "realesrgan_x4plus.pth",
        ):
            self.assertNotIn(forbidden, lower)
        self.assertIn("compile_mesh_painter.sh", lower)
        self.assertNotIn("/models/", lower)
        self.assertIn('VOLUME ["/models", "/data/input", "/data/output"]', self.dockerfile)

    def test_no_credentials_or_cloud_integrations(self):
        lower = self.dockerfile.lower()
        for forbidden in ("api_key=", "token=", "password=", "vast.ai", "runpod"):
            self.assertNotIn(forbidden, lower)


class DiagnosticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.health = _load(HEALTH_PATH, "t0011_health")
        cls.smoke = _load(SMOKE_PATH, "t0011_dependency_smoke")

    def test_missing_native_extensions_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hunyuan3d"
            (source / "hy3dpaint" / "custom_rasterizer").mkdir(parents=True)
            (source / "hy3dpaint" / "DifferentiableRenderer").mkdir(parents=True)
            revision = Path(directory) / "revision"
            revision.write_text(EXPECTED_COMMIT + "\n", encoding="utf-8")
            report = self.health._hunyuan_diagnostics(
                {"hunyuan_source": str(source)},
                {"HUNYUAN_COMMIT": EXPECTED_COMMIT, "HUNYUAN_REVISION_FILE": str(revision)},
            )
        self.assertTrue(report["revision_matches"])
        self.assertEqual("CUSTOM_RASTERIZER_NOT_BUILT", report["custom_rasterizer"]["status"])
        self.assertEqual("DIFFERENTIABLE_RENDERER_NOT_BUILT", report["differentiable_renderer"]["status"])

    def test_compiled_native_artifact_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hunyuan3d"
            custom = source / "hy3dpaint" / "custom_rasterizer"
            renderer = source / "hy3dpaint" / "DifferentiableRenderer"
            custom.mkdir(parents=True)
            renderer.mkdir(parents=True)
            (custom / "rasterizer.so").write_bytes(b"not a real extension")
            report = self.health._hunyuan_diagnostics({"hunyuan_source": str(source)}, {})
        self.assertEqual("CUSTOM_RASTERIZER_BUILT", report["custom_rasterizer"]["status"])

    def test_dependency_smoke_imports_representatives_with_offline_guards(self):
        modules = {
            name: types.SimpleNamespace(__version__=("2.5.1+cu124" if name == "torch" else "1.0"))
            for name in self.smoke.REPRESENTATIVE_IMPORTS
        }
        modules["torchvision"].__version__ = "0.20.1+cu124"
        modules["torchaudio"].__version__ = "2.5.1+cu124"
        with mock.patch.object(self.smoke.importlib, "import_module", side_effect=lambda name: modules[name]):
            report = self.smoke.collect_imports()
        self.assertEqual("DEPENDENCY_IMPORTS_READY", report["status"])
        self.assertEqual("1", report["offline_guards"]["HF_HUB_OFFLINE"])
        self.assertEqual(set(self.smoke.REPRESENTATIVE_IMPORTS), set(report["imports"]))

    def test_version_mismatch_fails_dependency_contract(self):
        module = types.SimpleNamespace(__version__="99.0.0")
        with mock.patch.object(self.smoke.importlib, "import_module", return_value=module):
            report = self.smoke.collect_imports()
        self.assertEqual("DEPENDENCY_IMPORTS_FAILED", report["status"])
        self.assertEqual("VERSION_MISMATCH", report["imports"]["torch"]["status"])


if __name__ == "__main__":
    unittest.main()
