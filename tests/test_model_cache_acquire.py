import hashlib
import http.server
import importlib.util
import io
import json
import tempfile
import threading
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


class _FixtureServer:
    """Tiny local HTTP server that counts requests and serves fixture bytes."""

    def __init__(self, files):
        self.files = dict(files)
        self.requests = []
        self._failures = {}
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


if __name__ == "__main__":
    unittest.main()
