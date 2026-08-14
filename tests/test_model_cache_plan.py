import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


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


def _content(name, size):
    base = f"{name}-".encode("utf-8")
    return (base * ((size // len(base)) + 1))[:size]


class _Fixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache = self.root / "models"
        self.cache.mkdir()
        self.files = {
            "a.bin": _content("alpha", 16),
            "b.bin": _content("beta", 32),
        }
        manifest_files = []
        for name, data in self.files.items():
            manifest_files.append(
                {
                    "path": f"data/{name}",
                    "url": f"https://example.invalid/{name}",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        self.manifest = {
            "schema_version": 1,
            "artifact_set": "fixture-set",
            "revision": "v1-abcdef0123456789",
            "namespace": "fixture-set/v1-abcdef0123456789",
            "files": manifest_files,
        }
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def environment(self):
        return {"MODEL_CACHE_DIR": str(self.cache)}

    def target(self, name):
        return self.cache / "fixture-set" / "v1-abcdef0123456789" / "data" / name

    def cleanup(self):
        self.temporary.cleanup()


class OfflinePlanTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()

    def tearDown(self):
        self.fixture.cleanup()

    def _run(self, command, environment=None):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = module.main(
                [command, "--manifest", str(self.fixture.manifest_path), "--json"],
                dict(self.fixture.environment() if environment is None else environment),
            )
        return exit_code, json.loads(output.getvalue())

    def test_plan_is_offline_deterministic_and_reports_byte_totals(self):
        with mock.patch.object(module, "_opener", side_effect=AssertionError("network access")):
            first_exit, first = self._run("plan")
            second_exit, second = self._run("plan")
        self.assertEqual(0, first_exit)
        self.assertEqual("plan", first["command"])
        self.assertTrue(first["success"])
        self.assertEqual("OK", first["classification"])
        self.assertEqual(0, first["exit_code"])
        self.assertEqual(1, first["schema_version"])
        self.assertEqual(2, first["file_count"])
        self.assertEqual({"ABSENT": 2, "PARTIAL": 0, "CORRUPTED": 0, "VERIFIED": 0}, first["file_counts"])
        self.assertEqual(48, first["bytes"]["total_expected"])
        self.assertEqual(48, first["bytes"]["required"])
        self.assertIsNone(first["bytes"]["max_bytes"])
        self.assertEqual(0, first["network"]["requests_attempted"])
        self.assertEqual(0, first["network"]["retries"])
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(first, second)
        self.assertEqual(2, len(first["files"]))
        for entry in first["files"]:
            self.assertEqual("ABSENT", entry["state"])
            self.assertTrue(entry["target"].startswith(str(self.fixture.cache)))

    def test_status_distinguishes_absent_partial_corrupted_verified(self):
        verified = self.fixture.target("a.bin")
        verified.parent.mkdir(parents=True, exist_ok=True)
        verified.write_bytes(self.fixture.files["a.bin"])
        corrupted = self.fixture.target("b.bin")
        corrupted.parent.mkdir(parents=True, exist_ok=True)
        corrupted.write_bytes(b"wrong-content")
        partial = self.fixture.target("a.bin").with_name("_acq-a1.part")
        partial.write_bytes(b"incomplete")

        exit_code, report = self._run("status")
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual("VERIFIED", by_path["data/a.bin"]["state"])
        self.assertEqual("CORRUPTED", by_path["data/b.bin"]["state"])
        self.assertIn("size mismatch", by_path["data/b.bin"]["detail"])

        # Remove the corrupted final to check the partial state of the second file.
        corrupted.unlink()
        self.fixture.target("b.bin").with_name("_acq-b1.part").write_bytes(b"partial-b")
        exit_code, report = self._run("status")
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual("PARTIAL", by_path["data/b.bin"]["state"])
        self.assertEqual("VERIFIED", by_path["data/a.bin"]["state"])
        self.assertEqual(32, report["bytes"]["required"])

    def test_verify_is_offline_and_returns_nonzero_when_not_verified(self):
        with mock.patch.object(module, "_opener", side_effect=AssertionError("network access")):
            exit_code, report = self._run("verify")
        self.assertEqual(4, exit_code)
        self.assertFalse(report["success"])
        self.assertEqual("NOT_VERIFIED", report["classification"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual({"ABSENT": 2, "PARTIAL": 0, "CORRUPTED": 0, "VERIFIED": 0}, report["file_counts"])

    def test_verify_succeeds_when_everything_is_verified(self):
        for name, data in self.fixture.files.items():
            target = self.fixture.target(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        exit_code, report = self._run("verify")
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        self.assertEqual({"ABSENT": 0, "PARTIAL": 0, "CORRUPTED": 0, "VERIFIED": 2}, report["file_counts"])
        self.assertEqual(0, report["bytes"]["required"])

    def test_plan_reports_absent_when_cache_root_is_missing(self):
        missing = self.fixture.root / "missing-models"
        exit_code, report = self._run("plan", {"MODEL_CACHE_DIR": str(missing)})
        self.assertEqual(0, exit_code)
        self.assertEqual({"ABSENT": 2, "PARTIAL": 0, "CORRUPTED": 0, "VERIFIED": 0}, report["file_counts"])
        self.assertEqual(str(missing), report["cache_root"])

    def test_cache_root_defaults_to_models(self):
        default = module._cache_root({})
        self.assertEqual("models", default.name)
        custom = module._cache_root({"MODEL_CACHE_DIR": "/custom/models"})
        self.assertTrue(str(custom).replace("\\", "/").endswith("/custom/models"))

    def test_invalid_manifest_exits_with_manifest_code_and_json(self):
        self.fixture.manifest_path.write_text("{}", encoding="utf-8")
        exit_code, report = self._run("plan")
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", report["classification"])
        self.assertFalse(report["success"])

    def test_validate_destination_rejects_symlink_escape(self):
        root = Path(os.path.abspath(str(self.fixture.cache)))
        target = root / "fixture-set" / "v1-abcdef0123456789" / "data" / "a.bin"
        outside = self.fixture.root / "outside"
        target.parent.mkdir(parents=True)
        def fake_realpath(value):
            return str(outside) if str(value) == str(target.parent) else str(value)

        def fake_is_symlink(path):
            return Path.__fspath__(path) == str(target.parent)

        with (
            mock.patch.object(module.Path, "is_symlink", new=fake_is_symlink),
            mock.patch.object(module.os.path, "realpath", new=fake_realpath),
        ):
            with self.assertRaises(module.ManifestValidationError) as raised:
                module._validate_destination(root, self.fixture.manifest, "data/a.bin")
        self.assertIn("symlink", str(raised.exception))

    def test_status_reports_corrupted_when_ancestor_symlink_escapes(self):
        root = Path(os.path.abspath(str(self.fixture.cache)))
        target_parent = root / "fixture-set" / "v1-abcdef0123456789" / "data"
        outside = self.fixture.root / "outside"

        def fake_is_symlink(path):
            return Path.__fspath__(path) == str(target_parent)

        def fake_realpath(value):
            return str(outside) if str(value) == str(target_parent) else str(value)

        with (
            mock.patch.object(module.Path, "is_symlink", new=fake_is_symlink),
            mock.patch.object(module.os.path, "realpath", new=fake_realpath),
        ):
            exit_code, report = self._run("status")
        self.assertEqual(0, exit_code)
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual("CORRUPTED", by_path["data/a.bin"]["state"])
        self.assertIn("symlink", by_path["data/a.bin"]["detail"])
        self.assertEqual("CORRUPTED", by_path["data/b.bin"]["state"])
        self.assertIn("symlink", by_path["data/b.bin"]["detail"])

    def test_status_reports_corrupted_for_broken_temporary_symlink(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        part = Path(str(target)).with_name("_acq-broken.part")
        # A real file on disk so the glob scan finds it; the mocked
        # is_symlink classifies it as an unsafe temporary path.
        part.write_bytes(b"")

        def fake_is_symlink(path):
            return Path.__fspath__(path) == str(part)

        with mock.patch.object(module.Path, "is_symlink", new=fake_is_symlink):
            exit_code, report = self._run("status")
        self.assertEqual(0, exit_code)
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual("CORRUPTED", by_path["data/a.bin"]["state"])
        self.assertIn("temporary path is unsafe", by_path["data/a.bin"]["detail"])
        # The unsafe temporary file lives in the shared data/ directory, so
        # both entries in that directory are conservatively classified.
        self.assertEqual("CORRUPTED", by_path["data/b.bin"]["state"])
        self.assertIn("temporary path is unsafe", by_path["data/b.bin"]["detail"])

    def test_status_reports_corrupted_for_internal_ancestor_symlink(self):
        root = Path(os.path.abspath(str(self.fixture.cache)))
        target_parent = root / "fixture-set" / "v1-abcdef0123456789" / "data"
        inside = root / "elsewhere"

        def fake_is_symlink(path):
            return Path.__fspath__(path) == str(target_parent)

        def fake_realpath(value):
            return str(inside) if str(value) == str(target_parent) else str(value)

        with (
            mock.patch.object(module.Path, "is_symlink", new=fake_is_symlink),
            mock.patch.object(module.os.path, "realpath", new=fake_realpath),
        ):
            exit_code, report = self._run("status")
        self.assertEqual(0, exit_code)
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual("CORRUPTED", by_path["data/a.bin"]["state"])
        self.assertIn("symlink", by_path["data/a.bin"]["detail"])
        self.assertEqual("CORRUPTED", by_path["data/b.bin"]["state"])
        self.assertIn("symlink", by_path["data/b.bin"]["detail"])
        with (
            mock.patch.object(module.Path, "is_symlink", new=fake_is_symlink),
            mock.patch.object(module.os.path, "realpath", new=fake_realpath),
        ):
            verify_code, verify_report = self._run("verify")
        self.assertEqual(4, verify_code)
        self.assertEqual(0, verify_report["network"]["requests_attempted"])

    def test_verified_final_with_unsafe_reserved_temp_is_corrupted(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        target.write_bytes(self.fixture.files["a.bin"])
        reserved = target.parent / "_acq-unsafe.part"
        reserved.write_bytes(b"")

        def fake_is_symlink(path):
            return Path.__fspath__(path) == str(reserved)

        with mock.patch.object(module.Path, "is_symlink", new=fake_is_symlink):
            exit_code, report = self._run("status")
        self.assertEqual(0, exit_code)
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual("CORRUPTED", by_path["data/a.bin"]["state"])
        self.assertIn("reserved temporary path is unsafe", by_path["data/a.bin"]["detail"])


    def test_public_verify_api_matches_cli(self):
        with mock.patch.object(module, "_opener", side_effect=AssertionError("network access")):
            cli_code, cli_report = self._run("verify")
        api_report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        self.assertEqual(cli_code, 4)
        self.assertEqual(api_report, cli_report)
        self.assertEqual(api_report["plan_id"], cli_report["plan_id"])
        self.assertEqual(api_report["file_counts"], cli_report["file_counts"])
        self.assertEqual(api_report["bytes"], cli_report["bytes"])
        self.assertEqual(api_report["fully_cached"], False)

    def test_parsed_verify_api_matches_path_based_api(self):
        parsed = module.parse_manifest(self.fixture.manifest_path)
        path_report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        parsed_report = module.verify_parsed_manifest_cache(
            parsed, self.fixture.cache
        )
        self.assertEqual(parsed_report, path_report)

    def test_public_verify_api_fully_verified_cache(self):
        for name, data in self.fixture.files.items():
            target = self.fixture.target(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        self.assertTrue(report["success"])
        self.assertEqual(report["classification"], "OK")
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["fully_cached"])
        self.assertEqual(report["file_counts"]["VERIFIED"], 2)
        self.assertEqual(report["bytes"]["required"], 0)

    def test_public_verify_api_absent_partial_size_and_sha(self):
        # Absent.
        report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        self.assertFalse(report["success"])
        self.assertEqual(report["file_counts"]["ABSENT"], 2)
        self.assertEqual(report["bytes"]["required"], 48)

        # Partial.
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_name("_acq-a1.part").write_bytes(b"partial")
        report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual(by_path["data/a.bin"]["state"], "PARTIAL")

        # Size mismatch.
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"short")
        report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual(by_path["data/a.bin"]["state"], "CORRUPTED")
        self.assertIn("size mismatch", by_path["data/a.bin"]["detail"])

        # SHA-256 mismatch.
        wrong = b"X" * len(self.fixture.files["a.bin"])
        target.write_bytes(wrong)
        report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual(by_path["data/a.bin"]["state"], "CORRUPTED")
        self.assertIn("SHA-256", by_path["data/a.bin"]["detail"])

    def test_public_verify_api_final_symlink_and_non_regular(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True, exist_ok=True)
        outside = self.fixture.root / "outside.bin"
        outside.write_bytes(self.fixture.files["a.bin"])
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual(by_path["data/a.bin"]["state"], "CORRUPTED")
        self.assertIn("symlink", by_path["data/a.bin"]["detail"])

        target.unlink()
        target.mkdir()
        report = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual(by_path["data/a.bin"]["state"], "CORRUPTED")
        self.assertIn("not a regular file", by_path["data/a.bin"]["detail"])

    def test_public_verify_api_symlink_ancestor_is_corrupted(self):
        root = Path(os.path.abspath(str(self.fixture.cache)))
        target_parent = root / "fixture-set" / "v1-abcdef0123456789" / "data"
        outside = self.fixture.root / "outside"

        def fake_is_symlink(path):
            return Path.__fspath__(path) == str(target_parent)

        def fake_realpath(value):
            return str(outside) if str(value) == str(target_parent) else str(value)

        with (
            mock.patch.object(module.Path, "is_symlink", new=fake_is_symlink),
            mock.patch.object(module.os.path, "realpath", new=fake_realpath),
        ):
            report = module.verify_manifest_cache(
                self.fixture.manifest_path, self.fixture.cache
            )
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual(by_path["data/a.bin"]["state"], "CORRUPTED")
        self.assertIn("symlink", by_path["data/a.bin"]["detail"])

    def test_public_verify_api_is_read_only_and_defensive(self):
        for name, data in self.fixture.files.items():
            target = self.fixture.target(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        with (
            mock.patch.object(module, "_opener", side_effect=AssertionError("network")),
            mock.patch.object(module.os, "replace", side_effect=AssertionError("write")),
            mock.patch.object(module.os, "mkdir", side_effect=AssertionError("write")),
            mock.patch.object(module.os, "makedirs", side_effect=AssertionError("write")),
            mock.patch.object(module._ArtifactLock, "acquire", side_effect=AssertionError("lock")),
        ):
            first = module.verify_manifest_cache(
                self.fixture.manifest_path, self.fixture.cache
            )
        first["files"].append({"path": "mutated", "state": "VERIFIED"})
        first["file_counts"]["VERIFIED"] = 999
        first["bytes"]["required"] = 999
        second = module.verify_manifest_cache(
            self.fixture.manifest_path, self.fixture.cache
        )
        self.assertEqual(second["file_counts"]["VERIFIED"], 2)
        self.assertEqual(second["bytes"]["required"], 0)
        self.assertEqual(len(second["files"]), 2)


if __name__ == "__main__":
    unittest.main()
