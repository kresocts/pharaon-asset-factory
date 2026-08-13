import hashlib
import importlib.util
import io
import json
import os
import tempfile
import time
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


class LockTests(unittest.TestCase):
    def setUp(self):
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

    def tearDown(self):
        self.temporary.cleanup()

    def _lock(self):
        return module._ArtifactLock(self.cache, module._plan_id(self.manifest))

    def _lock_dir(self):
        return self.cache / ".locks" / module._plan_id(self.manifest)

    def test_lock_lives_under_cache_root_and_is_released(self):
        lock = self._lock()
        lock.acquire()
        self.assertTrue(self._lock_dir().is_dir())
        self.assertTrue((self._lock_dir() / "owner.json").is_file())
        self.assertEqual(str(self._lock_dir()).startswith(str(self.cache)), True)
        lock.release()
        self.assertFalse(self._lock_dir().exists())

    def test_second_acquirer_conflicts_with_active_lock_without_infinite_wait(self):
        first = self._lock()
        first.acquire()
        second = self._lock()
        with (
            mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.2),
            mock.patch.object(module.time, "sleep", return_value=None),
        ):
            with self.assertRaises(module.LockConflictError) as raised:
                second.acquire()
        self.assertIn("another process holds", str(raised.exception))
        self.assertTrue(first.lock_dir.is_dir(), "active lock must not be broken")
        first.release()
        self.assertFalse(self._lock_dir().exists())

    def test_active_lock_is_never_broken_even_when_old(self):
        first = self._lock()
        first.acquire()
        old = time.time() - module.STALE_LOCK_GRACE_SECONDS - 60
        os.utime(first.owner_path, (old, old))
        with (
            mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.2),
            mock.patch.object(module.time, "sleep", return_value=None),
        ):
            with self.assertRaises(module.LockConflictError):
                self._lock().acquire()
        self.assertTrue(first.lock_dir.is_dir(), "an active lock with a live owner must stay intact")
        first.release()

    def test_stale_lock_with_dead_owner_is_broken_conservatively(self):
        lock_dir = self._lock_dir()
        lock_dir.mkdir(parents=True)
        owner = lock_dir / "owner.json"
        owner.write_text(
            json.dumps({"plan_id": module._plan_id(self.manifest), "pid": 99999999, "start_epoch": 0.0}),
            encoding="utf-8",
        )
        old = time.time() - module.STALE_LOCK_GRACE_SECONDS - 60
        os.utime(owner, (old, old))
        lock = self._lock()
        lock.acquire()
        self.assertTrue(lock.lock_dir.is_dir())
        lock.release()

    def test_ownerless_stale_lock_is_broken_after_grace(self):
        lock_dir = self._lock_dir()
        lock_dir.mkdir(parents=True)
        old = time.time() - module.STALE_LOCK_NO_OWNER_GRACE_SECONDS - 60
        os.utime(lock_dir, (old, old))
        lock = self._lock()
        lock.acquire()
        self.assertTrue(lock.lock_dir.is_dir())
        lock.release()

    def test_lock_conflict_is_a_clean_machine_readable_failure(self):
        first = self._lock()
        first.acquire()
        try:
            output = io.StringIO()
            with (
                mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.1),
                mock.patch.object(module.time, "sleep", return_value=None),
                redirect_stdout(output),
            ):
                exit_code = module.main(
                    [
                        "acquire",
                        "--manifest",
                        str(self.manifest_path),
                        "--confirm-download",
                        "--max-bytes",
                        "48",
                        "--json",
                    ],
                    {"MODEL_CACHE_DIR": str(self.cache)},
                )
            self.assertEqual(6, exit_code)
            payload = json.loads(output.getvalue())
            self.assertEqual("LOCK_CONFLICT", payload["classification"])
            self.assertFalse(payload["success"])
            self.assertEqual(0, payload["network"]["requests_attempted"])
            self.assertIn("another process holds", payload["detail"])
        finally:
            first.release()

    def test_lock_error_does_not_write_final_files(self):
        first = self._lock()
        first.acquire()
        try:
            with (
                mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.1),
                mock.patch.object(module.time, "sleep", return_value=None),
            ):
                module.main(
                    [
                        "acquire",
                        "--manifest",
                        str(self.manifest_path),
                        "--confirm-download",
                        "--max-bytes",
                        "48",
                    ],
                    {"MODEL_CACHE_DIR": str(self.cache)},
                )
            self.assertFalse(any(self.cache.rglob("*.bin")))
            self.assertFalse(any(self.cache.rglob("*.part")))
        finally:
            first.release()

    def test_lock_root_symlink_escape_is_refused(self):
        outside = self.root / "outside-locks"
        outside.mkdir()
        locks = self.cache / ".locks"
        try:
            locks.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        output = io.StringIO()
        with (
            mock.patch.object(module, "_download_file", side_effect=AssertionError("network access")),
            redirect_stdout(output),
        ):
            exit_code = module.main(
                [
                    "acquire",
                    "--manifest",
                    str(self.manifest_path),
                    "--confirm-download",
                    "--max-bytes",
                    "48",
                    "--json",
                ],
                {"MODEL_CACHE_DIR": str(self.cache)},
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", payload["classification"])
        self.assertIn("lock root is a symlink", payload["detail"])
        self.assertEqual(0, payload["network"]["requests_attempted"])
        self.assertEqual([], list(outside.iterdir()))
        self.assertFalse(any(self.cache.rglob("*.bin")))

    def test_lock_root_non_directory_is_refused(self):
        (self.cache / ".locks").write_text("not a directory", encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = module.main(
                [
                    "acquire",
                    "--manifest",
                    str(self.manifest_path),
                    "--confirm-download",
                    "--max-bytes",
                    "48",
                    "--json",
                ],
                {"MODEL_CACHE_DIR": str(self.cache)},
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", payload["classification"])
        self.assertIn("not a directory", payload["detail"])
        self.assertEqual(0, payload["network"]["requests_attempted"])
        self.assertFalse(any(self.cache.rglob("*.bin")))


if __name__ == "__main__":
    unittest.main()