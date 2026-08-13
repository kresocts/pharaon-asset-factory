import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_CACHE_PATH = ROOT / "docker" / "model_cache.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("model_cache", MODEL_CACHE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load model_cache module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = _load_module()


def valid_manifest(**overrides):
    data = {
        "schema_version": 1,
        "artifact_set": "fixture-set",
        "revision": "v1-abcdef0123456789",
        "namespace": "fixture-set/v1-abcdef0123456789",
        "description": "deterministic test fixture",
        "files": [
            {
                "path": "data/a.bin",
                "url": "https://example.invalid/a.bin",
                "size": 8,
                "sha256": hashlib.sha256(b"content!").hexdigest(),
                "role": "fixture-a",
            }
        ],
    }
    data.update(overrides)
    return data


def write_manifest(directory, data):
    path = Path(directory) / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class ManifestValidationTests(unittest.TestCase):
    def _parse(self, data):
        with tempfile.TemporaryDirectory() as directory:
            return module.parse_manifest(write_manifest(directory, data))

    def _reject(self, data, fragment):
        with self.assertRaises(module.ManifestValidationError) as raised:
            self._parse(data)
        self.assertIn(fragment, str(raised.exception))

    def test_valid_manifest_is_accepted(self):
        manifest = self._parse(valid_manifest())
        self.assertEqual(1, manifest["schema_version"])
        self.assertEqual("fixture-set", manifest["artifact_set"])
        self.assertEqual("v1-abcdef0123456789", manifest["revision"])
        self.assertEqual("fixture-set/v1-abcdef0123456789", manifest["namespace"])
        self.assertEqual(8, manifest["total_size"])
        self.assertEqual("fixture-a", manifest["files"][0]["role"])

    def test_missing_checksum_is_rejected(self):
        data = valid_manifest()
        del data["files"][0]["sha256"]
        self._reject(data, "missing required fields")

    def test_invalid_checksum_format_is_rejected(self):
        data = valid_manifest()
        data["files"][0]["sha256"] = "ZZ" + "0" * 62
        self._reject(data, "sha256")
        data["files"][0]["sha256"] = hashlib.sha256(b"x").hexdigest().upper()
        self._reject(data, "sha256")

    def test_missing_and_invalid_sizes_are_rejected(self):
        data = valid_manifest()
        del data["files"][0]["size"]
        self._reject(data, "missing required fields")
        data = valid_manifest()
        data["files"][0]["size"] = 0
        self._reject(data, "positive integer")
        data = valid_manifest()
        data["files"][0]["size"] = -4
        self._reject(data, "positive integer")

    def test_mutable_revisions_are_rejected(self):
        for revision in ("main", "latest", "master", "HEAD"):
            self._reject(valid_manifest(revision=revision), "mutable revision")

    def test_missing_or_ambiguous_revision_is_rejected(self):
        data = valid_manifest()
        del data["revision"]
        self._reject(data, "revision")
        self._reject(valid_manifest(revision=""), "revision")

    def test_unsupported_url_scheme_is_rejected(self):
        data = valid_manifest()
        data["files"][0]["url"] = "ftp://example.invalid/a.bin"
        self._reject(data, "unsupported URL scheme")

    def test_http_is_rejected_for_remote_hosts(self):
        data = valid_manifest()
        data["files"][0]["url"] = "http://example.invalid/a.bin"
        self._reject(data, "https")

    def test_http_is_allowed_for_loopback_test_fixtures(self):
        data = valid_manifest()
        data["files"][0]["url"] = "http://127.0.0.1:8000/a.bin"
        manifest = self._parse(data)
        self.assertEqual("http://127.0.0.1:8000/a.bin", manifest["files"][0]["url"])

    def test_mutable_url_references_are_rejected(self):
        for fragment in ("/resolve/main/", "/blob/main/", "/resolve/latest/"):
            data = valid_manifest()
            data["files"][0]["url"] = f"https://example.invalid{fragment}a.bin"
            self._reject(data, "mutable source reference")

    def test_url_with_embedded_credentials_is_rejected(self):
        data = valid_manifest()
        data["files"][0]["url"] = "https://user:pass@example.invalid/a.bin"
        self._reject(data, "embedded credentials")

    def test_duplicate_destination_is_rejected(self):
        data = valid_manifest()
        data["files"].append(dict(data["files"][0], role=None))
        self._reject(data, "duplicate destination path")

    def test_absolute_path_is_rejected(self):
        data = valid_manifest()
        data["files"][0]["path"] = "/etc/escape.bin"
        self._reject(data, "relative path")

    def test_traversal_path_is_rejected(self):
        data = valid_manifest()
        data["files"][0]["path"] = "../escape.bin"
        self._reject(data, "invalid component")
        data = valid_manifest()
        data["files"][0]["path"] = "data/../../escape.bin"
        self._reject(data, "invalid component")

    def test_unsafe_namespace_is_rejected(self):
        self._reject(valid_manifest(namespace="../outside"), "namespace")
        self._reject(valid_manifest(namespace="fixture-set/../x"), "namespace")
        self._reject(valid_manifest(namespace="/absolute"), "namespace")

    def test_unsupported_schema_version_is_rejected(self):
        self._reject(valid_manifest(schema_version=2), "schema_version")

    def test_empty_file_list_is_rejected(self):
        self._reject(valid_manifest(files=[]), "non-empty list")

    def test_missing_file_fields_are_rejected(self):
        data = valid_manifest()
        del data["files"][0]["url"]
        self._reject(data, "missing required fields")

    def test_file_count_policy_limit_is_enforced(self):
        file_template = valid_manifest()["files"][0]
        files = []
        for index in range(module.MAX_MANIFEST_FILES + 1):
            entry = dict(file_template)
            entry["path"] = f"data/file-{index}.bin"
            files.append(entry)
        self._reject(valid_manifest(files=files), "policy limit")

    def test_missing_manifest_file_is_rejected(self):
        with self.assertRaises(module.ManifestValidationError):
            module.parse_manifest(Path("does-not-exist.json"))

    def test_non_json_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(module.ManifestValidationError):
                module.parse_manifest(path)


if __name__ == "__main__":
    unittest.main()
