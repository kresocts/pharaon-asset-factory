import hashlib
import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
MANIFEST = ROOT / "docker" / "model-manifests" / "test-fixture.json"
SERVER = ROOT / "docker" / "model_fixture_server.py"


def _load_fixture_server():
    spec = importlib.util.spec_from_file_location("model_fixture_server", SERVER)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load model_fixture_server module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModelCacheDockerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
        self.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_entrypoint_exposes_models_command_before_fallback(self):
        self.assertLess(self.entrypoint.index("models)"), self.entrypoint.index("*)"))
        self.assertIn("exec python /app/model_cache.py", self.entrypoint)

    def test_entrypoint_does_not_run_models_automatically(self):
        self.assertIn("${1:-health}", self.entrypoint)
        default_case = self.entrypoint[self.entrypoint.index('case "${1:-health}"'):]
        self.assertLess(default_case.index("health)"), default_case.index("models)"))

    def test_dockerfile_copies_model_cache_after_heavy_dependency_layers(self):
        self.assertIn("COPY --chown=app:app docker/model_cache.py /app/model_cache.py", self.dockerfile)
        self.assertIn("COPY --chown=app:app docker/model_fixture_server.py /app/model_fixture_server.py", self.dockerfile)
        self.assertIn(
            "COPY --chown=app:app docker/model-manifests/test-fixture.json /app/model-manifests/test-fixture.json",
            self.dockerfile,
        )
        heavy = self.dockerfile.index("python -m pip install --no-cache-dir")
        copy = self.dockerfile.index("docker/model_cache.py")
        self.assertGreater(copy, heavy)

    def test_dockerfile_build_smoke_runs_offline_plan(self):
        self.assertIn("python /app/model_cache.py plan", self.dockerfile)
        self.assertIn("--manifest /app/model-manifests/test-fixture.json", self.dockerfile)

    def test_dockerfile_has_no_automatic_or_background_download(self):
        for marker in ("wget", "curl", "snapshot_download", "hf download", "huggingface-cli"):
            self.assertNotIn(marker, self.dockerfile.lower())
        self.assertNotIn("models)", self.entrypoint.split("health)")[0])

    def test_models_volume_and_runtime_user_remain_external_and_non_root(self):
        self.assertIn('VOLUME ["/models", "/data/input", "/data/output"]', self.dockerfile)
        self.assertIn("MODEL_CACHE_DIR=/models", self.dockerfile)
        self.assertIn("USER app:app", self.dockerfile)
        self.assertNotIn("/models/", self.dockerfile.lower())

    def test_fixture_manifest_matches_fixture_server_bytes(self):
        server = _load_fixture_server()
        declared = {item["path"]: (int(item["size"]), str(item["sha256"])) for item in self.manifest["files"]}
        self.assertEqual(set(declared), set(server.FIXTURE_CONTENT))
        total = 0
        for name, data in server.FIXTURE_CONTENT.items():
            size, sha256 = declared[name]
            total += size
            self.assertEqual(len(data), size)
            self.assertEqual(hashlib.sha256(data).hexdigest(), sha256)
        self.assertLess(total, 5 * 1024 * 1024)

    def test_fixture_manifest_uses_test_safe_loopback_urls(self):
        for item in self.manifest["files"]:
            self.assertTrue(item["url"].startswith("http://host.docker.internal:18765/"))
            self.assertNotIn("@", item["url"])
            lowered = item["url"].lower()
            for mutable in ("/main/", "/latest/", "/master/", "/head/"):
                self.assertNotIn(mutable, lowered)

    def test_fixture_manifest_uses_immutable_revision_and_valid_hashes(self):
        self.assertNotIn(self.manifest["revision"].lower(), ("main", "latest", "master", "head"))
        for item in self.manifest["files"]:
            self.assertEqual(len(item["sha256"]), 64)
            self.assertEqual(item["sha256"], item["sha256"].lower())
            self.assertGreater(int(item["size"]), 0)

    def test_docker_context_contains_no_model_weight_files_or_credentials(self):
        for suffix in ("safetensors", "ckpt", "pth", "pt"):
            hits = [path for path in (ROOT / "docker").rglob(f"*.{suffix}")]
            self.assertEqual([], hits, f"unexpected {suffix} file in docker context")
        context_text = (DOCKERFILE.read_text(encoding="utf-8") + ENTRYPOINT.read_text(encoding="utf-8")).lower()
        for marker in ("private key", "api_key=", "token=", "password=", "credential", "secret"):
            self.assertNotIn(marker, context_text)


if __name__ == "__main__":
    unittest.main()