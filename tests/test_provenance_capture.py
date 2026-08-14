"""Offline tests for the verified provenance capture logger."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path

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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
        session.execute(transport)
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(transport)
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
        records = session.load_records()
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
        retained = session.responses_dir / "01-r1.bin"
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
        records = session.load_records()
        records[0]["response_body_bytes"] = 999
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain(records)

    def test_inserted_record_detected(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", redirect_target_id="r2"),
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
        records = session.load_records()
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
                make_request("r1", "https://example.com/a", redirect_target_id="r2"),
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
        records = session.load_records()
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain([records[1]])

    def test_reordered_record_detected(self):
        plan = make_plan(
            [
                make_request("r1", "https://example.com/a", redirect_target_id="r2"),
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
        records = session.load_records()
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
        self.assertTrue(records[0]["redirect_followed"])
        self.assertTrue(records[0]["redirect_exact_match"])
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
        self.assertTrue(records[0]["redirect_followed"])
        self.assertTrue(records[0]["redirect_exact_match"])

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
        self.assertTrue(records[0]["redirect_followed"])

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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
        records = session.load_records()
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
                    make_request("r1", "https://example.com/a", redirect_target_id="r2"),
                    make_request("r2", "https://example.com/b", redirect_target_id="r1", redirect_from_id="r1"),
                ]
            )

    def test_multi_entry_cycle_fails(self):
        with self.assertRaises(pc.PlanValidationError):
            make_plan(
                [
                    make_request("r1", "https://example.com/a", redirect_target_id="r2"),
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
        records = session.load_records()
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
        records = session.load_records()
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
        self.assertTrue(record["redirect_followed"])
        self.assertEqual(len(session.load_records()), 1)

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
        self.assertEqual(len(session.load_records()), 2)


if __name__ == "__main__":
    unittest.main()
