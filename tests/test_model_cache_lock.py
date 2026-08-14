import hashlib
import importlib.util
import io
import json
import os
import tempfile
import threading
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
        return module._ArtifactLock(self.cache, module._lock_key(self.manifest), module._plan_id(self.manifest))

    def _lock_dir(self):
        return self.cache / ".locks" / module._lock_key(self.manifest)

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

    def test_stale_lock_with_dead_owner_is_not_auto_removed(self):
        lock_dir = self._lock_dir()
        lock_dir.mkdir(parents=True)
        owner = lock_dir / "owner.json"
        owner.write_text(
            json.dumps({"plan_id": module._plan_id(self.manifest), "pid": 99999999, "start_epoch": 0.0}),
            encoding="utf-8",
        )
        old = time.time() - module.STALE_LOCK_GRACE_SECONDS - 60
        os.utime(owner, (old, old))
        with (
            mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.2),
            mock.patch.object(module.time, "sleep", return_value=None),
        ):
            outcome, elapsed = self._run_acquire_with_timeout(self._lock())
        self.assertEqual("conflict", outcome["result"])
        self.assertLess(elapsed, 2.0)
        self.assertTrue(lock_dir.is_dir(), "stale lock must not be auto-removed")
        self.assertTrue(owner.is_file())
        self.assertEqual(99999999, json.loads(owner.read_text(encoding="utf-8"))["pid"])

    def test_ownerless_stale_lock_is_not_auto_removed(self):
        lock_dir = self._lock_dir()
        lock_dir.mkdir(parents=True)
        old = time.time() - module.STALE_LOCK_NO_OWNER_GRACE_SECONDS - 60
        os.utime(lock_dir, (old, old))
        with (
            mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.2),
            mock.patch.object(module.time, "sleep", return_value=None),
        ):
            outcome, elapsed = self._run_acquire_with_timeout(self._lock())
        self.assertEqual("conflict", outcome["result"])
        self.assertLess(elapsed, 2.0)
        self.assertTrue(lock_dir.is_dir(), "ownerless stale lock must not be auto-removed")

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
            self.assertEqual(0, payload["network"]["bytes_received"])
            self.assertIn("another process holds", payload["detail"])
            self.assertEqual("fixture-set", payload["artifact_set"])
            self.assertEqual("v1-abcdef0123456789", payload["revision"])
            self.assertEqual("fixture-set/v1-abcdef0123456789", payload["namespace"])
            self.assertEqual(module._plan_id(self.manifest), payload["plan_id"])
            self.assertEqual(str(self.cache), payload["cache_root"])
            self.assertEqual(2, payload["file_count"])
            self.assertEqual(
                {"ABSENT": 2, "PARTIAL": 0, "CORRUPTED": 0, "VERIFIED": 0},
                payload["file_counts"],
            )
            self.assertEqual(48, payload["bytes"]["total_expected"])
            self.assertEqual(48, payload["bytes"]["required"])
            self.assertEqual(2, len(payload["files"]))
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

    def test_different_manifests_same_destination_share_lock_key(self):
        other = json.loads(json.dumps(self.manifest))
        other["files"] = [
            dict(other["files"][0], sha256=hashlib.sha256(b"other-a").hexdigest(), role="other-a"),
            dict(other["files"][1], sha256=hashlib.sha256(b"other-b").hexdigest(), url="https://other.invalid/b.bin"),
        ]
        self.assertNotEqual(module._plan_id(self.manifest), module._plan_id(other))
        self.assertEqual(module._lock_key(self.manifest), module._lock_key(other))

    def test_namespace_prefix_manifests_share_lock_key(self):
        first = dict(
            self.manifest,
            namespace="fixture-set",
            files=[dict(self.manifest["files"][0], path="v1/x.bin")],
        )
        second = dict(
            self.manifest,
            namespace="fixture-set/v1",
            files=[dict(self.manifest["files"][0], path="x.bin")],
        )
        self.assertEqual(module._lock_key(first), module._lock_key(second))
        self.assertNotEqual(module._plan_id(first), module._plan_id(second))

    def _run_acquire_with_timeout(self, lock, timeout=5.0):
        """Run acquire in a daemon thread so an infinite-loop regression fails."""
        outcome = {}

        def worker():
            try:
                lock.acquire()
                outcome["result"] = "acquired"
            except module.LockConflictError as error:
                outcome["result"] = "conflict"
                outcome["message"] = str(error)
            except Exception as error:  # pragma: no cover - unexpected
                outcome["result"] = type(error).__name__
                outcome["message"] = str(error)

        thread = threading.Thread(target=worker, daemon=True)
        start = time.perf_counter()
        thread.start()
        thread.join(timeout)
        elapsed = time.perf_counter() - start
        self.assertFalse(thread.is_alive(), "acquire must terminate within the bounded wait")
        return outcome, elapsed

    def test_unremovable_stale_owner_path_is_bounded(self):
        lock_dir = self._lock_dir()
        lock_dir.mkdir(parents=True)
        owner = lock_dir / "owner.json"
        owner.mkdir()  # owner.json as a directory is unremovable without recursion
        old = time.time() - module.STALE_LOCK_GRACE_SECONDS - 60
        os.utime(lock_dir, (old, old))
        os.utime(owner, (old, old))
        fake_time = {"now": 0.0}
        sleep_calls = {"count": 0}

        def fake_monotonic():
            return fake_time["now"]

        def fake_sleep(_):
            sleep_calls["count"] += 1
            fake_time["now"] += module.LOCK_POLL_INTERVAL

        with (
            mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.3),
            mock.patch.object(module.time, "monotonic", side_effect=fake_monotonic),
            mock.patch.object(module.time, "sleep", side_effect=fake_sleep),
        ):
            outcome, elapsed = self._run_acquire_with_timeout(self._lock())
        self.assertEqual("conflict", outcome["result"])
        self.assertIn("another process holds", outcome["message"])
        self.assertLess(elapsed, 2.0, "acquire must return within the bounded wait")
        self.assertGreaterEqual(sleep_calls["count"], 1, "poll sleep must occur")
        self.assertLessEqual(sleep_calls["count"], 10, "no busy loop without polling")
        self.assertTrue(lock_dir.is_dir(), "unremovable lock remnants must stay untouched")
        self.assertTrue(owner.is_dir(), "owner.json directory must stay untouched")

    def test_stale_observation_followed_by_active_replacement_is_safe(self):
        lock_dir = self._lock_dir()
        lock_dir.mkdir(parents=True)
        owner_path = lock_dir / "owner.json"
        owner_path.write_text(
            json.dumps({"plan_id": "old", "pid": 99999999, "start_epoch": 0.0}),
            encoding="utf-8",
        )
        old = time.time() - module.STALE_LOCK_GRACE_SECONDS - 60
        os.utime(owner_path, (old, old))
        replacement = {
            "lock_key": "fixture-set",
            "plan_id": "replacement",
            "owner_token": "replacement-token",
            "pid": 1,
            "start_epoch": time.time(),
        }
        replacement_bytes = json.dumps(replacement, sort_keys=True)
        original_stale = module._ArtifactLock._stale
        replaced = {"done": False}

        def observe_then_replace(_self, observed_dir):
            result = original_stale(observed_dir)
            if result and not replaced["done"]:
                replaced["done"] = True
                owner_path.write_text(replacement_bytes, encoding="utf-8")
            return original_stale(observed_dir)

        fake_time = {"now": 0.0}

        def fake_monotonic():
            return fake_time["now"]

        def fake_sleep(_):
            fake_time["now"] += module.LOCK_POLL_INTERVAL

        with (
            mock.patch.object(module._ArtifactLock, "_stale", new=observe_then_replace),
            mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.3),
            mock.patch.object(module.time, "monotonic", side_effect=fake_monotonic),
            mock.patch.object(module.time, "sleep", side_effect=fake_sleep),
        ):
            outcome, elapsed = self._run_acquire_with_timeout(self._lock())
        self.assertEqual("conflict", outcome["result"])
        self.assertLess(elapsed, 2.0)
        self.assertTrue(replaced["done"], "the stale observation must occur")
        self.assertEqual(replacement_bytes, owner_path.read_text(encoding="utf-8"))
        self.assertTrue(lock_dir.is_dir())

    def test_old_owner_release_after_replacement_is_safe(self):
        lock = self._lock()
        lock.acquire()
        lock_dir = lock.lock_dir
        replacement = {
            "lock_key": "fixture-set",
            "plan_id": "replacement",
            "owner_token": "replacement-token",
            "pid": 1,
            "start_epoch": 0.0,
        }
        replacement_bytes = json.dumps(replacement, sort_keys=True)
        (lock_dir / "owner.json").write_text(replacement_bytes, encoding="utf-8")
        lock.release()
        self.assertTrue(lock_dir.is_dir(), "replacement lock must survive release")
        self.assertEqual(replacement_bytes, (lock_dir / "owner.json").read_text(encoding="utf-8"))

    def test_old_owner_touch_after_replacement_is_safe(self):
        lock = self._lock()
        lock.acquire()
        lock_dir = lock.lock_dir
        owner_path = lock_dir / "owner.json"
        replacement = {
            "lock_key": "fixture-set",
            "plan_id": "replacement",
            "owner_token": "replacement-token",
            "pid": 1,
            "start_epoch": 0.0,
        }
        replacement_bytes = json.dumps(replacement, sort_keys=True)
        owner_path.write_text(replacement_bytes, encoding="utf-8")
        old_mtime = time.time() - 100
        os.utime(owner_path, (old_mtime, old_mtime))
        before_ns = owner_path.stat().st_mtime_ns
        lock.touch()
        self.assertEqual(before_ns, owner_path.stat().st_mtime_ns)
        self.assertEqual(replacement_bytes, owner_path.read_text(encoding="utf-8"))

    def test_owner_touch_and_release_work_for_current_owner(self):
        lock = self._lock()
        lock.acquire()
        lock_dir = lock.lock_dir
        owner_path = lock_dir / "owner.json"
        self.assertTrue(lock._owns_lock())
        time.sleep(0.01)
        before_ns = owner_path.stat().st_mtime_ns
        lock.touch()
        self.assertGreaterEqual(owner_path.stat().st_mtime_ns, before_ns)
        lock.release()
        self.assertFalse(lock_dir.exists())

    def test_concurrent_observers_never_delete_active_generations(self):
        results = []

        def worker():
            try:
                self._lock().acquire()
                results.append("acquired")
            except module.LockConflictError:
                results.append("conflict")

        def run_pair():
            results.clear()
            with (
                mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.3),
                mock.patch.object(module.time, "sleep", return_value=None),
            ):
                threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
            for thread in threads:
                self.assertFalse(thread.is_alive(), "no infinite loop")

        # Phase 1: two acquirers race on an empty lock root. Exactly one
        # acquires; the loser returns bounded LOCK_CONFLICT and the winner's
        # generation survives untouched.
        run_pair()
        self.assertEqual(1, results.count("acquired"))
        self.assertEqual(1, results.count("conflict"))
        winner_owner = self._lock_dir() / "owner.json"
        self.assertTrue(winner_owner.is_file(), "winner lock must survive")
        stale_bytes = winner_owner.read_text(encoding="utf-8")
        # Phase 2: make the surviving generation look stale. With automatic
        # stale removal disabled, both observers conflict bounded and the
        # stale generation stays byte-identical (no deletion race exists).
        old = time.time() - module.STALE_LOCK_GRACE_SECONDS - 60
        os.utime(winner_owner, (old, old))
        run_pair()
        self.assertEqual(0, results.count("acquired"))
        self.assertEqual(2, results.count("conflict"))
        self.assertEqual(stale_bytes, winner_owner.read_text(encoding="utf-8"))

    def test_existing_stale_lock_is_bounded_and_never_deleted(self):
        lock_dir = self._lock_dir()
        lock_dir.mkdir(parents=True)
        owner = lock_dir / "owner.json"
        owner.write_text(
            json.dumps({"plan_id": "x", "pid": 99999999, "start_epoch": 0.0}),
            encoding="utf-8",
        )
        old = time.time() - module.STALE_LOCK_GRACE_SECONDS - 60
        os.utime(owner, (old, old))
        fake_time = {"now": 0.0}
        sleep_calls = {"count": 0}

        def fake_monotonic():
            return fake_time["now"]

        def fake_sleep(_):
            sleep_calls["count"] += 1
            fake_time["now"] += module.LOCK_POLL_INTERVAL

        with (
            mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.3),
            mock.patch.object(module.time, "monotonic", side_effect=fake_monotonic),
            mock.patch.object(module.time, "sleep", side_effect=fake_sleep),
        ):
            outcome, elapsed = self._run_acquire_with_timeout(self._lock())
        self.assertEqual("conflict", outcome["result"])
        self.assertIn("manual operator action", outcome["message"])
        self.assertLess(elapsed, 2.0)
        self.assertGreaterEqual(sleep_calls["count"], 1)
        self.assertLessEqual(sleep_calls["count"], 10)
        self.assertTrue(lock_dir.is_dir())
        self.assertTrue(owner.is_file())


if __name__ == "__main__":
    unittest.main()