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

    def test_http_is_allowed_for_host_docker_internal_test_fixtures(self):
        data = valid_manifest()
        data["files"][0]["url"] = "http://host.docker.internal:18765/a.bin"
        manifest = self._parse(data)
        self.assertEqual("http://host.docker.internal:18765/a.bin", manifest["files"][0]["url"])

    def test_mutable_url_references_are_rejected(self):
        for fragment in ("/resolve/main/", "/blob/main/", "/resolve/latest/"):
            data = valid_manifest()
            data["files"][0]["url"] = f"https://example.invalid{fragment}a.bin"
            self._reject(data, "mutable source reference")

    def test_percent_encoded_mutable_references_are_rejected(self):
        for url in (
            "https://example.invalid/resolve/%6dain/a.bin",
            "https://example.invalid/resolve/%6c%61%74%65%73%74/a.bin",
            "https://example.invalid/resolve/%4d%41%49%4e/a.bin",
            "https://example.invalid/%6d%61%69%6e/a.bin",
            "https://example.invalid/resolve%2Fmain%2Fa.bin",
        ):
            data = valid_manifest()
            data["files"][0]["url"] = url
            self._reject(data, "mutable source reference")

    def test_percent_encoded_immutable_path_is_accepted(self):
        data = valid_manifest()
        data["files"][0]["url"] = "https://example.invalid/some%20file%2Bv1.bin"
        manifest = self._parse(data)
        self.assertEqual("https://example.invalid/some%20file%2Bv1.bin", manifest["files"][0]["url"])

    def test_url_fragment_is_rejected(self):
        for url in ("https://example.invalid/a.bin#main", "https://example.invalid/a.bin#anything"):
            data = valid_manifest()
            data["files"][0]["url"] = url
            self._reject(data, "fragment")

    def test_malformed_percent_encoding_is_rejected(self):
        data = valid_manifest()
        data["files"][0]["url"] = "https://example.invalid/a%zz.bin"
        self._reject(data, "percent-encoding")

    def test_double_encoded_mutable_paths_are_rejected(self):
        for url in (
            "https://example.invalid/resolve/%256dain/a.bin",
            "https://example.invalid/resolve%252Fmain%252Fa.bin",
            "https://example.invalid/resolve/%252Fresolve%252Fmain%252F/a.bin",
        ):
            data = valid_manifest()
            data["files"][0]["url"] = url
            self._reject(data, "mutable source reference")

    def test_mutable_query_references_are_rejected(self):
        for query in (
            "?revision=main",
            "?ref=latest",
            "?branch=MAIN",
            "?tag=%6d%61%69%6e",
            "?rev=%256d%2561%2569%256e",
        ):
            data = valid_manifest()
            data["files"][0]["url"] = f"https://example.invalid/download{query}"
            self._reject(data, "mutable source reference")

    def test_legitimate_signed_query_is_accepted(self):
        data = valid_manifest()
        data["files"][0]["url"] = (
            "https://example.invalid/a.bin"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            "&X-Amz-Credential=AKIDEXAMPLE"
            "&X-Amz-Signature=deadbeef"
        )
        manifest = self._parse(data)
        self.assertIn("X-Amz-Signature", manifest["files"][0]["url"])

    def test_immutable_revision_query_is_accepted(self):
        data = valid_manifest()
        data["files"][0]["url"] = "https://example.invalid/a.bin?revision=v1-abcdef0123456789"
        manifest = self._parse(data)
        self.assertEqual(
            "https://example.invalid/a.bin?revision=v1-abcdef0123456789",
            manifest["files"][0]["url"],
        )

    def test_case_ambiguous_destinations_are_rejected(self):
        data = valid_manifest()
        data["files"][0]["path"] = "A.bin"
        data["files"].append(dict(data["files"][0], path="a.bin", role=None))
        self._reject(data, "case-ambiguous")

    def test_ancestor_destination_conflict_is_rejected(self):
        data = valid_manifest()
        data["files"][0]["path"] = "foo"
        data["files"].append(dict(data["files"][0], path="foo/bar", role=None))
        self._reject(data, "file ancestor")

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

    def test_manifest_exactly_at_byte_limit_is_accepted(self):
        raw = json.dumps(valid_manifest()).encode("utf-8")
        if len(raw) > module.MAX_MANIFEST_BYTES:
            self.skipTest("fixture is larger than the configured manifest limit")
        raw = raw + b" " * (module.MAX_MANIFEST_BYTES - len(raw))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(raw)
            manifest = module.parse_manifest(path)
        self.assertEqual(manifest["schema_version"], 1)

    def test_manifest_above_byte_limit_is_refused_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(b" " * (module.MAX_MANIFEST_BYTES + 1))
            with self.assertRaises(module.ManifestValidationError) as raised:
                module.parse_manifest(path)
        self.assertIn("byte limit", str(raised.exception))
        self.assertNotIn("Traceback", str(raised.exception))

    def test_invalid_utf8_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_bytes(bytes([0xff, 0xfe, 0x00]))
            with self.assertRaises(module.ManifestValidationError) as raised:
                module.parse_manifest(path)
        self.assertIn("UTF-8", str(raised.exception))
        self.assertNotIn("Traceback", str(raised.exception))

    def test_duplicate_top_level_key_is_refused(self):
        text = (
            '{"schema_version": 1, "schema_version": 1, '
            '"artifact_set": "fixture-set", "revision": "v1-abcdef0123456789", '
            '"namespace": "fixture-set/v1-abcdef0123456789", "files": []}'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(module.ManifestValidationError) as raised:
                module.parse_manifest(path)
        self.assertIn("duplicate manifest object key", str(raised.exception))
        self.assertNotIn("Traceback", str(raised.exception))

    def test_duplicate_nested_file_key_is_refused(self):
        text = (
            '{"schema_version": 1, "artifact_set": "fixture-set", '
            '"revision": "v1-abcdef0123456789", '
            '"namespace": "fixture-set/v1-abcdef0123456789", '
            '"files": [{"path": "a.bin", "path": "b.bin", "url": '
            '"https://example.invalid/a.bin", "size": 1, '
            '"sha256": "'
            + "0" * 64
            + '"}]}'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(module.ManifestValidationError) as raised:
                module.parse_manifest(path)
        self.assertIn("duplicate manifest object key", str(raised.exception))
        self.assertNotIn("Traceback", str(raised.exception))

    def test_non_finite_json_constants_are_refused(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            data = valid_manifest()
            data["files"][0]["size"] = constant
            text = json.dumps(data).replace(f'"{constant}"', constant)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "manifest.json"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(module.ManifestValidationError) as raised:
                    module.parse_manifest(path)
            self.assertIn("non-finite", str(raised.exception))
            self.assertNotIn("Traceback", str(raised.exception))

    def test_deeply_nested_but_size_compliant_manifest_is_refused(self):
        text = "[" * 10000 + "0" + "]" * 10000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(module.ManifestValidationError) as raised:
                module.parse_manifest(path)
        self.assertIn("nesting depth", str(raised.exception))
        self.assertNotIn("Traceback", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
