"""Offline tests for the verified provenance capture logger."""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import provenance_capture as pc


class FakeResponse:
    def __init__(self, status, body=b"", headers=None, final_host=None):
        self.status = status
        self.body = body
        self.headers = {} if headers is None else headers
        self.final_host = final_host
        self.position = 0
        self.read_calls = 0

    def getheader(self, name):
        values = self.headers.get(name.lower())
        return None if values is None else values[0]

    def getheaders(self):
        return list(self.headers.items())

    def get_all(self, name):
        return self.headers.get(name.lower(), [])

    def read(self, amount=-1):
        self.read_calls += 1
        if self.position >= len(self.body):
            return b""
        if amount < 0:
            result = self.body[self.position :]
            self.position = len(self.body)
            return result
        result = self.body[self.position : self.position + amount]
        self.position += len(result)
        return result


class FlakyResponse(FakeResponse):
    def __init__(self, status, body=b"", headers=None, final_host=None,
                 read_error_after=0, error=None):
        super().__init__(status, body, headers, final_host)
        self.read_error_after = read_error_after
        self.error = error or socket.timeout("read timed out")

    def read(self, amount=-1):
        if self.position >= self.read_error_after:
            raise self.error
        if amount < 0 or amount > 1:
            amount = 1
        return super().read(amount)


class OversizedResponse(FakeResponse):
    def read(self, amount=-1):
        if amount < 0:
            amount = 1024
        return b"x" * (amount + 5)


class SequenceResponse(FakeResponse):
    def __init__(self, status, chunks, headers=None, final_host=None):
        super().__init__(status, b"", headers, final_host)
        self.chunks = list(chunks)

    def read(self, amount=-1):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = 0
        self.requested = []

    def __call__(self, method, url, headers):
        self.calls += 1
        self.requested.append((method, url, headers))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("no fake response configured")
        return self.responses.pop(0)


def make_request(request_id, url, purpose="test", **kwargs):
    data = {
        "id": request_id,
        "method": "GET",
        "url": url,
        "purpose": purpose,
        "allow_query": False,
        "retain": False,
        "range_request": False,
        "expected_statuses": [],
        "redirect_target_id": None,
        "redirect_from_id": None,
    }
    data.update(kwargs)
    return data


def http_records(records):
    return [
        record
        for record in records
        if record.get("record_type") == pc.HTTP_RECORD_TYPE
    ]


def make_plan(requests, max_requests=10, max_bytes=2 * 1024 * 1024,
              allowed_hosts=None):
    hosts = allowed_hosts or sorted(
        {pc._normalise_host(req["url"]) for req in requests}
    )
    payload = {
        "schema_version": pc.SCHEMA_VERSION,
        "max_requests": max_requests,
        "max_bytes": max_bytes,
        "allowed_hosts": hosts,
        "requests": requests,
    }
    payload["plan_hash"] = pc.compute_plan_hash(payload)
    return pc.SessionPlan.from_dict(payload)


class ProvenanceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.tmp.name) / "session"

    def tearDown(self):
        self.tmp.cleanup()

    def init_session(self, plan):
        session = pc.ProvenanceSession(self.session_dir, plan=plan)
        session.initialize()
        return session

    def write_plan_file(self, plan):
        path = Path(self.tmp.name) / "plan.json"
        path.write_text(
            json.dumps(plan.to_dict(), sort_keys=True),
            encoding="utf-8",
        )
        return path

    def test_successful_200_exact_byte_and_hash_capture(self):
        body = b"hello"
        plan = make_plan([make_request("r1", "https://example.com/body", retain=True)])
        session = self.init_session(plan)
        records = session.execute(FakeTransport([FakeResponse(200, body)]))
        self.assertEqual(records[0]["status"], 200)
        self.assertEqual(records[0]["response_body_bytes"], len(body))
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(body))
        self.assertTrue(records[0]["retained_filename"])

    def test_404_nonempty_body_measured_and_hashed(self):
        body = b"not found body"
        plan = make_plan([make_request("r1", "https://example.com/404")])
        session = self.init_session(plan)
        records = session.execute(FakeTransport([FakeResponse(404, body)]))
        self.assertEqual(records[0]["status"], 404)
        self.assertEqual(records[0]["response_body_bytes"], len(body))
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(body))

    def test_404_empty_body_measured_as_zero(self):
        plan = make_plan([make_request("r1", "https://example.com/404")])
        session = self.init_session(plan)
        records = session.execute(FakeTransport([FakeResponse(404, b"")]))
        self.assertEqual(records[0]["status"], 404)
        self.assertEqual(records[0]["response_body_bytes"], 0)
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b""))

    def test_500_measured_and_logged(self):
        body = b"server error"
        plan = make_plan([make_request("r1", "https://example.com/500")])
        session = self.init_session(plan)
        records = session.execute(FakeTransport([FakeResponse(500, body)]))
        self.assertEqual(records[0]["status"], 500)
        self.assertEqual(records[0]["response_body_bytes"], len(body))
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(body))

    def test_transport_failure_recorded_distinctly(self):
        plan = make_plan([make_request("r1", "https://example.com/fail")])
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(FakeTransport(error=RuntimeError("boom")))
        records = http_records(session.load_records())
        self.assertIsNone(records[0]["status"])
        self.assertEqual(records[0]["transport_classification"], "TRANSPORT_ERROR")
        self.assertEqual(records[0]["response_body_bytes"], 0)
        self.assertEqual(records[0]["no_body_identity"], "no_http_response")

    def test_timeout_recorded_distinctly_without_retry(self):
        plan = make_plan([make_request("r1", "https://example.com/timeout")])
        session = self.init_session(plan)
        transport = FakeTransport(error=socket.timeout("timed out"))
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(transport)
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "TIMEOUT")
        self.assertEqual(transport.calls, 1)

    def test_content_length_over_budget_refuses_body_read(self):
        plan = make_plan(
            [make_request("r1", "https://example.com/too-big")],
            max_bytes=10,
        )
        session = self.init_session(plan)
        response = FakeResponse(200, b"x" * 11, {"content-length": ["11"]})
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(FakeTransport([response]))
        self.assertEqual(response.read_calls, 0)
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "BUDGET_REFUSAL")
        self.assertEqual(records[0]["response_body_bytes"], 0)
        self.assertFalse(records[0]["body_measured"])
        self.assertEqual(records[0]["no_body_identity"], "body_not_read")

    def test_content_length_equal_to_budget_succeeds(self):
        plan = make_plan(
            [make_request("r1", "https://example.com/exact")],
            max_bytes=5,
        )
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport([FakeResponse(200, b"hello", {"content-length": ["5"]})]),
        )
        self.assertEqual(records[0]["response_body_bytes"], 5)
        self.assertEqual(records[0]["remaining_byte_budget"], 0)

    def test_streaming_overrun_without_content_length_marks_blocked(self):
        plan = make_plan(
            [make_request("r1", "https://example.com/stream")],
            max_bytes=4,
        )
        session = self.init_session(plan)
        response = FakeResponse(200, b"hello")
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(FakeTransport([response]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "BYTE_BUDGET_EXCEEDED")
        self.assertEqual(records[0]["response_body_bytes"], 4)
        self.assertEqual(records[0]["remaining_byte_budget"], 0)
        self.assertEqual(response.position, 4)

    def test_request_eleven_refused(self):
        requests = []
        for index in range(1, 11):
            requests.append(
                make_request(
                    f"r{index}",
                    f"https://example.com/{index}",
                    expected_statuses=[200],
                )
            )
        plan = make_plan(requests, max_requests=10, max_bytes=1024 * 1024)
        session = self.init_session(plan)
        transport = FakeTransport([FakeResponse(200, b"x") for _ in range(10)])
        records = session.execute(transport)
        self.assertEqual(len(records), 10)
        self.assertEqual(len(session.execute(transport)), 10)
        with self.assertRaises(pc.RequestPolicyError):
            session.request_one("r10", transport)
        self.assertEqual(transport.calls, 10)

    def test_byte_budget_exhaustion_refused(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/one"),
            ],
            max_bytes=3,
        )
        session = self.init_session(plan)
        transport = FakeTransport([FakeResponse(200, b"abc")])
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(transport)
        records = http_records(session.load_records())
        self.assertEqual(len(records), 1)
        self.assertEqual(transport.calls, 1)

    def test_weight_body_url_rejected(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan([make_request("r1", "https://example.com/model.fp16.ckpt")])

    def test_range_request_rejected(self):
        plan = make_plan(
            [make_request("r1", "https://example.com/file", range_request=True)],
        )
        session = self.init_session(plan)
        transport = FakeTransport([])
        with self.assertRaises(pc.RequestPolicyError):
            session.execute(transport)
        self.assertEqual(transport.calls, 0)

    def test_invalid_url_policy_rejections(self):
        allowed = frozenset({"example.com"})
        cases = [
            ("http://example.com/a", "non-https"),
            ("https://user:pass@example.com/a", "credentials"),
            ("https://example.com/a?x=1", "query"),
            ("https://example.com/a#frag", "fragment"),
            ("https://evil.example/a", "unknown host"),
            ("https://example.com:444/a", "non-default port"),
        ]
        for url, _name in cases:
            with self.subTest(url=url):
                with self.assertRaises(pc.RequestPolicyError):
                    pc._validate_public_url(
                        url,
                        allowed_hosts=allowed,
                        allow_query=False,
                    )

    def test_raw_bodies_saved_before_analysis(self):
        body = b"raw-body"
        plan = make_plan([make_request("r1", "https://example.com/raw", retain=True)])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, body)]))
        retained = session.responses_dir / "0001.bin"
        self.assertTrue(retained.exists())
        self.assertEqual(retained.read_bytes(), body)

    def test_no_environment_header_or_credential_leakage(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        log_text = session.log_path.read_text(encoding="utf-8")
        self.assertNotIn("Authorization", log_text)
        self.assertNotIn("Cookie", log_text)
        self.assertNotIn("HOME", log_text)
        self.assertNotIn("password", log_text)

    def test_cli_validate_plan_success(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        plan_file = self.write_plan_file(plan)
        exit_code = pc.main(["validate-plan", "--plan", str(plan_file)])
        self.assertEqual(exit_code, pc.EXIT_OK)

    def test_cli_init_success(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        plan_file = self.write_plan_file(plan)
        exit_code = pc.main(
            [
                "init",
                "--session-dir",
                str(self.session_dir),
                "--plan",
                str(plan_file),
            ]
        )
        self.assertEqual(exit_code, pc.EXIT_OK)
        self.assertTrue((self.session_dir / "session.log.jsonl").exists())

    def test_cli_invalid_usage_exit_64(self):
        exit_code = pc.main(["validate-plan"])
        self.assertEqual(exit_code, pc.EXIT_USAGE)


    def test_hash_chain_validation(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        pc.verify_record_chain(session.load_records())

    def test_modified_record_detected(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        records = http_records(session.load_records())
        records[0]["response_body_bytes"] = 999
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain(records)

    def test_inserted_record_detected(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", redirect_target_id="r2", expected_statuses=[301]),
                make_request("r2", "https://example.com/b", redirect_from_id="r1"),
            ]
        )
        session = self.init_session(plan)
        session.execute(
            FakeTransport(
                [
                    FakeResponse(301, b"r", {"location": ["https://example.com/b"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        records = http_records(session.load_records())
        forged = dict(records[0])
        forged["sequence"] = 2
        forged["previous_hash"] = records[0]["current_hash"]
        forged["current_hash"] = pc.record_hash(forged)
        original_second = dict(records[1])
        original_second["sequence"] = 3
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain([records[0], forged, original_second])

    def test_deleted_record_detected(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", redirect_target_id="r2", expected_statuses=[301]),
                make_request("r2", "https://example.com/b", redirect_from_id="r1"),
            ]
        )
        session = self.init_session(plan)
        session.execute(
            FakeTransport(
                [
                    FakeResponse(301, b"r", {"location": ["https://example.com/b"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        records = http_records(session.load_records())
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain([records[1]])

    def test_reordered_record_detected(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", redirect_target_id="r2", expected_statuses=[301]),
                make_request("r2", "https://example.com/b", redirect_from_id="r1"),
            ]
        )
        session = self.init_session(plan)
        session.execute(
            FakeTransport(
                [
                    FakeResponse(301, b"r", {"location": ["https://example.com/b"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        records = http_records(session.load_records())
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain([records[1], records[0]])

    def test_duplicate_sequence_detected(self):
        record = {
            "schema_version": pc.SCHEMA_VERSION,
            "sequence": 1,
            "previous_hash": pc.ZERO_HASH,
            "url": "https://example.com/a",
        }
        record["current_hash"] = pc.record_hash(record)
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain([record, record])

    def test_summary_mismatch_detected(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        summary = json.loads(session.summary_path.read_text(encoding="utf-8"))
        summary["aggregate_bytes"] = 999
        session.summary_path.write_text(
            json.dumps(summary, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_malformed_existing_session_refuses_further_requests(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.log_path.write_text("{not-json\n", encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.execute(FakeTransport([FakeResponse(200, b"ok")]))

    def test_finalized_session_appends_refused(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        session.finalize()
        with self.assertRaises(pc.SessionFinalizedError):
            session.execute(FakeTransport([FakeResponse(200, b"no")]))

    def test_plan_mutation_after_initialization_fails(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        plan_path = session.plan_path
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        data["requests"][0]["url"] = "https://example.com/changed"
        plan_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_duplicate_plan_ids_fail(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/a"),
                    make_request("r1", "https://example.com/b"),
                ]
            )

    def test_duplicate_plan_urls_fail(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/a"),
                    make_request("r2", "https://example.com/a"),
                ]
            )

    def test_duplicate_key_json_fails(self):
        with self.assertRaises(pc.SessionInvalidError):
            pc._load_json_text('{"a": 1, "a": 2}', "test")

    def test_non_finite_json_fails(self):
        with self.assertRaises(pc.SessionInvalidError):
            pc._load_json_text("NaN", "test")

    def test_deep_json_fails_without_traceback(self):
        text = '{"a":' * 10000 + '0' + '}' * 10000
        with self.assertRaises(pc.SessionInvalidError):
            pc._load_json_text(text, "test")

    def test_plan_file_size_limit(self):
        path = Path(self.tmp.name) / "oversized-plan.json"
        path.write_text(" " * (pc.MAX_PLAN_BYTES + 1), encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            pc._load_json_file_bounded(path, "session plan", pc.MAX_PLAN_BYTES)


    def test_relative_redirect_resolves_and_follows(self):
        plan = make_plan(
            [
                make_request(
                    "config-resolve",
                    "https://huggingface.co/models/x/resolve/main/config.yaml",
                    redirect_target_id="config-cache",
                    expected_statuses=[307],
                ),
                make_request(
                    "config-cache",
                    "https://huggingface.co/api/resolve-cache/models/x/resolve/main/config.yaml",
                    redirect_from_id="config-resolve",
                    expected_statuses=[200],
                ),
            ],
            allowed_hosts=["huggingface.co"],
        )
        session = self.init_session(plan)
        transport = FakeTransport(
            [
                FakeResponse(
                    307,
                    b"redirect-body",
                    {"location": ["/api/resolve-cache/models/x/resolve/main/config.yaml"]},
                ),
                FakeResponse(200, b"ok"),
            ]
        )
        records = session.execute(transport)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["redirect_location"], "/api/resolve-cache/models/x/resolve/main/config.yaml")
        self.assertEqual(
            records[0]["redirect_resolved_url"],
            "https://huggingface.co/api/resolve-cache/models/x/resolve/main/config.yaml",
        )
        self.assertTrue(records[0]["redirect_authorized"])
        self.assertTrue(records[0]["redirect_exact_match"])
        self.assertFalse(records[0]["redirect_followed"])
        self.assertTrue(records[1]["redirect_followed"])
        self.assertEqual(
            records[1]["redirect_source_record_hash"],
            records[0]["current_hash"],
        )
        self.assertEqual(records[1]["status"], 200)

    def test_redirect_response_body_logged_before_target_request(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[301]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport(
                [
                    FakeResponse(301, b"redirect-body", {"location": ["https://example.com/end"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        self.assertEqual(records[0]["response_body_bytes"], len(b"redirect-body"))
        self.assertEqual(records[1]["status"], 200)

    def test_redirect_hop_counted_as_separate_request(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[302]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport(
                [
                    FakeResponse(302, b"r", {"location": ["https://example.com/end"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["remaining_request_budget"], 9)
        self.assertEqual(records[1]["remaining_request_budget"], 8)

    def test_absolute_expected_redirect_succeeds(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport(
                [
                    FakeResponse(307, b"r", {"location": ["https://example.com/end"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        self.assertTrue(records[0]["redirect_authorized"])
        self.assertTrue(records[0]["redirect_exact_match"])
        self.assertFalse(records[0]["redirect_followed"])
        self.assertTrue(records[1]["redirect_followed"])
        self.assertEqual(
            records[1]["redirect_source_record_hash"],
            records[0]["current_hash"],
        )

    def test_scheme_relative_redirect_succeeds_exact(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport(
                [
                    FakeResponse(307, b"r", {"location": ["//example.com/end"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        self.assertTrue(records[0]["redirect_authorized"])
        self.assertFalse(records[0]["redirect_followed"])
        self.assertTrue(records[1]["redirect_followed"])

    def test_redirect_to_wrong_host_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://good.example/end", redirect_from_id="r1", expected_statuses=[200]),
            ],
            allowed_hosts=["example.com", "good.example"],
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [FakeResponse(307, b"r", {"location": ["https://evil.example/end"]})]
                )
            )
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "UNEXPECTED_REDIRECT")
        self.assertFalse(records[0]["redirect_followed"])

    def test_redirect_wrong_path_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [FakeResponse(307, b"r", {"location": ["https://example.com/wrong"]})]
                )
            )
        records = http_records(session.load_records())
        self.assertFalse(records[0]["redirect_followed"])
        self.assertEqual(records[0]["redirect_refusal_reason"], "redirect_target_mismatch")

    def test_redirect_wrong_query_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end?rev=abc", allow_query=True, redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [FakeResponse(307, b"r", {"location": ["https://example.com/end?rev=xyz"]})]
                )
            )
        records = http_records(session.load_records())
        self.assertFalse(records[0]["redirect_followed"])

    def test_query_reordering_does_not_pass(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end?a=1&b=2", allow_query=True, redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [FakeResponse(307, b"r", {"location": ["https://example.com/end?b=2&a=1"]})]
                )
            )
        records = http_records(session.load_records())
        self.assertFalse(records[0]["redirect_followed"])

    def test_redirect_with_fragment_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [FakeResponse(307, b"r", {"location": ["https://example.com/end#frag"]})]
                )
            )
        records = http_records(session.load_records())
        self.assertFalse(records[0]["redirect_followed"])

    def test_redirect_containing_credentials_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [FakeResponse(307, b"r", {"location": ["https://user:pass@example.com/end"]})]
                )
            )
        records = http_records(session.load_records())
        self.assertFalse(records[0]["redirect_followed"])

    def test_missing_location_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(FakeTransport([FakeResponse(307, b"r")]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["redirect_refusal_reason"], "missing_location")

    def test_conflicting_location_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [
                        FakeResponse(
                            307,
                            b"r",
                            {
                                "location": [
                                    "https://example.com/end",
                                    "https://example.com/other",
                                ]
                            },
                        )
                    ]
                )
            )
        records = http_records(session.load_records())
        self.assertEqual(records[0]["redirect_refusal_reason"], "conflicting_location_values")

    def test_unplanned_redirect_refused_after_logging_body(self):
        plan = make_plan([make_request("r1", "https://example.com/start")])
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [FakeResponse(302, b"redirect", {"location": ["https://example.com/end"]})]
                )
            )
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "UNEXPECTED_REDIRECT")
        self.assertFalse(records[0]["redirect_followed"])
        self.assertEqual(records[0]["response_body_bytes"], len(b"redirect"))


    def test_redirect_target_missing_fails_plan_validation(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [make_request("r1", "https://example.com/start", redirect_target_id="missing")]
            )

    def test_redirect_source_target_backreference_mismatch_fails(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/start", redirect_target_id="r2"),
                    make_request("r2", "https://example.com/end", redirect_from_id="r1"),
                    make_request("r3", "https://example.com/other", redirect_from_id="r1"),
                ]
            )

    def test_redirect_target_unrelated_later_request_fails(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/start", redirect_target_id="r3"),
                    make_request("r2", "https://example.com/middle"),
                    make_request("r3", "https://example.com/end", redirect_from_id="r1"),
                ]
            )

    def test_self_redirect_fails(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/a", redirect_target_id="r1"),
                    make_request("r2", "https://example.com/b", redirect_from_id="r1"),
                ]
            )

    def test_two_entry_cycle_fails(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/a", redirect_target_id="r2", expected_statuses=[301]),
                    make_request("r2", "https://example.com/b", redirect_target_id="r1", redirect_from_id="r1"),
                ]
            )

    def test_multi_entry_cycle_fails(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/a", redirect_target_id="r2", expected_statuses=[301]),
                    make_request("r2", "https://example.com/b", redirect_from_id="r1", redirect_target_id="r3"),
                    make_request("r3", "https://example.com/c", redirect_from_id="r2", redirect_target_id="r1"),
                ]
            )

    def test_redirect_target_already_consumed_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        session.execute(
            FakeTransport(
                [
                    FakeResponse(307, b"r", {"location": ["https://example.com/end"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        with self.assertRaises(pc.RequestPolicyError):
            session.request_one("r1", FakeTransport([FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]))

    def test_redirect_target_changed_after_session_start_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        session.execute(
            FakeTransport(
                [
                    FakeResponse(307, b"r", {"location": ["https://example.com/end"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        data = json.loads(session.plan_path.read_text(encoding="utf-8"))
        data["requests"][1]["url"] = "https://example.com/changed"
        session.plan_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_redirect_budget_insufficient_prevents_target_request(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ],
            max_bytes=3,
        )
        session = self.init_session(plan)
        transport = FakeTransport(
            [FakeResponse(307, b"abc", {"location": ["https://example.com/end"]})]
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(transport)
        self.assertEqual(transport.calls, 1)
        records = http_records(session.load_records())
        self.assertEqual(len(records), 1)

    def test_no_fabricated_target_record_on_refusal(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(
                FakeTransport(
                    [FakeResponse(307, b"r", {"location": ["https://example.com/wrong"]})]
                )
            )
        records = http_records(session.load_records())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["plan_entry_id"], "r1")

    def test_request_one_does_not_auto_follow(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        record = session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        self.assertTrue(record["redirect_authorized"])
        self.assertFalse(record["redirect_followed"])
        self.assertEqual(len(http_records(session.load_records())), 1)

    def test_request_one_target_without_source_fails(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.RequestPolicyError):
            session.request_one("r2", FakeTransport([FakeResponse(200, b"ok")]))

    def test_request_one_target_after_source_succeeds(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        record = session.request_one("r2", FakeTransport([FakeResponse(200, b"ok")]))
        self.assertEqual(record["status"], 200)
        self.assertTrue(record["redirect_followed"])
        self.assertEqual(record["redirect_source_entry_id"], "r1")
        self.assertEqual(len(http_records(session.load_records())), 2)


    def test_request_one_out_of_order_before_r1_refused(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        transport = FakeTransport([FakeResponse(200, b"ok")])
        with self.assertRaises(pc.RequestPolicyError):
            session.request_one("r2", transport)
        self.assertEqual(transport.calls, 0)
        self.assertEqual(http_records(session.load_records()), [])

    def test_request_one_skips_r2_after_r1_refused(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
                make_request("r3", "https://example.com/c"),
            ]
        )
        session = self.init_session(plan)
        transport = FakeTransport(
            [
                FakeResponse(200, b"a"),
                FakeResponse(200, b"b"),
            ]
        )
        session.request_one("r1", transport)
        with self.assertRaises(pc.RequestPolicyError):
            session.request_one("r3", transport)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(len(http_records(session.load_records())), 1)

    def test_execute_refuses_redirect_target_after_normal_200(self):
        plan = make_plan(
            [
                make_request(
                    "r1",
                    "https://example.com/start",
                    redirect_target_id="r2",
                    expected_statuses=[307],
                ),
                make_request(
                    "r2",
                    "https://example.com/end",
                    redirect_from_id="r1",
                    expected_statuses=[200],
                ),
            ]
        )
        session = self.init_session(plan)
        transport = FakeTransport([FakeResponse(200, b"source-ok")])
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(transport)
        self.assertEqual(transport.calls, 1)
        records = http_records(session.load_records())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["plan_entry_id"], "r1")
        self.assertEqual(records[0]["transport_classification"], "UNEXPECTED_STATUS")
        self.assertFalse(records[0]["redirect_followed"])

    def test_redirect_target_authorized_only_by_immediate_predecessor(self):
        plan = make_plan(
            [
                make_request(
                    "r1",
                    "https://example.com/start",
                    redirect_target_id="r2",
                    expected_statuses=[307],
                ),
                make_request(
                    "r2",
                    "https://example.com/end",
                    redirect_from_id="r1",
                    expected_statuses=[200],
                ),
            ]
        )
        session = self.init_session(plan)
        source_record = session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        self.assertEqual(
            session._authorize_next_entry(plan, [source_record], "r2").id,
            "r2",
        )
        forged = dict(source_record)
        forged["plan_entry_id"] = "wrong-source"
        with self.assertRaises(pc.RequestPolicyError):
            session._authorize_next_entry(plan, [forged], "r2")

    def test_exact_plan_prefix_rejects_gap(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
                make_request("r3", "https://example.com/c"),
            ]
        )
        with self.assertRaises(pc.SessionInvalidError):
            pc._validate_exact_plan_prefix(
                plan,
                [{"plan_entry_id": "r2"}],
            )
        with self.assertRaises(pc.SessionInvalidError):
            pc._validate_exact_plan_prefix(
                plan,
                [
                    {"plan_entry_id": "r1"},
                    {"plan_entry_id": "r3"},
                ],
            )

    def test_boolean_plan_fields_reject_non_booleans(self):
        for name in ("allow_query", "retain", "range_request"):
            for value in ("false", "true", 0, 1, None, [], {}):
                with self.subTest(name=name, value=value):
                    raw = make_request("r1", "https://example.com/a")
                    raw[name] = value
                    payload = {
                        "schema_version": pc.SCHEMA_VERSION,
                        "max_requests": 1,
                        "max_bytes": 1024,
                        "allowed_hosts": ["example.com"],
                        "requests": [raw],
                    }
                    with self.assertRaises(pc.PlanValidationError):
                        pc.SessionPlan.from_dict(payload)

    def test_missing_boolean_plan_fields_default_false(self):
        raw = {
            "id": "r1",
            "method": "GET",
            "url": "https://example.com/a",
            "purpose": "test",
            "expected_statuses": [],
            "redirect_target_id": None,
            "redirect_from_id": None,
        }
        payload = {
            "schema_version": pc.SCHEMA_VERSION,
            "max_requests": 1,
            "max_bytes": 1024,
            "allowed_hosts": ["example.com"],
            "requests": [raw],
        }
        plan = pc.SessionPlan.from_dict(payload)
        self.assertFalse(plan.requests[0].allow_query)
        self.assertFalse(plan.requests[0].retain)
        self.assertFalse(plan.requests[0].range_request)

    def test_cli_out_of_order_error_is_structured(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = pc.main(
                [
                    "request",
                    "--session-dir",
                    str(self.session_dir),
                    "--entry-id",
                    "r2",
                ]
            )
        self.assertEqual(exit_code, pc.EXIT_POLICY_REFUSAL)
        self.assertNotIn("Traceback", buffer.getvalue())
        self.assertIn("out-of-order", buffer.getvalue())


    def test_blocked_state_tamper_cannot_revive_session(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one(
                "r1",
                FakeTransport(error=RuntimeError("boom")),
            )
        state = json.loads(session.state_path.read_text(encoding="utf-8"))
        summary = json.loads(session.summary_path.read_text(encoding="utf-8"))
        state["blocked_reason"] = None
        summary["blocked_reason"] = None
        session.state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        session.summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
        transport = FakeTransport([FakeResponse(200, b"ok")])
        with self.assertRaises(pc.SessionInvalidError):
            session.request_one("r2", transport)
        self.assertEqual(transport.calls, 0)
        self.assertEqual(len(http_records(session.load_records())), 1)

    def test_finalized_state_tamper_cannot_revive_session(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        session.request_one("r1", FakeTransport([FakeResponse(200, b"ok")]))
        session.finalize()
        state = json.loads(session.state_path.read_text(encoding="utf-8"))
        summary = json.loads(session.summary_path.read_text(encoding="utf-8"))
        state["finalized"] = False
        summary["finalized"] = False
        session.state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        session.summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
        transport = FakeTransport([FakeResponse(200, b"ok")])
        with self.assertRaises(pc.SessionInvalidError):
            session.request_one("r2", transport)
        self.assertEqual(transport.calls, 0)

    def test_state_summary_terminal_mismatch_fails(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        session.finalize()
        state = json.loads(session.state_path.read_text(encoding="utf-8"))
        state["finalized"] = False
        session.state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_modified_retained_body_fails_verification(self):
        body = b"retained-body"
        plan = make_plan([make_request("r1", "https://example.com/a", retain=True)])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, body)]))
        target = session.responses_dir / "0001.bin"
        target.write_bytes(b"changed")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_missing_retained_body_fails_verification(self):
        body = b"retained-body"
        plan = make_plan([make_request("r1", "https://example.com/a", retain=True)])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, body)]))
        (session.responses_dir / "0001.bin").unlink()
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_retained_path_escape_and_symlink_are_rejected(self):
        session = pc.ProvenanceSession(self.session_dir)
        session.responses_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(pc.SessionInvalidError):
            session._safe_retained_target("..\\0001.bin")
        with self.assertRaises(pc.SessionInvalidError):
            session._safe_retained_target("0001.bin/extra")
        target = session.responses_dir / "0001.bin"
        target.write_bytes(b"ok")
        link = session.responses_dir / "0002.bin"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        with self.assertRaises(pc.SessionInvalidError):
            session._safe_retained_target("0002.bin")

    def test_unsafe_request_ids_are_rejected(self):
        for value in (
            "x/../outside",
            "a\\b",
            "a:b",
            "a b",
            "..",
            ".",
            "CON",
            "a..",
            "UPPER",
        ):
            with self.subTest(value=value):
                with self.assertRaises(pc.PlanValidationError):
                    pc._validate_request_id(value)

    def test_canonically_duplicate_urls_are_rejected(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/a"),
                    make_request("r2", "https://EXAMPLE.COM:443/a"),
                ]
            )

    def test_query_and_encoded_checkpoint_urls_are_rejected(self):
        cases = [
            ("https://example.com/download?file=model.ckpt", True),
            ("https://example.com/model%2Eckpt", False),
            ("https://example.com/model%252Eckpt", False),
            ("https://example.com/model%25252Eckpt", False),
            ("https://example.com/model%2525252Eckpt", False),
            ("https://example.com/MODEL.CKPT", False),
            ("https://example.com/download?file=model%252Eckpt", True),
            ("https://example.com/download?file=model%25252Eckpt", True),
        ]
        for index, (url, allow_query) in enumerate(cases):
            with self.subTest(url=url):
                with self.assertRaises(pc.PlanValidationError):
                    make_plan(
                        [
                            make_request(
                                f"r{index}",
                                url,
                                allow_query=allow_query,
                            )
                        ]
                    )

    def test_valid_redirect_target_binds_source_hash(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport(
                [
                    FakeResponse(307, b"r", {"location": ["https://example.com/end"]}),
                    FakeResponse(200, b"ok"),
                ]
            )
        )
        self.assertTrue(records[0]["redirect_authorized"])
        self.assertFalse(records[0]["redirect_followed"])
        self.assertTrue(records[1]["redirect_followed"])
        self.assertEqual(records[1]["redirect_source_entry_id"], "r1")
        self.assertEqual(records[1]["redirect_source_record_hash"], records[0]["current_hash"])


    def test_concurrent_session_lock_allows_one_transport_call(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        started = threading.Event()
        release = threading.Event()
        responses = [FakeResponse(200, b"ok")]
        results = []

        def slow_transport(method, url, headers):
            started.set()
            if not release.wait(5):
                raise RuntimeError("release timeout")
            return responses.pop(0)

        def run_winner():
            winner = pc.ProvenanceSession(self.session_dir)
            results.append(winner.request_one("r1", slow_transport))

        thread = threading.Thread(target=run_winner)
        thread.start()
        self.assertTrue(started.wait(5))
        loser = pc.ProvenanceSession(self.session_dir)
        loser_transport = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionBusyError):
            loser.request_one("r1", loser_transport)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(loser_transport.calls, 0)
        records = http_records(session.load_records())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["sequence"], 2)
        session.verify_session()

    def test_response_read_timeout_before_body_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        transport = FakeTransport(
            [FlakyResponse(200, b"hello", read_error_after=0, error=socket.timeout("read"))]
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", transport)
        records = http_records(session.load_records())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], 200)
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_READ_TIMEOUT")
        self.assertEqual(records[0]["response_body_bytes"], 0)
        self.assertFalse(records[0]["body_complete"])
        later = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionFinalizedError):
            session.request_one("r2", later)
        self.assertEqual(later.calls, 0)

    def test_response_read_timeout_after_partial_body_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        transport = FakeTransport(
            [FlakyResponse(200, b"hello", read_error_after=3, error=socket.timeout("read"))]
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", transport)
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_READ_TIMEOUT")
        self.assertEqual(records[0]["response_body_bytes"], 3)
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b"hel"))
        self.assertFalse(records[0]["body_complete"])

    def test_malformed_content_length_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        response = FakeResponse(200, b"ok", {"content-length": ["abc"]})
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", FakeTransport([response]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "CONTENT_LENGTH_INVALID")
        self.assertIsNone(records[0]["response_body_sha256"])
        self.assertFalse(records[0]["body_measured"])
        self.assertEqual(records[0]["response_body_bytes"], 0)

    def test_conflicting_content_length_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        response = FakeResponse(200, b"ok", {"content-length": ["1", "2"]})
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", FakeTransport([response]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "CONTENT_LENGTH_INVALID")
        self.assertEqual(records[0]["response_body_bytes"], 0)

    def test_early_eof_blocks_truthfully(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        response = FakeResponse(200, b"short", {"content-length": ["10"]})
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", FakeTransport([response]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "CONTENT_LENGTH_INVALID")
        self.assertEqual(records[0]["response_body_bytes"], 5)
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b"short"))
        self.assertFalse(records[0]["body_complete"])

    def test_retained_body_write_failure_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", retain=True),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        with mock.patch.object(
            session,
            "_save_body",
            side_effect=pc.SessionInvalidError("disk full"),
        ):
            with self.assertRaises(pc.BudgetBlockedError):
                session.request_one("r1", FakeTransport([FakeResponse(200, b"ok")]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_STORAGE_ERROR")
        self.assertIsNone(records[0]["retained_filename"])
        self.assertTrue(records[0]["body_complete"])
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b"ok"))

    def test_symlinked_session_root_rejected(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        target = Path(self.tmp.name) / "real-session"
        target.mkdir()
        link = Path(self.tmp.name) / "session-link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        session = pc.ProvenanceSession(link, plan=plan)
        with self.assertRaises(pc.SessionInvalidError):
            session.initialize()

    def test_preexisting_log_symlink_rejected(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        outside = Path(self.tmp.name) / "outside.log"
        outside.write_text("outside\n", encoding="utf-8")
        session.log_path.unlink()
        try:
            os.symlink(outside, session.log_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_symlinked_responses_ancestor_rejected(self):
        plan = make_plan([make_request("r1", "https://example.com/a", retain=True)])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        outside = Path(self.tmp.name) / "outside-responses"
        outside.mkdir()
        shutil.rmtree(session.responses_dir)
        try:
            os.symlink(outside, session.responses_dir, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_valid_hf_resolve_cache_url_passes(self):
        plan = make_plan(
            [
                make_request(
                    "config-resolve",
                    "https://huggingface.co/models/x/resolve/main/config.yaml",
                    redirect_target_id="config-cache",
                    expected_statuses=[307],
                ),
                make_request(
                    "config-cache",
                    "https://huggingface.co/api/resolve-cache/models/x/resolve/main/config.yaml",
                    redirect_from_id="config-resolve",
                    expected_statuses=[200],
                ),
            ],
            allowed_hosts=["huggingface.co"],
        )
        self.assertEqual(plan.requests[0].id, "config-resolve")

    def test_sanitized_internal_error_does_not_leak_sentinel(self):
        buffer = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(
            pc.ProvenanceSession,
            "verify_session",
            side_effect=RuntimeError("SECRET_SENTINEL_DO_NOT_LEAK"),
        ), redirect_stdout(buffer), redirect_stderr(errors):
            exit_code = pc.main(
                ["verify", "--session-dir", str(self.session_dir)]
            )
        self.assertEqual(exit_code, pc.EXIT_INTERNAL)
        output = buffer.getvalue()
        payload = json.loads(output)
        self.assertEqual(payload["classification"], "INTERNAL_ERROR")
        self.assertEqual(payload["detail"], "unexpected internal error")
        self.assertNotIn("SECRET_SENTINEL_DO_NOT_LEAK", output)
        self.assertNotIn("SECRET_SENTINEL_DO_NOT_LEAK", errors.getvalue())
        self.assertNotIn("Traceback", output)
        self.assertNotIn("Traceback", errors.getvalue())


    def test_redirect_target_transport_timeout_binds_source(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        target_transport = FakeTransport(error=socket.timeout("timed out"))
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r2", target_transport)
        records = http_records(session.load_records())
        self.assertEqual(len(records), 2)
        target = records[1]
        self.assertEqual(target["plan_entry_id"], "r2")
        self.assertEqual(target["transport_classification"], "TIMEOUT")
        self.assertTrue(target["redirect_followed"])
        self.assertEqual(target["redirect_source_entry_id"], "r1")
        self.assertEqual(target["redirect_source_record_hash"], records[0]["current_hash"])
        session.verify_session()
        later = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionFinalizedError):
            session.request_one("r2", later)
        self.assertEqual(later.calls, 0)

    def test_redirect_target_transport_error_binds_source(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r2", FakeTransport(error=RuntimeError("boom")))
        target = http_records(session.load_records())[1]
        self.assertEqual(target["transport_classification"], "TRANSPORT_ERROR")
        self.assertTrue(target["redirect_followed"])
        self.assertEqual(target["redirect_source_entry_id"], "r1")
        session.verify_session()

    def test_redirect_target_read_timeout_binds_source(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one(
                "r2",
                FakeTransport(
                    [FlakyResponse(200, b"hello", read_error_after=0, error=socket.timeout("read"))]
                ),
            )
        target = http_records(session.load_records())[1]
        self.assertEqual(target["transport_classification"], "RESPONSE_READ_TIMEOUT")
        self.assertTrue(target["redirect_followed"])
        self.assertEqual(target["redirect_source_entry_id"], "r1")
        session.verify_session()

    def test_redirect_target_invalid_content_length_binds_source(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one(
                "r2",
                FakeTransport([FakeResponse(200, b"ok", {"content-length": ["abc"]})]),
            )
        target = http_records(session.load_records())[1]
        self.assertEqual(target["transport_classification"], "CONTENT_LENGTH_INVALID")
        self.assertTrue(target["redirect_followed"])
        self.assertEqual(target["redirect_source_entry_id"], "r1")
        session.verify_session()

    def test_redirect_target_unexpected_status_binds_source(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200]),
            ]
        )
        session = self.init_session(plan)
        session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r2", FakeTransport([FakeResponse(500, b"error")]))
        target = http_records(session.load_records())[1]
        self.assertEqual(target["transport_classification"], "UNEXPECTED_STATUS")
        self.assertTrue(target["redirect_followed"])
        self.assertEqual(target["redirect_source_entry_id"], "r1")
        session.verify_session()

    def test_redirect_target_storage_failure_binds_source(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/start", redirect_target_id="r2", expected_statuses=[307]),
                make_request("r2", "https://example.com/end", redirect_from_id="r1", expected_statuses=[200], retain=True),
            ]
        )
        session = self.init_session(plan)
        session.request_one(
            "r1",
            FakeTransport(
                [FakeResponse(307, b"r", {"location": ["https://example.com/end"]})]
            ),
        )
        with mock.patch.object(
            session,
            "_save_body",
            side_effect=pc.SessionInvalidError("disk full"),
        ):
            with self.assertRaises(pc.BudgetBlockedError):
                session.request_one("r2", FakeTransport([FakeResponse(200, b"ok")]))
        target = http_records(session.load_records())[1]
        self.assertEqual(target["transport_classification"], "RESPONSE_STORAGE_ERROR")
        self.assertTrue(target["redirect_followed"])
        self.assertEqual(target["redirect_source_entry_id"], "r1")
        session.verify_session()

    def test_preexisting_atomic_temp_file_refused(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = pc.ProvenanceSession(self.session_dir, plan=plan)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / "session-plan.json.tmp").write_text("x", encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.initialize()

    def test_symlinked_parent_rejected(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        target = Path(self.tmp.name) / "external-parent"
        target.mkdir()
        link = Path(self.tmp.name) / "linked-parent"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        session = pc.ProvenanceSession(link / "session", plan=plan)
        with self.assertRaises(pc.SessionInvalidError):
            session.initialize()
        self.assertFalse((target / "session").exists())

    def test_nested_symlinked_ancestor_rejected(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        target = Path(self.tmp.name) / "external-parent"
        target.mkdir()
        link = Path(self.tmp.name) / "linked-parent"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")
        session = pc.ProvenanceSession(link / "nested" / "session", plan=plan)
        with self.assertRaises(pc.SessionInvalidError):
            session.initialize()

    def test_hardlinked_log_rejected_and_external_unchanged(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        outside = Path(self.tmp.name) / "outside.log"
        outside.write_text("outside-bytes", encoding="utf-8")
        original = outside.read_bytes()
        session.log_path.unlink()
        try:
            os.link(outside, session.log_path)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks are not available on this platform")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()
        self.assertEqual(outside.read_bytes(), original)

    def test_hardlinked_retained_body_fails(self):
        plan = make_plan([make_request("r1", "https://example.com/a", retain=True)])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        outside = Path(self.tmp.name) / "outside.bin"
        outside.write_bytes(b"outside")
        retained = session.responses_dir / "0001.bin"
        retained.unlink()
        try:
            os.link(outside, retained)
        except (OSError, NotImplementedError):
            self.skipTest("hardlinks are not available on this platform")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_ordinary_single_link_files_still_work(self):
        plan = make_plan([make_request("r1", "https://example.com/a", retain=True)])
        session = self.init_session(plan)
        records = session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        self.assertEqual(records[0]["status"], 200)
        session.verify_session()


    def test_retained_body_os_open_failure_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", retain=True),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        real_open = os.open

        def fake_open(path, flags, mode=0o777):
            if str(path).endswith("0001.bin"):
                raise OSError("disk failure")
            return real_open(path, flags, mode)

        with mock.patch("tools.provenance_capture.os.open", side_effect=fake_open):
            with self.assertRaises(pc.BudgetBlockedError):
                session.request_one("r1", FakeTransport([FakeResponse(200, b"abc")]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_STORAGE_ERROR")
        self.assertEqual(records[0]["response_body_bytes"], 3)
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b"abc"))
        self.assertFalse((session.responses_dir / "0001.bin").exists())
        later = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionFinalizedError):
            session.request_one("r2", later)
        self.assertEqual(later.calls, 0)

    def test_retained_body_os_write_short_write_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", retain=True),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        real_write = os.write
        calls = [0]

        def fake_write(fd, data):
            if len(data) > 3:
                return real_write(fd, data)
            calls[0] += 1
            if calls[0] == 1:
                return 1
            return 0

        with mock.patch("tools.provenance_capture.os.write", side_effect=fake_write):
            with self.assertRaises(pc.BudgetBlockedError):
                session.request_one("r1", FakeTransport([FakeResponse(200, b"abc")]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_STORAGE_ERROR")
        self.assertFalse((session.responses_dir / "0001.bin").exists())

    def test_retained_body_fsync_failure_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", retain=True),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        real_fsync = os.fsync
        calls = [0]

        def fake_fsync(fd):
            calls[0] += 1
            if calls[0] == 4:
                raise OSError("fsync failure")
            return real_fsync(fd)

        with mock.patch("tools.provenance_capture.os.fsync", side_effect=fake_fsync):
            with self.assertRaises(pc.BudgetBlockedError):
                session.request_one("r1", FakeTransport([FakeResponse(200, b"abc")]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_STORAGE_ERROR")
        self.assertFalse((session.responses_dir / "0001.bin").exists())

    def test_content_length_plus_chunked_blocks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        response = FakeResponse(
            200,
            b"abc",
            {"content-length": ["1"], "transfer-encoding": ["chunked"]},
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", FakeTransport([response]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "CONTENT_LENGTH_INVALID")
        self.assertEqual(records[0]["response_body_bytes"], 0)
        self.assertFalse(records[0]["body_measured"])

    def test_chunked_without_content_length_streams(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport(
                [FakeResponse(200, b"abc", {"transfer-encoding": ["chunked"]})]
            )
        )
        self.assertEqual(records[0]["status"], 200)
        self.assertEqual(records[0]["response_body_bytes"], 3)
        self.assertTrue(records[0]["body_complete"])

    def test_transfer_encoding_mixed_case_and_unsupported(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport(
                [FakeResponse(200, b"abc", {"transfer-encoding": ["  Chunked "]})]
            )
        )
        self.assertEqual(records[0]["response_body_bytes"], 3)

        plan2 = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session2 = pc.ProvenanceSession(
            Path(self.tmp.name) / "session2",
            plan=plan2,
        )
        session2.initialize()
        with self.assertRaises(pc.BudgetBlockedError):
            session2.request_one(
                "r1",
                FakeTransport(
                    [FakeResponse(200, b"abc", {"transfer-encoding": ["gzip"]})]
                ),
            )
        self.assertEqual(
            http_records(session2.load_records())[0]["transport_classification"],
            "CONTENT_LENGTH_INVALID",
        )

    def test_max_bytes_boundary(self):
        make_plan(
            [make_request("r1", "https://example.com/a")],
            max_bytes=pc.DEFAULT_MAX_BYTES,
        )
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [make_request("r1", "https://example.com/a")],
                max_bytes=pc.DEFAULT_MAX_BYTES + 1,
            )

    def test_request_count_boundary(self):
        make_plan([make_request("r1", "https://example.com/a")], max_requests=1)
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/a"),
                    make_request("r2", "https://example.com/b"),
                ],
                max_requests=1,
            )

    def test_unknown_top_level_field_after_init_fails(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        data = json.loads(session.plan_path.read_text(encoding="utf-8"))
        data["extra"] = "bad"
        session.plan_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_unknown_request_field_after_init_fails(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        data = json.loads(session.plan_path.read_text(encoding="utf-8"))
        data["requests"][0]["extra"] = "bad"
        session.plan_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_missing_stored_plan_hash_fails(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        data = json.loads(session.plan_path.read_text(encoding="utf-8"))
        del data["plan_hash"]
        session.plan_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_replaced_stored_plan_hash_fails(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        data = json.loads(session.plan_path.read_text(encoding="utf-8"))
        data["plan_hash"] = "0" * 64
        session.plan_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.verify_session()

    def test_duplicate_allowed_hosts_fail(self):
        payload = {
            "schema_version": pc.SCHEMA_VERSION,
            "max_requests": 1,
            "max_bytes": 1024,
            "allowed_hosts": ["example.com", "EXAMPLE.COM"],
            "requests": [make_request("r1", "https://example.com/a")],
        }
        with self.assertRaises(pc.PlanValidationError):
            pc.SessionPlan.from_dict(payload)

    def test_plan_json_semantic_whitespace_reordering_passes(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        data = json.loads(session.plan_path.read_text(encoding="utf-8"))
        session.plan_path.write_text(
            json.dumps(data, indent=4, sort_keys=False),
            encoding="utf-8",
        )
        session.verify_session()


    def test_empty_url_path_canonical_duplicate_rejected(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com"),
                    make_request("r2", "https://example.com/"),
                ]
            )

    def test_post_transport_log_append_failure_blocks_retry(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        original_append = session._append_jsonl_line
        calls = [0]

        def fail_second(record):
            calls[0] += 1
            if calls[0] == 2:
                raise pc.SessionInvalidError("simulated zero-byte append")
            return original_append(record)

        with mock.patch.object(session, "_append_jsonl_line", side_effect=fail_second):
            with self.assertRaises(pc.SessionInvalidError):
                session.request_one("r1", FakeTransport([FakeResponse(200, b"ok")]))
        later = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionInvalidError):
            session.request_one("r2", later)
        self.assertEqual(later.calls, 0)
        full = session.load_records()
        self.assertEqual(full[0]["record_type"], pc.RESERVATION_RECORD_TYPE)

    def test_response_metadata_and_bad_reads_block_without_retry(self):
        class MetadataFailureResponse(FakeResponse):
            def getheaders(self):
                raise RuntimeError("metadata failure")

        class NonBytesResponse(FakeResponse):
            def read(self, amount=-1):
                return "not-bytes"

        class OversizedResponse(FakeResponse):
            def read(self, amount=-1):
                return b"x" * (amount + 5)

        cases = [
            MetadataFailureResponse(200, b"ok"),
            NonBytesResponse(200, b"ok"),
            OversizedResponse(200, b"ok"),
        ]
        for response in cases:
            with self.subTest(response=type(response).__name__):
                plan = make_plan(
                    [
                        make_request("r1", "https://example.com/a"),
                        make_request("r2", "https://example.com/b"),
                    ]
                )
                session = pc.ProvenanceSession(
                    Path(self.tmp.name) / f"session-{type(response).__name__}",
                    plan=plan,
                )
                session.initialize()
                with self.assertRaises(pc.BudgetBlockedError):
                    session.request_one("r1", FakeTransport([response]))
                later = FakeTransport([FakeResponse(200, b"no")])
                with self.assertRaises(pc.SessionFinalizedError):
                    session.request_one("r2", later)
                self.assertEqual(later.calls, 0)

    def test_projection_failure_appends_storage_record(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        original_write = session._write_json_atomic

        def fail_state(path, value, subject):
            if path == session.state_path:
                raise pc.SessionInvalidError("projection failure")
            return original_write(path, value, subject)

        with mock.patch.object(session, "_write_json_atomic", side_effect=fail_state):
            with self.assertRaises(pc.SessionInvalidError):
                session.request_one("r1", FakeTransport([FakeResponse(200, b"ok")]))
        full = session.load_records()
        self.assertTrue(
            any(
                record.get("record_type") == pc.PROJECTION_FAILURE_RECORD_TYPE
                and record.get("transport_classification") == "RESPONSE_STORAGE_ERROR"
                for record in full
            )
        )
        later = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionInvalidError):
            session.request_one("r2", later)
        self.assertEqual(later.calls, 0)


    def test_secure_authoritative_reads_refuse_inode_swap(self):
        plan = make_plan([make_request("r1", "https://example.com/a", retain=True)])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"retained")]))

        replacements = {
            "plan": session.plan_path,
            "state": session.state_path,
            "summary": session.summary_path,
            "log": session.log_path,
            "retained": session.responses_dir / "0001.bin",
        }
        for name, target in replacements.items():
            with self.subTest(name=name):
                replacement = Path(self.tmp.name) / f"{name}-replacement"
                replacement.write_bytes(target.read_bytes())
                original_bytes = replacement.read_bytes()
                real_open = os.open

                def fake_open(path, flags, mode=0o777):
                    if Path(path) == target:
                        return real_open(replacement, flags, mode)
                    return real_open(path, flags, mode)

                with mock.patch("tools.provenance_capture.os.open", side_effect=fake_open):
                    with self.assertRaises(pc.SessionInvalidError):
                        if name == "plan":
                            session._read_authoritative_bytes(
                                target, "session plan", pc.MAX_PLAN_BYTES
                            )
                        elif name == "state":
                            session._read_authoritative_bytes(
                                target, "session state", pc.MAX_STATE_BYTES
                            )
                        elif name == "summary":
                            session._read_authoritative_bytes(
                                target, "session summary", pc.MAX_SUMMARY_BYTES
                            )
                        elif name == "log":
                            session._read_authoritative_bytes(
                                target, "session log", pc.MAX_LOG_BYTES
                            )
                        else:
                            session._read_authoritative_bytes(
                                target, "retained body", pc.DEFAULT_MAX_BYTES
                            )
                self.assertEqual(replacement.read_bytes(), original_bytes)

    def test_oversized_first_read_accounts_exact_bytes(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ],
            max_bytes=5,
        )
        session = self.init_session(plan)
        response = OversizedResponse(200, b"ok")
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", FakeTransport([response]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_READ_OVERSIZED")
        self.assertEqual(records[0]["response_body_bytes"], 10)
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b"x" * 10))
        self.assertFalse(records[0]["body_complete"])
        session.verify_session()
        later = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionFinalizedError):
            session.request_one("r2", later)
        self.assertEqual(later.calls, 0)

    def test_oversized_read_after_valid_chunks(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        response = SequenceResponse(
            200,
            [b"ab", b"xyzw"],
            {"content-length": ["5"]},
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", FakeTransport([response]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_READ_OVERSIZED")
        self.assertEqual(records[0]["response_body_bytes"], 6)
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b"abxyzw"))

    def test_oversized_read_crosses_byte_budget(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a"),
                make_request("r2", "https://example.com/b"),
            ],
            max_bytes=3,
        )
        session = self.init_session(plan)
        response = SequenceResponse(200, [b"abcdef"])
        with self.assertRaises(pc.BudgetBlockedError):
            session.request_one("r1", FakeTransport([response]))
        records = http_records(session.load_records())
        self.assertEqual(records[0]["transport_classification"], "RESPONSE_READ_OVERSIZED")
        self.assertEqual(records[0]["response_body_bytes"], 6)
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b"abcdef"))
        later = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionFinalizedError):
            session.request_one("r2", later)
        self.assertEqual(later.calls, 0)

    def test_symlink_swap_refused_without_transport_and_external_unchanged(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", retain=True),
                make_request("r2", "https://example.com/b"),
            ]
        )
        session = self.init_session(plan)
        session.request_one(
            "r1",
            FakeTransport([FakeResponse(200, b"retained")]),
        )

        external = Path(self.tmp.name) / "external-identical.bin"
        external.write_bytes(b"retained")
        target = session.responses_dir / "0001.bin"
        target.unlink()
        try:
            target.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available on this platform")

        later = FakeTransport([FakeResponse(200, b"no")])
        with self.assertRaises(pc.SessionInvalidError):
            session.request_one("r2", later)
        self.assertEqual(later.calls, 0)
        self.assertEqual(external.read_bytes(), b"retained")


if __name__ == "__main__":
    unittest.main()
