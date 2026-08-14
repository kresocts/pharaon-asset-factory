import hashlib
import http.server
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
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


class _FixtureServer:
    """Tiny local HTTP server that counts requests and serves fixture bytes."""

    def __init__(self, files):
        self.files = dict(files)
        self.requests = []
        self._failures = {}
        self._redirects = {}
        self._partials = {}
        self._no_lengths = {}
        self._chunked = {}
        self._redirect_bodies = {}
        handler = self._handler()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def _handler(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append(self.path)
                name = self.path.lstrip("/")
                if name in server._redirects:
                    location = server._redirects.pop(name)
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.end_headers()
                    return
                if name in server._redirect_bodies:
                    location, body = server._redirect_bodies.pop(name)
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if name in server._no_lengths:
                    server._no_lengths.pop(name)
                    data = server.files.get(name)
                    if data is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                    self.close_connection = True
                    return
                if name in server._chunked:
                    server._chunked.pop(name)
                    data = server.files.get(name)
                    if data is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n0\r\n\r\n")
                    return
                if name in server._partials:
                    n = server._partials.pop(name)
                    data = server.files.get(name)
                    if data is None:
                        self.send_error(404)
                        return
                    # Send a Content-Length body of only the first n bytes,
                    # then close cleanly so the client receives n bytes and
                    # then a premature-EOF retryable transport error.
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data[:n])
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.close()
                    return
                if name in server._failures:
                    status = server._failures.pop(name)
                    self.send_error(status)
                    return
                if name not in server.files:
                    self.send_error(404)
                    return
                data = server.files[name]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                pass

        return Handler

    def fail_next(self, name, status):
        self._failures[name] = status

    def redirect_next(self, name, location):
        self._redirects[name] = location

    def fail_partial_next(self, name, bytes_to_send):
        self._partials[name] = bytes_to_send

    def no_content_length_next(self, name):
        self._no_lengths[name] = True

    def chunked_next(self, name):
        self._chunked[name] = True

    def redirect_with_body_next(self, name, location, body):
        self._redirect_bodies[name] = (location, body)

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class _Fixture:
    def __init__(self, files=None):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache = self.root / "models"
        self.cache.mkdir()
        self.files = files if files is not None else {
            "a.bin": _content("alpha", 16),
            "b.bin": _content("beta", 32),
        }
        self.server = _FixtureServer(self.files)
        self.server.start()
        manifest_files = []
        for name, data in self.files.items():
            manifest_files.append(
                {
                    "path": f"data/{name}",
                    "url": f"http://127.0.0.1:{self.server.port}/{name}",
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
        self.write_manifest(self.manifest)

    def write_manifest(self, data):
        self.manifest_path.write_text(json.dumps(data), encoding="utf-8")

    def target(self, name):
        return self.cache / "fixture-set" / "v1-abcdef0123456789" / "data" / name

    def cleanup(self):
        self.server.stop()
        self.temporary.cleanup()


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _Fixture()

    def tearDown(self):
        self.fixture.cleanup()

    def _acquire(self, *extra, max_bytes=1048576, confirm=True, environment=None):
        args = ["acquire", "--manifest", str(self.fixture.manifest_path)]
        if confirm:
            args.append("--confirm-download")
        if max_bytes is not None:
            args.extend(["--max-bytes", str(max_bytes)])
        args.append("--json")
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = module.main(
                args,
                dict(self.environment() if environment is None else environment),
            )
        return exit_code, json.loads(output.getvalue())

    def environment(self):
        return {"MODEL_CACHE_DIR": str(self.fixture.cache)}

    def test_successful_fixture_acquisition_verifies_and_promotes_atomically(self):
        exit_code, report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        self.assertEqual("OK", report["classification"])
        self.assertEqual(2, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertEqual(48, report["bytes"]["downloaded"])
        for name, data in self.fixture.files.items():
            target = self.fixture.target(name)
            self.assertTrue(target.is_file())
            self.assertEqual(data, target.read_bytes())
            self.assertFalse(Path(str(target) + ".part").exists())
        for entry in report["files"]:
            self.assertEqual("VERIFIED", entry["state"])
            self.assertTrue(entry["downloaded"])
            self.assertEqual("downloaded", entry["action"])
        self.assertEqual(2, len(self.fixture.server.requests))

    def test_acquisition_with_exact_byte_budget_succeeds(self):
        exit_code, report = self._acquire(max_bytes=48)
        self.assertEqual(0, exit_code)
        self.assertEqual(48, report["bytes"]["max_bytes"])
        self.assertEqual(2, len(self.fixture.server.requests))

    def test_missing_confirmation_is_a_policy_refusal_with_zero_requests(self):
        exit_code, report = self._acquire(confirm=False)
        self.assertEqual(2, exit_code)
        self.assertFalse(report["success"])
        self.assertEqual("POLICY_REFUSAL", report["classification"])
        self.assertIn("--confirm-download", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertFalse(any(self.fixture.cache.rglob("*.bin")))

    def test_missing_byte_limit_is_a_policy_refusal_with_zero_requests(self):
        exit_code, report = self._acquire(max_bytes=None)
        self.assertEqual(2, exit_code)
        self.assertEqual("POLICY_REFUSAL", report["classification"])
        self.assertIn("--max-bytes", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertFalse(any(self.fixture.cache.rglob("*.bin")))

    def test_insufficient_byte_limit_is_a_policy_refusal_with_zero_requests(self):
        exit_code, report = self._acquire(max_bytes=47)
        self.assertEqual(2, exit_code)
        self.assertEqual("POLICY_REFUSAL", report["classification"])
        self.assertIn("47", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertFalse(any(self.fixture.cache.rglob("*.bin")))

    def test_negative_byte_limit_is_invalid_usage(self):
        exit_code, report = self._acquire(max_bytes=-1)
        self.assertEqual(64, exit_code)
        self.assertEqual("INVALID_REQUEST", report["classification"])
        self.assertEqual(0, report["network"]["requests_attempted"])

    def test_post_lock_budget_recheck_refuses_when_cache_invalidated(self):
        # Preflight sees a fully verified cache, so --max-bytes 0 is accepted.
        for name, data in self.fixture.files.items():
            target = self.fixture.target(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        original_acquire = module._ArtifactLock.acquire

        def invalidate_then_acquire(lock):
            # Simulate a verified file disappearing while the lock is acquired.
            self.fixture.target("a.bin").unlink()
            return original_acquire(lock)

        with (
            mock.patch.object(module._ArtifactLock, "acquire", new=invalidate_then_acquire),
            mock.patch.object(module, "_download_file", side_effect=AssertionError("network access")) as download,
        ):
            exit_code, report = self._acquire(max_bytes=0)
        self.assertEqual(2, exit_code)
        self.assertEqual("POLICY_REFUSAL", report["classification"])
        download.assert_not_called()
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))
        by_path = {entry["path"]: entry for entry in report["files"]}
        self.assertEqual("ABSENT", by_path["data/a.bin"]["state"])
        self.assertEqual("VERIFIED", by_path["data/b.bin"]["state"])
        self.assertEqual(16, report["bytes"]["required"])
        self.assertEqual(0, report["bytes"]["max_bytes"])

    def test_cache_reuse_makes_zero_additional_requests_and_does_not_rewrite(self):
        first_exit, first_report = self._acquire()
        self.assertEqual(0, first_exit)
        targets = {name: self.fixture.target(name) for name in self.fixture.files}
        mtimes = {name: target.stat().st_mtime_ns for name, target in targets.items()}
        requests_after_first = list(self.fixture.server.requests)

        second_exit, second_report = self._acquire()
        self.assertEqual(0, second_exit)
        self.assertTrue(second_report["success"])
        self.assertEqual(0, second_report["network"]["requests_attempted"])
        self.assertEqual(requests_after_first, self.fixture.server.requests)
        for entry in second_report["files"]:
            self.assertEqual("VERIFIED", entry["state"])
            self.assertEqual("reused", entry["action"])
        self.assertEqual(0, second_report["bytes"]["required"])
        for name, target in targets.items():
            self.assertEqual(mtimes[name], target.stat().st_mtime_ns)
            self.assertEqual(self.fixture.files[name], target.read_bytes())

    def test_checksum_mismatch_is_an_integrity_failure_without_final_file(self):
        manifest = dict(self.fixture.manifest)
        manifest["files"][0]["sha256"] = hashlib.sha256(b"wrong").hexdigest()
        self.fixture.write_manifest(manifest)
        exit_code, report = self._acquire()
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_size_mismatch_is_an_integrity_failure(self):
        manifest = dict(self.fixture.manifest)
        manifest["files"][0]["size"] = len(self.fixture.files["a.bin"]) + 5
        self.fixture.write_manifest(manifest)
        exit_code, report = self._acquire()
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertFalse(self.fixture.target("a.bin").exists())

    def test_oversized_response_is_an_integrity_failure(self):
        manifest = dict(self.fixture.manifest)
        manifest["files"][0]["size"] = len(self.fixture.files["a.bin"]) - 4
        self.fixture.write_manifest(manifest)
        exit_code, report = self._acquire()
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_stale_partial_file_is_replaced_by_fresh_download(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        part = Path(str(target)).with_name("_acq-stale.part")
        part.write_bytes(b"stale partial bytes")
        exit_code, report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertEqual(self.fixture.files["a.bin"], target.read_bytes())
        self.assertFalse(part.exists())
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))
        self.assertEqual(2, len(self.fixture.server.requests))
        self.assertTrue(report["files"][0]["downloaded"])

    def test_corrupted_final_file_is_replaced_after_verified_download(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"corrupted existing content")
        exit_code, report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertEqual(self.fixture.files["a.bin"], target.read_bytes())
        self.assertEqual(2, len(self.fixture.server.requests))
        self.assertTrue(report["files"][0]["downloaded"])

    def test_transient_failure_is_retried_with_bounded_retries(self):
        self.fixture.server.fail_next("a.bin", 503)
        with mock.patch.object(module.time, "sleep", return_value=None):
            exit_code, report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertEqual(3, report["network"]["requests_attempted"])
        self.assertEqual(1, report["network"]["retries"])
        self.assertEqual(self.fixture.files["a.bin"], self.fixture.target("a.bin").read_bytes())

    def test_permanent_http_error_is_not_retried(self):
        self.fixture.server.fail_next("a.bin", 404)
        exit_code, report = self._acquire()
        self.assertEqual(5, exit_code)
        self.assertEqual("TRANSPORT_FAILURE", report["classification"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertFalse(self.fixture.target("a.bin").exists())

    def test_connection_refusal_is_a_transport_failure_with_bounded_retries(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        manifest = dict(self.fixture.manifest)
        manifest["files"][0]["url"] = f"http://127.0.0.1:{port}/x.bin"
        manifest["files"][0]["size"] = 1
        manifest["files"][0]["sha256"] = hashlib.sha256(b"x").hexdigest()
        self.fixture.write_manifest(manifest)
        with mock.patch.object(module.time, "sleep", return_value=None):
            exit_code, report = self._acquire()
        self.assertEqual(5, exit_code)
        self.assertEqual("TRANSPORT_FAILURE", report["classification"])
        self.assertEqual(3, report["network"]["requests_attempted"])
        self.assertEqual(2, report["network"]["retries"])
        self.assertFalse(self.fixture.target("a.bin").exists())

    def test_streaming_writes_via_unique_temp_and_uses_atomic_replace(self):
        exit_code, _report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))
        for name in self.fixture.files:
            self.assertTrue(self.fixture.target(name).is_file())

    def test_acquire_without_manifest_is_manifest_failure(self):
        self.fixture.manifest_path.unlink()
        exit_code, report = self._acquire()
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", report["classification"])

    def test_symlink_destination_is_a_path_security_failure(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        outside = self.fixture.root / "outside.bin"
        outside.write_bytes(b"secret")
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        exit_code, report = self._acquire()
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", report["classification"])
        self.assertIn("symlink", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)

    def test_directory_at_destination_is_a_path_security_failure(self):
        target = self.fixture.target("a.bin")
        target.mkdir(parents=True)
        exit_code, report = self._acquire()
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", report["classification"])
        self.assertIn("not a regular file", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_refused_redirect_is_classified_transport_failure(self):
        self.fixture.server.redirect_next("a.bin", "http://example.invalid/evil.bin")
        exit_code, report = self._acquire()
        self.assertEqual(5, exit_code)
        self.assertEqual("TRANSPORT_FAILURE", report["classification"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertEqual(["/a.bin"], self.fixture.server.requests)
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_allowed_loopback_redirect_succeeds(self):
        self.fixture.server.redirect_next(
            "a.bin", f"http://127.0.0.1:{self.fixture.server.port}/a.bin"
        )
        exit_code, report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        for name, data in self.fixture.files.items():
            self.assertEqual(data, self.fixture.target(name).read_bytes())
        self.assertEqual(3, len(self.fixture.server.requests))
        self.assertEqual(3, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertEqual(48, report["network"]["bytes_received"])

    def test_acquire_refuses_when_ancestor_symlink_escapes(self):
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
            exit_code, report = self._acquire()
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", report["classification"])
        self.assertIn("symlink", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)

    def test_broken_temporary_symlink_is_refused(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        outside = self.fixture.root / "outside-target.bin"
        part = target.parent / "_acq-attacker.part"
        try:
            part.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        exit_code, report = self._acquire()
        self.assertEqual(70, exit_code)
        self.assertEqual("LOCAL_IO_FAILURE", report["classification"])
        self.assertIn("reserved temporary path is unsafe", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertFalse(outside.exists(), "outside target must not be created through the symlink")
        self.assertFalse(target.exists())
        self.assertTrue(part.is_symlink())

    def test_temporary_symlink_is_refused(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        outside = self.fixture.root / "outside-target.bin"
        outside.write_bytes(b"outside payload")
        part = target.parent / "_acq-attacker.part"
        try:
            part.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        exit_code, report = self._acquire()
        self.assertEqual(70, exit_code)
        self.assertEqual("LOCAL_IO_FAILURE", report["classification"])
        self.assertIn("reserved temporary path is unsafe", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertEqual(b"outside payload", outside.read_bytes())
        self.assertFalse(target.exists())
        self.assertTrue(part.is_symlink())

    def test_unsafe_reserved_temp_directory_is_refused(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        reserved = target.parent / "_acq-stale.part"
        reserved.mkdir()
        output = io.StringIO()
        with redirect_stdout(output):
            status_code = module.main(
                ["status", "--manifest", str(self.fixture.manifest_path), "--json"],
                {"MODEL_CACHE_DIR": str(self.fixture.cache)},
            )
        status_report = json.loads(output.getvalue())
        self.assertEqual(0, status_code)
        self.assertEqual("CORRUPTED", status_report["files"][0]["state"])
        self.assertIn("reserved temporary path is unsafe", status_report["files"][0]["detail"])
        output = io.StringIO()
        with redirect_stdout(output):
            verify_code = module.main(
                ["verify", "--manifest", str(self.fixture.manifest_path), "--json"],
                {"MODEL_CACHE_DIR": str(self.fixture.cache)},
            )
        verify_report = json.loads(output.getvalue())
        self.assertEqual(4, verify_code)
        exit_code, report = self._acquire()
        self.assertEqual(70, exit_code)
        self.assertEqual("LOCAL_IO_FAILURE", report["classification"])
        self.assertIn("reserved temporary path is unsafe", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertFalse(report["fully_cached"])
        self.assertFalse(any(self.fixture.cache.rglob("*.bin")))

    def test_safe_stale_regular_temp_is_cleaned(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        reserved = target.parent / "_acq-stale.part"
        reserved.write_bytes(b"stale")
        exit_code, report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        self.assertTrue(report["fully_cached"])
        self.assertFalse(reserved.exists())
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))
        self.assertEqual(self.fixture.files["a.bin"], self.fixture.target("a.bin").read_bytes())

    def test_stale_temp_cleanup_failure_is_not_success(self):
        target = self.fixture.target("a.bin")
        target.parent.mkdir(parents=True)
        target.write_bytes(self.fixture.files["a.bin"])
        reserved = target.parent / "_acq-stale.part"
        reserved.write_bytes(b"stale")
        original_unlink = module.Path.unlink

        def failing_unlink(self):
            if self.name == "_acq-stale.part":
                raise OSError(13, "Permission denied")
            return original_unlink(self)

        with mock.patch.object(module.Path, "unlink", new=failing_unlink):
            exit_code, report = self._acquire()
        self.assertEqual(70, exit_code)
        self.assertEqual("LOCAL_IO_FAILURE", report["classification"])
        self.assertIn("reserved temporary file", report["detail"])
        self.assertFalse(report["success"])
        self.assertEqual(self.fixture.files["a.bin"], self.fixture.target("a.bin").read_bytes())
        self.assertTrue(reserved.exists())

    def test_final_zero_temporary_invariant(self):
        self.fixture.server.files["one.bin"] = b"x"
        manifest = {
            "schema_version": 1,
            "artifact_set": "zero-temp-set",
            "revision": "v1-abcdef0123456789",
            "namespace": "zero-temp-set/v1-abcdef0123456789",
            "files": [
                {
                    "path": "one.bin",
                    "url": f"http://127.0.0.1:{self.fixture.server.port}/one.bin",
                    "size": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
            ],
        }
        self.fixture.write_manifest(manifest)
        exit_code, report = self._acquire(max_bytes=1)
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        self.assertTrue(report["fully_cached"])
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))
        for entry in report["files"]:
            self.assertEqual("VERIFIED", entry["state"])

    def test_final_temp_collision_paths_acquire_safely(self):
        self.fixture.server.files["foo.part"] = b"part-content"
        self.fixture.server.files["foo"] = b"final-content"
        manifest = {
            "schema_version": 1,
            "artifact_set": "collision-set",
            "revision": "v1-abcdef0123456789",
            "namespace": "collision-set/v1-abcdef0123456789",
            "files": [
                {
                    "path": "foo.part",
                    "url": f"http://127.0.0.1:{self.fixture.server.port}/foo.part",
                    "size": len(b"part-content"),
                    "sha256": hashlib.sha256(b"part-content").hexdigest(),
                },
                {
                    "path": "foo",
                    "url": f"http://127.0.0.1:{self.fixture.server.port}/foo",
                    "size": len(b"final-content"),
                    "sha256": hashlib.sha256(b"final-content").hexdigest(),
                },
            ],
        }
        self.fixture.write_manifest(manifest)
        exit_code, report = self._acquire(max_bytes=100)
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        base = self.fixture.cache / "collision-set" / "v1-abcdef0123456789"
        self.assertEqual(b"part-content", (base / "foo.part").read_bytes())
        self.assertEqual(b"final-content", (base / "foo").read_bytes())
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))
        states = {entry["path"]: entry["state"] for entry in report["files"]}
        self.assertEqual({"foo.part": "VERIFIED", "foo": "VERIFIED"}, states)

    def test_ancestor_destination_conflict_is_refused_before_network(self):
        manifest = dict(self.fixture.manifest)
        manifest["files"].append(dict(manifest["files"][0], path="data", role=None))
        self.fixture.write_manifest(manifest)
        exit_code, report = self._acquire()
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", report["classification"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)

    def test_final_under_lock_verification_failure_is_not_success(self):
        original_collect = module._collect_states
        calls = {"count": 0}

        def flaky_collect(cache_root, manifest):
            result = original_collect(cache_root, manifest)
            calls["count"] += 1
            if calls["count"] >= 3:
                result[0] = dict(result[0], state="CORRUPTED", detail="simulated verification failure")
            return result

        with mock.patch.object(module, "_collect_states", new=flaky_collect):
            exit_code, report = self._acquire()
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertIn("final verification failed", report["detail"])
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))

    def test_redirect_overhead_leaves_insufficient_final_budget(self):
        payload = b"0123456789"
        self.fixture.server.files["one.bin"] = payload
        manifest = {
            "schema_version": 1,
            "artifact_set": "budget-set",
            "revision": "v1-abcdef0123456789",
            "namespace": "budget-set/v1-abcdef0123456789",
            "files": [
                {
                    "path": "one.bin",
                    "url": f"http://127.0.0.1:{self.fixture.server.port}/one.bin",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        }
        self.fixture.write_manifest(manifest)
        self.fixture.server.redirect_with_body_next(
            "one.bin", f"http://127.0.0.1:{self.fixture.server.port}/one.bin", b"r" * 5
        )
        exit_code, report = self._acquire(max_bytes=10)
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertIn("remaining allowance", report["detail"])
        self.assertEqual(5, report["network"]["bytes_received"])
        self.assertEqual(2, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        final_path = self.fixture.cache / "budget-set" / "v1-abcdef0123456789" / "one.bin"
        self.assertFalse(final_path.exists())
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))
        self.assertEqual(["/one.bin", "/one.bin"], self.fixture.server.requests)

    def test_redirect_overhead_with_sufficient_final_budget(self):
        payload = b"0123456789"
        self.fixture.server.files["one.bin"] = payload
        manifest = {
            "schema_version": 1,
            "artifact_set": "budget-set",
            "revision": "v1-abcdef0123456789",
            "namespace": "budget-set/v1-abcdef0123456789",
            "files": [
                {
                    "path": "one.bin",
                    "url": f"http://127.0.0.1:{self.fixture.server.port}/one.bin",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        }
        self.fixture.write_manifest(manifest)
        self.fixture.server.redirect_with_body_next(
            "one.bin", f"http://127.0.0.1:{self.fixture.server.port}/one.bin", b"r" * 5
        )
        exit_code, report = self._acquire(max_bytes=15)
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        self.assertEqual(15, report["network"]["bytes_received"])
        self.assertEqual(2, report["network"]["requests_attempted"])
        final_path = self.fixture.cache / "budget-set" / "v1-abcdef0123456789" / "one.bin"
        self.assertEqual(payload, final_path.read_bytes())

    def test_multi_file_remaining_budget_is_not_consumed(self):
        port = self.fixture.server.port
        self.fixture.server.redirect_with_body_next(
            "a.bin", f"http://127.0.0.1:{port}/a.bin", b"r" * 8
        )
        exit_code, report = self._acquire(max_bytes=55)
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertEqual(24, report["network"]["bytes_received"])
        self.assertEqual(3, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertEqual(self.fixture.files["a.bin"], self.fixture.target("a.bin").read_bytes())
        self.assertFalse(self.fixture.target("b.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))

    def test_redirect_body_exceeding_allowance_is_budgeted(self):
        self.fixture.server.files["one.bin"] = b"x"
        manifest = {
            "schema_version": 1,
            "artifact_set": "redirect-set",
            "revision": "v1-abcdef0123456789",
            "namespace": "redirect-set/v1-abcdef0123456789",
            "files": [
                {
                    "path": "one.bin",
                    "url": f"http://127.0.0.1:{self.fixture.server.port}/one.bin",
                    "size": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
            ],
        }
        self.fixture.write_manifest(manifest)
        self.fixture.server.redirect_with_body_next(
            "one.bin",
            f"http://127.0.0.1:{self.fixture.server.port}/one.bin",
            b"r" * 100000,
        )
        exit_code, report = self._acquire(max_bytes=1)
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertIn("redirect body", report["detail"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertEqual(1, report["network"]["bytes_received"])
        final_path = self.fixture.cache / "redirect-set" / "v1-abcdef0123456789" / "one.bin"
        self.assertFalse(final_path.exists())
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))

    def test_redirect_body_within_allowance_is_counted(self):
        self.fixture.server.files["one.bin"] = b"x"
        manifest = {
            "schema_version": 1,
            "artifact_set": "redirect-set",
            "revision": "v1-abcdef0123456789",
            "namespace": "redirect-set/v1-abcdef0123456789",
            "files": [
                {
                    "path": "one.bin",
                    "url": f"http://127.0.0.1:{self.fixture.server.port}/one.bin",
                    "size": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
            ],
        }
        self.fixture.write_manifest(manifest)
        self.fixture.server.redirect_with_body_next(
            "one.bin",
            f"http://127.0.0.1:{self.fixture.server.port}/one.bin",
            b"r" * 16,
        )
        exit_code, report = self._acquire(max_bytes=17)
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        self.assertEqual(2, report["network"]["requests_attempted"])
        self.assertEqual(17, report["network"]["bytes_received"])
        self.assertEqual(1, report["bytes"]["downloaded"])
        final_path = self.fixture.cache / "redirect-set" / "v1-abcdef0123456789" / "one.bin"
        self.assertEqual(b"x", final_path.read_bytes())
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))

    def test_redirect_chain_counts_every_request(self):
        port = self.fixture.server.port
        self.fixture.server.files["one.bin"] = b"x"
        manifest = {
            "schema_version": 1,
            "artifact_set": "chain-set",
            "revision": "v1-abcdef0123456789",
            "namespace": "chain-set/v1-abcdef0123456789",
            "files": [
                {
                    "path": "one.bin",
                    "url": f"http://127.0.0.1:{port}/a.bin",
                    "size": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
            ],
        }
        self.fixture.write_manifest(manifest)
        self.fixture.server.redirect_with_body_next(
            "a.bin", f"http://127.0.0.1:{port}/mid.bin", b"r" * 8
        )
        self.fixture.server.redirect_with_body_next("mid.bin", f"http://127.0.0.1:{port}/one.bin", b"")
        exit_code, report = self._acquire(max_bytes=9)
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        self.assertEqual(3, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertEqual(9, report["network"]["bytes_received"])
        final_path = self.fixture.cache / "chain-set" / "v1-abcdef0123456789" / "one.bin"
        self.assertEqual(b"x", final_path.read_bytes())
        self.assertEqual(["/a.bin", "/mid.bin", "/one.bin"], self.fixture.server.requests)
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))

    def test_redirect_then_retry_counts_every_request(self):
        port = self.fixture.server.port
        self.fixture.server.redirect_next("a.bin", f"http://127.0.0.1:{port}/a.bin")
        self.fixture.server.fail_next("a.bin", 503)
        exit_code, report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        # a.bin: initial redirect request + followed redirect + retry (3);
        # b.bin: one final request. All count as actual HTTP exchanges.
        self.assertEqual(4, report["network"]["requests_attempted"])
        self.assertEqual(1, report["network"]["retries"])
        self.assertEqual(
            ["/a.bin", "/a.bin", "/a.bin", "/b.bin"], self.fixture.server.requests
        )
        self.assertEqual(48, report["network"]["bytes_received"])
        self.assertEqual(self.fixture.files["a.bin"], self.fixture.target("a.bin").read_bytes())

    def test_internal_ancestor_symlink_alias_is_refused(self):
        real = self.fixture.cache / "B"
        real.mkdir()
        alias = self.fixture.cache / "A"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        manifest = dict(self.fixture.manifest, namespace="A/v1-abcdef0123456789")
        self.fixture.write_manifest(manifest)
        output = io.StringIO()
        with redirect_stdout(output):
            status_code = module.main(
                ["status", "--manifest", str(self.fixture.manifest_path), "--json"],
                {"MODEL_CACHE_DIR": str(self.fixture.cache)},
            )
        status_report = json.loads(output.getvalue())
        self.assertEqual(0, status_code)
        self.assertEqual("CORRUPTED", status_report["files"][0]["state"])
        self.assertIn("symlink", status_report["files"][0]["detail"])
        output = io.StringIO()
        with redirect_stdout(output):
            verify_code = module.main(
                ["verify", "--manifest", str(self.fixture.manifest_path), "--json"],
                {"MODEL_CACHE_DIR": str(self.fixture.cache)},
            )
        verify_report = json.loads(output.getvalue())
        self.assertEqual(4, verify_code)
        self.assertEqual(0, verify_report["network"]["requests_attempted"])
        exit_code, report = self._acquire()
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", report["classification"])
        self.assertIn("symlink", report["detail"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        self.assertFalse(any(real.rglob("*.bin")))
        self.assertFalse(any(real.rglob("_acq-*")))
        self.assertFalse((alias / "v1-abcdef0123456789").exists())

    def test_internal_symlink_cannot_bypass_destination_locking(self):
        real = self.fixture.cache / "B"
        real.mkdir()
        alias = self.fixture.cache / "A"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        # Manifest A (aliased through the symlink): rejected before network.
        manifest_a = dict(self.fixture.manifest, namespace="A/v1-abcdef0123456789")
        self.fixture.write_manifest(manifest_a)
        exit_code, report = self._acquire()
        self.assertEqual(3, exit_code)
        self.assertEqual("MANIFEST_INVALID", report["classification"])
        self.assertEqual(0, report["network"]["requests_attempted"])
        self.assertEqual([], self.fixture.server.requests)
        # Manifest B (canonical namespace B/v1, different expected hashes):
        # it must not report success for content matching B's wrong hashes.
        manifest_b = dict(self.fixture.manifest, namespace="B/v1-abcdef0123456789")
        manifest_b["files"] = [
            dict(file, sha256=hashlib.sha256(b"other").hexdigest()) for file in manifest_b["files"]
        ]
        self.fixture.write_manifest(manifest_b)
        exit_code, report = self._acquire()
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        # No acquisition ever succeeded for the aliased physical destination.
        self.assertFalse(any(real.rglob("*.bin")))
        self.assertFalse(any(real.rglob("_acq-*")))
        self.assertFalse(any(self.fixture.cache.rglob("_acq-*")))

    def test_interrupted_transfer_consumes_budget_and_does_not_retry(self):
        # Pre-verify b.bin so the preflight required total is exactly a.bin's
        # size and --max-bytes 16 is accepted.
        b_target = self.fixture.target("b.bin")
        b_target.parent.mkdir(parents=True, exist_ok=True)
        b_target.write_bytes(self.fixture.files["b.bin"])
        self.fixture.server.fail_partial_next("a.bin", 10)
        exit_code, report = self._acquire(max_bytes=16)
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertEqual(10, report["network"]["bytes_received"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))
        self.assertEqual(self.fixture.files["b.bin"], b_target.read_bytes())

    def test_interrupted_transfer_retries_within_larger_allowance(self):
        self.fixture.server.fail_partial_next("a.bin", 10)
        exit_code, report = self._acquire(max_bytes=58)
        self.assertEqual(0, exit_code)
        self.assertTrue(report["success"])
        self.assertEqual(3, report["network"]["requests_attempted"])
        self.assertEqual(1, report["network"]["retries"])
        self.assertEqual(58, report["network"]["bytes_received"])
        self.assertEqual(48, report["bytes"]["downloaded"])
        for name, data in self.fixture.files.items():
            self.assertEqual(data, self.fixture.target(name).read_bytes())

    def test_temporary_file_creation_failure_is_local_io(self):
        with mock.patch.object(module.os, "open", side_effect=OSError(28, "No space left on device")):
            exit_code, report = self._acquire()
        self.assertEqual(70, exit_code)
        self.assertEqual("LOCAL_IO_FAILURE", report["classification"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_write_failure_is_local_io(self):
        class _FullFile:
            def __init__(self, fd):
                self._fd = fd
            def write(self, data):
                raise OSError(28, "No space left on device")
            def flush(self):
                pass
            def fileno(self):
                return self._fd
            def close(self):
                os.close(self._fd)
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                self.close()
                return False

        with mock.patch.object(module.os, "fdopen", side_effect=lambda fd, mode: _FullFile(fd)):
            exit_code, report = self._acquire()
        self.assertEqual(70, exit_code)
        self.assertEqual("LOCAL_IO_FAILURE", report["classification"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_fsync_failure_is_local_io(self):
        with mock.patch.object(module.os, "fsync", side_effect=OSError(5, "Input/output error")):
            exit_code, report = self._acquire()
        self.assertEqual(70, exit_code)
        self.assertEqual("LOCAL_IO_FAILURE", report["classification"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_promotion_failure_is_local_io(self):
        with mock.patch.object(module.os, "replace", side_effect=OSError(13, "Permission denied")):
            exit_code, report = self._acquire()
        self.assertEqual(70, exit_code)
        self.assertEqual("LOCAL_IO_FAILURE", report["classification"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["retries"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_manifests_sharing_destination_are_serialized_by_lock(self):
        # Manifest A (correct hashes) acquires successfully first.
        first_exit, _first_report = self._acquire()
        self.assertEqual(0, first_exit)
        requests_after_first = list(self.fixture.server.requests)
        # Manifest B: same artifact_set/revision/namespace/target paths, but
        # different hashes, URLs, and roles -> different plan_id, same lock key.
        other = dict(self.fixture.manifest)
        other["files"] = [
            dict(
                other["files"][0],
                sha256=hashlib.sha256(b"wrong-a").hexdigest(),
                url=f"http://127.0.0.1:{self.fixture.server.port}/a.bin",
                role="other-a",
            ),
            dict(
                other["files"][1],
                sha256=hashlib.sha256(b"wrong-b").hexdigest(),
                url=f"http://127.0.0.1:{self.fixture.server.port}/b.bin",
                role="other-b",
            ),
        ]
        self.fixture.write_manifest(other)
        self.assertNotEqual(module._plan_id(self.fixture.manifest), module._plan_id(other))
        self.assertEqual(module._lock_key(self.fixture.manifest), module._lock_key(other))
        # Hold the destination lock and prove B gets a clean LOCK_CONFLICT
        # with zero requests and full context.
        lock = module._ArtifactLock(
            self.fixture.cache, module._lock_key(other), module._plan_id(other)
        )
        lock.acquire()
        try:
            with (
                mock.patch.object(module, "LOCK_WAIT_SECONDS", 0.2),
                mock.patch.object(module.time, "sleep", return_value=None),
            ):
                exit_code, report = self._acquire()
            self.assertEqual(6, exit_code)
            self.assertEqual("LOCK_CONFLICT", report["classification"])
            self.assertEqual(0, report["network"]["requests_attempted"])
            self.assertEqual(0, report["network"]["bytes_received"])
            self.assertEqual(requests_after_first, self.fixture.server.requests)
            self.assertEqual(other["artifact_set"], report["artifact_set"])
            self.assertEqual(other["revision"], report["revision"])
            self.assertEqual(other["namespace"], report["namespace"])
            self.assertEqual(2, report["file_count"])
            self.assertEqual(2, len(report["files"]))
        finally:
            lock.release()
        # Without the held lock, B can never report success for content that
        # matches B's wrong hashes; the final files remain A's verified bytes.
        exit_code, report = self._acquire()
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        for name, data in self.fixture.files.items():
            self.assertEqual(data, self.fixture.target(name).read_bytes())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_no_content_length_response_is_refused_before_body(self):
        manifest = dict(self.fixture.manifest)
        manifest["files"] = [
            dict(manifest["files"][0], size=1, sha256=hashlib.sha256(b"x").hexdigest())
        ]
        self.fixture.write_manifest(manifest)
        self.fixture.server.no_content_length_next("a.bin")
        exit_code, report = self._acquire(max_bytes=1)
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertIn("Content-Length", report["detail"])
        self.assertEqual(1, report["network"]["requests_attempted"])
        self.assertEqual(0, report["network"]["bytes_received"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))

    def test_chunked_response_is_refused_before_body(self):
        self.fixture.server.chunked_next("a.bin")
        exit_code, report = self._acquire()
        self.assertEqual(4, exit_code)
        self.assertEqual("INTEGRITY_FAILURE", report["classification"])
        self.assertIn("Transfer-Encoding", report["detail"])
        self.assertEqual(0, report["network"]["bytes_received"])
        self.assertFalse(self.fixture.target("a.bin").exists())
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))


class RedirectPolicyTests(unittest.TestCase):
    """The network boundary must not expand through server-controlled redirects."""

    def _handler(self):
        return module._RestrictedRedirectHandler()

    def _redirect(self, original, target):
        request = urllib.request.Request(original)
        return self._handler().redirect_request(request, None, 302, "Found", {}, target)

    def test_redirect_to_non_http_scheme_is_refused(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "ftp://example.invalid/b"))

    def test_https_to_http_redirect_is_refused(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "http://example.invalid/b"))

    def test_http_to_remote_http_redirect_is_refused(self):
        self.assertIsNone(self._redirect("http://127.0.0.1:8000/a", "http://example.invalid/b"))

    def test_loopback_http_redirect_is_allowed(self):
        request = self._redirect("http://127.0.0.1:8000/a", "http://127.0.0.1:9000/b")
        self.assertIsNotNone(request)
        self.assertEqual("http://127.0.0.1:9000/b", request.full_url)

    def test_host_docker_internal_http_redirect_is_allowed(self):
        request = self._redirect(
            "http://host.docker.internal:18765/a", "http://host.docker.internal:18765/b"
        )
        self.assertIsNotNone(request)

    def test_https_redirect_is_allowed(self):
        request = self._redirect("https://example.invalid/a", "https://cdn.example.invalid/b")
        self.assertIsNotNone(request)

    def test_redirect_with_userinfo_is_rejected(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "https://user:pass@example.invalid/b"))
        self.assertIsNone(self._redirect("http://127.0.0.1:8000/a", "http://user:pass@127.0.0.1:8000/b"))

    def test_redirect_to_mutable_path_is_rejected(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "https://example.invalid/resolve/main/b"))

    def test_https_production_to_loopback_http_is_rejected(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "http://127.0.0.1:8000/b"))

    def test_https_production_to_loopback_https_is_rejected(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "https://127.0.0.1:8443/b"))

    def test_https_production_to_host_docker_internal_is_rejected(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "https://host.docker.internal:8443/b"))

    def test_redirect_to_encoded_mutable_path_is_rejected(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "https://example.invalid/resolve/%6dain/b"))
        self.assertIsNone(self._redirect("http://127.0.0.1:8000/a", "http://127.0.0.1:8000/resolve/%6c%61%74%65%73%74/b"))

    def test_redirect_to_double_encoded_mutable_path_is_rejected(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "https://example.invalid/resolve/%256dain/b"))

    def test_redirect_to_mutable_query_is_rejected(self):
        self.assertIsNone(self._redirect("https://example.invalid/a", "https://example.invalid/download?revision=main"))
        self.assertIsNone(self._redirect("https://example.invalid/a", "https://example.invalid/download?ref=latest"))

    def test_redirect_with_legitimate_signed_query_is_allowed(self):
        request = self._redirect(
            "https://example.invalid/a", "https://cdn.example.invalid/b?X-Amz-Signature=deadbeef"
        )
        self.assertIsNotNone(request)


if __name__ == "__main__":
    unittest.main()
