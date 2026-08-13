#!/usr/bin/env python3
"""Tiny deterministic HTTP fixture server for T-0014 model-cache validation.

Serves the exact bytes declared by the committed test manifest over loopback,
counts incoming GET requests, and optionally serves altered bytes for one
artifact so integrity-failure paths can be exercised without touching any
production model data. The total fixture payload is 87 bytes, far below the
ticket's 5 MiB test-data budget.

The server validates its built-in bytes against the manifest at startup so a
manifest/fixture drift fails loudly instead of producing false test evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence


DEFAULT_MANIFEST = "/app/model-manifests/test-fixture.json"
DEFAULT_PORT = 18765

FIXTURE_CONTENT = {
    "data/a.bin": b"pharaon-fixture-a-0123456789\n",
    "data/b.bin": b"pharaon-fixture-b-9876543210\n",
    "data/c.bin": b"pharaon-fixture-c-abcdefghij\n",
}


def _validated_manifest(manifest_path: str) -> dict[str, tuple[int, str]]:
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    declared: dict[str, tuple[int, str]] = {}
    for item in manifest["files"]:
        declared[item["path"]] = (int(item["size"]), str(item["sha256"]))
    if set(declared) != set(FIXTURE_CONTENT):
        raise RuntimeError(
            "fixture server content and manifest file sets differ: "
            f"manifest={sorted(declared)}, fixtures={sorted(FIXTURE_CONTENT)}"
        )
    for name, data in FIXTURE_CONTENT.items():
        size, sha256 = declared[name]
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise RuntimeError(f"fixture {name} does not match manifest size/hash")
    return declared


class _FixtureServer:
    def __init__(
        self,
        manifest_path: str,
        bind: str,
        port: int,
        corrupt: str | None,
        partial: str | None = None,
        no_length: str | None = None,
    ) -> None:
        _validated_manifest(manifest_path)
        self.corrupt = corrupt
        self.partial = None
        if partial:
            name, _, count = partial.partition(":")
            self.partial = (name, int(count))
        self.no_length = no_length
        self.requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                server.requests.append(self.path)
                print(f"REQUEST {self.path}", flush=True)
                name = self.path.lstrip("/")
                if server.no_length is not None and name == server.no_length:
                    server.no_length = None  # one-shot
                    data = server._bytes_for(name)
                    if data is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                    self.close_connection = True
                    return
                if server.partial is not None and name == server.partial[0]:
                    n = server.partial[1]
                    server.partial = None  # one-shot interrupted transfer
                    data = server._bytes_for(name)
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
                data = server._bytes_for(name)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = self
        self.httpd = ThreadingHTTPServer((bind, port), Handler)
        self.port = self.httpd.server_address[1]

    def _bytes_for(self, name: str) -> bytes | None:
        data = FIXTURE_CONTENT.get(name)
        if data is None:
            return None
        if name == self.corrupt:
            # Same length, different content: exercises the SHA-256 mismatch path.
            return data[:-1] + bytes([data[-1] ^ 0xFF])
        return data

    def serve(self) -> None:
        print(f"READY port={self.port}", flush=True)
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()
            print(f"SUMMARY requests={len(self.requests)}", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, help="path to the committed test manifest")
    parser.add_argument("--bind", default="127.0.0.1", help="bind address (loopback by default)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument("--corrupt", default=None, help="serve altered bytes for this artifact path")
    parser.add_argument(
        "--partial",
        default=None,
        help="serve a Content-Length body that closes after N bytes for one artifact path, e.g. data/a.bin:10",
    )
    parser.add_argument(
        "--no-length",
        default=None,
        help="serve one artifact without a Content-Length header, e.g. data/a.bin",
    )
    args = parser.parse_args(argv)
    try:
        server = _FixtureServer(args.manifest, args.bind, args.port, args.corrupt, args.partial, args.no_length)
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"FIXTURE_SERVER_ERROR {error}", file=sys.stderr)
        return 1
    server.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())