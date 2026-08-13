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
        part = Path(str(target) + ".part")
        part.write_bytes(b"stale partial bytes")
        exit_code, report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertEqual(self.fixture.files["a.bin"], target.read_bytes())
        self.assertFalse(part.exists())
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

    def test_streaming_writes_via_part_file_and_uses_atomic_replace(self):
        exit_code, _report = self._acquire()
        self.assertEqual(0, exit_code)
        self.assertFalse(any(self.fixture.cache.rglob("*.part")))
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
        self.assertEqual(0, report["network"]["retries"])

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


if __name__ == "__main__":
    unittest.main()
