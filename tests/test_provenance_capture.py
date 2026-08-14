import json
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urljoin

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
            result = self.body[self.position:]
            self.position = len(self.body)
            return result
        result = self.body[self.position:self.position + amount]
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
    payload["plan_id"] = pc.compute_plan_id(payload)
    return pc.SessionPlan.from_dict(payload)


def make_request(request_id, url, purpose="test", **kwargs):
    data = {
        "id": request_id,
        "method": "GET",
        "url": url,
        "purpose": purpose,
        "allow_query": False,
        "retain": False,
        "follow": None,
        "range_request": False,
    }
    data.update(kwargs)
    return data


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

    def test_successful_200_exact_byte_and_hash_capture(self):
        body = b"hello"
        plan = make_plan([
            make_request("r1", "https://example.com/body", retain=True),
        ])
        session = self.init_session(plan)
        transport = FakeTransport([FakeResponse(200, body)])
        records = session.execute(transport)
        self.assertEqual(records[0]["status"], 200)
        self.assertEqual(records[0]["response_body_bytes"], len(body))
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(body))
        self.assertTrue(records[0]["retained_filename"])

    def test_redirect_body_logged_before_second_hop_and_counted(self):
        plan = make_plan([
            make_request(
                "r1",
                "https://example.com/start",
                follow="r2",
            ),
            make_request("r2", "https://example.com/end"),
        ])
        session = self.init_session(plan)
        transport = FakeTransport([
            FakeResponse(301, b"redirect-body", {"location": ["https://example.com/end"]}),
            FakeResponse(200, b"ok"),
        ])
        records = session.execute(transport)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["status"], 301)
        self.assertTrue(records[0]["redirect_followed"])
        self.assertEqual(records[0]["response_body_bytes"], len(b"redirect-body"))
        self.assertEqual(records[1]["status"], 200)
        self.assertEqual(transport.calls, 2)

    def test_redirect_hop_counted_as_separate_request(self):
        plan = make_plan([
            make_request(
                "r1",
                "https://example.com/start",
                follow="r2",
            ),
            make_request("r2", "https://example.com/end"),
        ])
        session = self.init_session(plan)
        transport = FakeTransport([
            FakeResponse(302, b"redirect-body", {"location": ["https://example.com/end"]}),
            FakeResponse(200, b"ok"),
        ])
        records = session.execute(transport)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["remaining_request_budget"], 9)
        self.assertEqual(records[1]["remaining_request_budget"], 8)
        self.assertEqual(transport.calls, 2)
    def test_404_nonempty_body_measured_and_hashed(self):
        body = b"not found body"
        plan = make_plan([make_request("r1", "https://example.com/404")])
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport([FakeResponse(404, body)]),
        )
        self.assertEqual(records[0]["status"], 404)
        self.assertEqual(records[0]["response_body_bytes"], len(body))
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(body))

    def test_404_empty_body_measured_as_zero(self):
        plan = make_plan([make_request("r1", "https://example.com/404")])
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport([FakeResponse(404, b"")]),
        )
        self.assertEqual(records[0]["status"], 404)
        self.assertEqual(records[0]["response_body_bytes"], 0)
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(b""))

    def test_500_measured_and_logged(self):
        body = b"server error"
        plan = make_plan([make_request("r1", "https://example.com/500")])
        session = self.init_session(plan)
        records = session.execute(
            FakeTransport([FakeResponse(500, body)]),
        )
        self.assertEqual(records[0]["status"], 500)
        self.assertEqual(records[0]["response_body_bytes"], len(body))
        self.assertEqual(records[0]["response_body_sha256"], pc.sha256_bytes(body))

    def test_transport_failure_recorded_distinctly(self):
        plan = make_plan([make_request("r1", "https://example.com/fail")])
        session = self.init_session(plan)
        transport = FakeTransport(error=RuntimeError("boom"))
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(transport)
        records = session.load_records()
        self.assertEqual(records[0]["status"], None)
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
        response = FakeResponse(
            200,
            b"x" * 11,
            {"content-length": ["11"]},
        )
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
            follow = f"r{index + 1}" if index < 10 else None
            requests.append(
                make_request(
                    f"r{index}",
                    f"https://example.com/{index}",
                    follow=follow,
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
                make_request("r1", "https://example.com/one", follow="r2"),
                make_request("r2", "https://example.com/two"),
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
            make_plan([
                make_request("r1", "https://example.com/model.fp16.ckpt"),
            ])

    def test_range_request_rejected(self):
        plan = make_plan([
            make_request("r1", "https://example.com/file", range_request=True),
        ])
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
        ]
        for url, _name in cases:
            with self.subTest(url=url):
                with self.assertRaises(pc.RequestPolicyError):
                    pc._validate_public_url(
                        url,
                        allowed_hosts=allowed,
                        allow_query=False,
                    )

    def test_redirect_to_unknown_host_rejected(self):
        plan = make_plan([make_request("r1", "https://example.com/start")])
        session = self.init_session(plan)
        response = FakeResponse(
            301,
            b"redirect",
            {"location": ["https://evil.example/end"]},
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(FakeTransport([response]))
        records = session.load_records()
        self.assertEqual(records[0]["transport_classification"], "UNEXPECTED_REDIRECT")
        self.assertFalse(records[0]["redirect_followed"])

    def test_unexpected_redirect_rejected(self):
        plan = make_plan([make_request("r1", "https://example.com/start")])
        session = self.init_session(plan)
        response = FakeResponse(
            302,
            b"redirect",
            {"location": ["https://example.com/end"]},
        )
        with self.assertRaises(pc.BudgetBlockedError):
            session.execute(FakeTransport([response]))
        records = session.load_records()
        self.assertEqual(records[0]["transport_classification"], "UNEXPECTED_REDIRECT")
        self.assertFalse(records[0]["redirect_followed"])

    def test_hash_chain_validation(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        records = session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        pc.verify_record_chain(records)

    def test_modified_record_detected(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        records = session.load_records()
        records[0]["response_body_bytes"] = 999
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain(records)

    def test_inserted_record_detected(self):
        plan = make_plan([
            make_request("r1", "https://example.com/a", follow="r2"),
            make_request("r2", "https://example.com/b"),
        ])
        session = self.init_session(plan)
        session.execute(FakeTransport([
            FakeResponse(301, b"r", {"location": ["https://example.com/b"]}),
            FakeResponse(200, b"ok"),
        ]))
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
        plan = make_plan([
            make_request("r1", "https://example.com/a", follow="r2"),
            make_request("r2", "https://example.com/b"),
        ])
        session = self.init_session(plan)
        session.execute(FakeTransport([
            FakeResponse(301, b"r", {"location": ["https://example.com/b"]}),
            FakeResponse(200, b"ok"),
        ]))
        records = session.load_records()
        with self.assertRaises(pc.SessionInvalidError):
            pc.verify_record_chain([records[1]])

    def test_reordered_record_detected(self):
        plan = make_plan([
            make_request("r1", "https://example.com/a", follow="r2"),
            make_request("r2", "https://example.com/b"),
        ])
        session = self.init_session(plan)
        session.execute(FakeTransport([
            FakeResponse(301, b"r", {"location": ["https://example.com/b"]}),
            FakeResponse(200, b"ok"),
        ]))
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

    def test_append_resumes_only_after_validating_previous_chain(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.execute(FakeTransport([FakeResponse(200, b"ok")]))
        records = session.load_records()
        self.assertEqual(len(records), 1)
        session2 = pc.ProvenanceSession(self.session_dir, plan=plan)
        session2.verify_session()

    def test_malformed_existing_session_refuses_further_requests(self):
        plan = make_plan([make_request("r1", "https://example.com/a")])
        session = self.init_session(plan)
        session.log_path.write_text("{not-json\n", encoding="utf-8")
        with self.assertRaises(pc.SessionInvalidError):
            session.execute(FakeTransport([FakeResponse(200, b"ok")]))

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

    def test_cli_exits_with_structured_output_and_no_traceback(self):
        plan_payload = {
            "schema_version": pc.SCHEMA_VERSION,
            "max_requests": 10,
            "max_bytes": 1024,
            "allowed_hosts": ["example.com"],
            "requests": [make_request("r1", "https://example.com/a")],
        }
        plan_payload["plan_id"] = pc.compute_plan_id(plan_payload)
        plan_file = Path(self.tmp.name) / "plan.json"
        plan_file.write_text(
            json.dumps(plan_payload, sort_keys=True),
            encoding="utf-8",
        )
        exit_code = pc.main(["init", "--session", str(self.session_dir), "--plan", str(plan_file)])
        self.assertEqual(exit_code, pc.EXIT_OK)
        self.assertTrue((self.session_dir / "session.log.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
