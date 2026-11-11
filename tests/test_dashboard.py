"""The dashboard projects curated evidence without controlling a running swarm."""

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from swarm_control.dashboard import load_snapshot, make_server

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def timestamp(value):
    return value.isoformat()


def task(
    task_id="T01",
    *,
    product=True,
    attribution="trusted_local_unchanged_failed_worker_recovery",
):
    merged = datetime.now(UTC) - timedelta(minutes=5)
    return {
        "task_id": task_id,
        "issue": 1,
        "original_status": "needs_repair",
        "original_agent_exit": -1,
        "original_helper_check_exit": 0,
        "original_patch_sha256": "a" * 64,
        "queue_status": "merged_green_provisional",
        "provisional_qualified": True,
        "workload_counts_as_feature_only_not_delivery": product,
        "binding_checks": {"source_bound": True, "review_bound": True},
        "recovery": {
            "attribution": attribution,
            "reviewer": "independent-reviewer",
            "review_receipt_sha256": "b" * 64,
            "reviewed_patch_sha256": "a" * 64,
            "coordinator_delta_sha256": None,
            "reviewed_head_sha": "c" * 40,
            "merge_commit_sha": "d" * 40,
            "pr_url": "https://github.com/example/project/pull/1",
            "head_checks_url": "https://github.com/example/project/commit/"
            + "c" * 40
            + "/checks",
            "combined_checks_url": "https://github.com/example/project/commit/"
            + "d" * 40
            + "/checks",
            "review_duration_seconds": 2.5,
            "review_started_at_utc": timestamp(merged - timedelta(seconds=5)),
            "review_finished_at_utc": timestamp(merged - timedelta(seconds=2.5)),
            "merged_at_utc": timestamp(merged),
            "combined_green_observed_at_utc": timestamp(merged + timedelta(minutes=1)),
            "quality_window_end_utc": timestamp(
                merged + timedelta(hours=24, minutes=1)
            ),
        },
    }


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "curated.json"
        self.now = datetime.now(UTC)
        self.source = {
            "repository": "example/project",
            "run_id": "run-1",
            "as_of_utc": timestamp(self.now - timedelta(seconds=5)),
            "clock": {
                "T0_utc": timestamp(self.now - timedelta(hours=1)),
                "T1_utc": None,
                "delivery_cutoff_utc": timestamp(self.now + timedelta(hours=5)),
                "full_delivery_finished": False,
            },
            "rows": [task()],
            "local_validation_summary": {"attempt_count": 2, "failed_attempts": 1},
            "final_metrics": {
                "billing_reconciled": False,
                "AFK_efficiency_win_proven": False,
            },
        }
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.source))

    def snapshot(self):
        return load_snapshot(self.path, now=self.now)


class SnapshotTests(SnapshotFixture):
    def test_curated_counts_separate_qualification_classification_and_support(self):
        support = task(
            "T02", product=False, attribution="coordinator_code_or_test_repair"
        )
        support["recovery"]["coordinator_delta_sha256"] = "e" * 64
        empty = task("T03")
        empty.update(
            original_patch_sha256=EMPTY_SHA256,
            original_agent_exit=0,
            queue_status="original_empty_patch",
            provisional_qualified=False,
            recovery=None,
        )
        rejected = task("T04")
        rejected.update(
            original_agent_exit=137,
            original_helper_check_exit=1,
            queue_status="independent_review_rejected",
            provisional_qualified=False,
            recovery=None,
        )
        self.source["rows"] = [task(), support, empty, rejected]
        self.source["original_worker_outcomes"] = {"status_counts": {"needs_repair": 4}}
        self.save()
        result = self.snapshot()
        self.assertTrue(result["available"])
        summary = result["summary"]
        for key, expected in {
            "assigned": 4,
            "original_validated": 0,
            "original_needs_repair": 4,
            "provisional_tasks": 2,
            "provisional_product": 1,
            "provisional_support": 1,
            "unchanged_recoveries": 1,
            "coordinator_repaired": 1,
            "empty_patches": 1,
            "product_candidates": 3,
            "support_candidates": 1,
        }.items():
            with self.subTest(key=key):
                self.assertEqual(summary[key], expected)
        tasks = {row["id"]: row for row in result["tasks"]}
        self.assertTrue(tasks["T03"]["empty_patch"])
        self.assertFalse(tasks["T03"]["provisional"])
        self.assertFalse(tasks["T02"]["feature_candidate"])

    def test_false_or_nonboolean_binding_never_becomes_provisional(self):
        for value in (False, None, 1, "true"):
            with self.subTest(value=value):
                self.source["rows"][0]["binding_checks"]["source_bound"] = value
                self.save()
                result = self.snapshot()
                self.assertTrue(result["available"])
                self.assertEqual(result["summary"]["provisional_tasks"], 0)
                self.assertFalse(result["tasks"][0]["provisional"])
        self.source["rows"][0]["binding_checks"] = {"source_bound": True}
        self.source["rows"][0]["provisional_qualified"] = False
        self.save()
        self.assertEqual(self.snapshot()["summary"]["provisional_tasks"], 0)

    def test_contradictory_state_missing_delivery_and_bad_attribution_are_not_qualified(
        self,
    ):
        original = task()
        variations = []
        rejected = task()
        rejected["queue_status"] = "independent_review_rejected"
        variations.append(rejected)
        for key in (
            "pr_url",
            "reviewed_head_sha",
            "merge_commit_sha",
            "merged_at_utc",
            "combined_green_observed_at_utc",
            "attribution",
        ):
            missing = task()
            missing["recovery"][key] = None
            variations.append(missing)
        for index, row in enumerate(variations):
            with self.subTest(index=index):
                self.source["rows"] = [row]
                self.save()
                self.assertFalse(self.snapshot()["tasks"][0]["provisional"])
                self.assertEqual(self.snapshot()["summary"]["provisional_tasks"], 0)
        self.source["rows"] = [original]

    def test_unknown_payload_fields_do_not_escape_and_source_is_read_only(self):
        secret = "PRIVATE_SENTINEL_NOT_AN_ACTUAL_SECRET"
        self.source.update(
            raw_worker_logs=secret,
            api_key=secret,
            private_directory="/home/private-worker/runtime",
        )
        self.source["rows"][0].update(
            raw_report={"private": secret}, worker_token=secret
        )
        self.source["rows"][0]["recovery"]["unapproved_payload"] = secret
        self.save()
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with patch("subprocess.run", side_effect=AssertionError("external operation")):
            result = self.snapshot()
        serialized = json.dumps(result)
        self.assertTrue(result["available"])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("/home/private-worker", serialized)
        self.assertNotIn(str(self.path), serialized)
        self.assertEqual(
            before, {path.name: path.read_bytes() for path in self.root.iterdir()}
        )

    def test_unknown_billing_is_null_and_cannot_imply_zero_cost_or_an_efficiency_win(
        self,
    ):
        result = self.snapshot()
        self.assertTrue(result["available"])
        self.assertIsNone(result["financials"].get("actual_billed_total_usd"))
        self.assertIsNone(result["financials"].get("authorized_new_usd"))
        self.assertNotEqual(result.get("AFK_efficiency_win_proven"), True)
        self.source["dashboard_financials"] = {
            "authorized_new_usd": 200.0,
            "actual_billed_total_usd": None,
            "private_account_token": "PRIVATE_SENTINEL",
        }
        self.save()
        result = self.snapshot()
        self.assertEqual(result["financials"]["authorized_new_usd"], 200.0)
        self.assertIsNone(result["financials"].get("actual_billed_total_usd"))
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))

    def test_nonfinite_negative_and_boolean_financial_values_are_not_displayed_as_amounts(
        self,
    ):
        for value in (-1, float("inf"), float("nan"), True, "200"):
            with self.subTest(value=value):
                self.source["dashboard_financials"] = {"authorized_new_usd": value}
                self.save()
                result = self.snapshot()
                if result["available"]:
                    self.assertIsNone(result["financials"].get("authorized_new_usd"))
                json.dumps(result, allow_nan=False)

    def test_unreconciled_invoice_claim_is_not_a_billed_amount(self):
        self.source["dashboard_financials"] = {"actual_billed_total_usd": 12.5}
        self.save()
        self.assertIsNone(self.snapshot()["financials"]["actual_billed_total_usd"])
        self.source["final_metrics"]["billing_reconciled"] = True
        self.save()
        self.assertEqual(self.snapshot()["financials"]["actual_billed_total_usd"], 12.5)

    def test_empty_bindings_and_nonboolean_qualification_do_not_imply_delivery(self):
        self.source["rows"][0]["binding_checks"] = {}
        self.save()
        self.assertFalse(self.snapshot()["tasks"][0]["provisional"])
        self.source["rows"][0]["binding_checks"] = {"source_bound": True}
        for value in (None, 1, "true"):
            with self.subTest(value=value):
                self.source["rows"][0]["provisional_qualified"] = value
                self.save()
                self.assertFalse(self.snapshot()["tasks"][0]["provisional"])

    def test_optional_task_titles_accept_normal_task_ids_and_omit_private_or_invalid_text(
        self,
    ):
        self.assertIsNone(self.snapshot()["tasks"][0]["title"])
        self.source["rows"] = [task("task-1")]
        normal_title = "Fix task-1 public lobby instructions"
        self.source["dashboard_task_titles"] = {"task-1": normal_title}
        self.save()
        result = self.snapshot()
        self.assertTrue(result["available"])
        self.assertEqual(result["tasks"][0]["id"], "task-1")
        self.assertEqual(result["tasks"][0]["title"], normal_title)
        self.source["dashboard_task_titles"]["task-1"] = "A" * 240
        self.save()
        self.assertEqual(self.snapshot()["tasks"][0]["title"], "A" * 240)
        for value in (
            None,
            5,
            "",
            "A" * 241,
            "unsafe\x01title",
            "line\nbreak",
            "/home/private/runtime",
            "/Users/private/runtime",
            "/tmp/private/runtime",
            "sk-" + "A" * 32,
            "Bearer " + "A" * 40,
        ):
            with self.subTest(
                kind=type(value).__name__,
                length=len(value) if isinstance(value, str) else None,
            ):
                self.source["dashboard_task_titles"]["task-1"] = value
                self.save()
                result = self.snapshot()
                self.assertTrue(result["available"])
                self.assertIsNone(result["tasks"][0]["title"])

    def test_unsafe_links_are_never_exposed(self):
        for value in (
            "javascript:alert(1)",
            "file:///home/private/runtime",
            "https://evil.invalid/private",
            "https://github.com/example/project/blob/main/../private",
        ):
            with self.subTest(url=value):
                self.source["rows"][0]["recovery"]["pr_url"] = value
                self.source["dashboard_links"] = {"report_url": value}
                self.save()
                self.assertNotIn(value, json.dumps(self.snapshot()))

    def test_missing_malformed_oversize_and_wrong_shape_sources_fail_without_path_or_payload_leaks(
        self,
    ):
        for body in ("PRIVATE_SENTINEL", "[]", "{}", "x" * (8 * 1024 * 1024 + 1)):
            with self.subTest(length=len(body)):
                self.path.write_text(body)
                result = self.snapshot()
                self.assertFalse(result["available"])
                self.assertNotIn(str(self.path), json.dumps(result))
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
        self.path.unlink()
        self.assertFalse(self.snapshot()["available"])

    def test_future_naive_and_invalid_observation_timestamps_fail(self):
        for value in (
            timestamp(self.now + timedelta(hours=1)),
            timestamp(self.now + timedelta(seconds=10)),
            self.now.replace(tzinfo=None).isoformat(),
            "yesterday",
        ):
            with self.subTest(value=value):
                self.source["as_of_utc"] = value
                self.save()
                self.assertFalse(self.snapshot()["available"])

    def test_finished_clock_requires_ordered_past_end_and_unfinished_clock_ignores_t1(
        self,
    ):
        self.source["clock"]["full_delivery_finished"] = True
        for value in (
            None,
            self.now.replace(tzinfo=None).isoformat(),
            timestamp(self.now + timedelta(minutes=1)),
            timestamp(self.now - timedelta(hours=2)),
        ):
            with self.subTest(t1=value):
                self.source["clock"]["T1_utc"] = value
                self.save()
                self.assertFalse(self.snapshot()["available"])
        self.source["clock"]["full_delivery_finished"] = False
        self.source["clock"]["T1_utc"] = timestamp(self.now + timedelta(minutes=1))
        self.save()
        result = self.snapshot()
        self.assertTrue(result["available"])
        self.assertIsNone(result["clock"]["t1"])
        for value in (
            None,
            self.now.replace(tzinfo=None).isoformat(),
            timestamp(self.now + timedelta(minutes=1)),
        ):
            with self.subTest(t0=value):
                self.source["clock"]["T0_utc"] = value
                self.save()
                self.assertFalse(self.snapshot()["available"])

    def test_future_or_reversed_delivery_is_not_qualified_but_future_quality_deadline_is_valid(
        self,
    ):
        for source_field, output_field in (
            ("merged_at_utc", "merged_at"),
            ("combined_green_observed_at_utc", "green_at"),
        ):
            with self.subTest(field=source_field):
                row = task()
                row["recovery"][source_field] = timestamp(
                    self.now + timedelta(minutes=1)
                )
                self.source["rows"] = [row]
                self.save()
                result = self.snapshot()
                self.assertTrue(result["available"])
                self.assertFalse(result["tasks"][0]["provisional"])
                self.assertIsNone(result["tasks"][0]["delivery"][output_field])
        for merged, green in (
            (self.now - timedelta(minutes=1), self.now - timedelta(minutes=2)),
            (self.now - timedelta(hours=2), self.now - timedelta(minutes=1)),
        ):
            with self.subTest(merged=merged, green=green):
                row = task()
                row["recovery"].update(
                    merged_at_utc=timestamp(merged),
                    combined_green_observed_at_utc=timestamp(green),
                )
                self.source["rows"] = [row]
                self.save()
                self.assertFalse(self.snapshot()["tasks"][0]["provisional"])
        self.source["rows"] = [task()]
        self.save()
        result = self.snapshot()
        self.assertTrue(result["tasks"][0]["provisional"])
        self.assertGreater(
            datetime.fromisoformat(result["tasks"][0]["delivery"]["quality_until"]),
            self.now,
        )

    def test_future_or_invalid_activity_stays_unknown_instead_of_claiming_live_workers(
        self,
    ):
        for observed in (
            timestamp(self.now + timedelta(minutes=1)),
            self.now.replace(tzinfo=None).isoformat(),
            "invalid",
        ):
            with self.subTest(observed=observed):
                self.source["dashboard_activity"] = {
                    "active_workers": 1,
                    "observed_at": observed,
                }
                self.save()
                self.assertIsNone(self.snapshot()["summary"]["active_workers"])
        self.source["dashboard_activity"] = {
            "active_workers": 0,
            "observed_at": timestamp(self.now - timedelta(minutes=1)),
        }
        self.save()
        self.assertEqual(self.snapshot()["summary"]["active_workers"], 0)

    def test_missing_duplicate_or_excessive_rows_fail(self):
        for rows in (
            None,
            {},
            [task(), task()],
            [task(f"T{i:04}") for i in range(1001)],
        ):
            with self.subTest(kind=type(rows).__name__):
                self.source["rows"] = rows
                self.save()
                self.assertFalse(self.snapshot()["available"])

    def test_stale_is_observation_freshness_and_completed_is_not_quality_acceptance(
        self,
    ):
        self.source["as_of_utc"] = timestamp(self.now - timedelta(minutes=3))
        self.source["clock"].update(
            full_delivery_finished=True,
            T1_utc=timestamp(self.now - timedelta(minutes=4)),
        )
        self.save()
        result = self.snapshot()
        self.assertTrue(result["available"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["phase"], "complete")
        self.assertNotEqual(result.get("quality_window_accepted"), True)


class HttpTests(SnapshotFixture):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.path, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_address[1]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def request(self, target, *, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, target, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_http_api_and_health_return_current_sanitized_data(self):
        status, headers, raw = self.request("/api/snapshot")
        self.assertEqual(status, 200)
        result = json.loads(raw)
        self.assertTrue(result["available"])
        self.assertEqual(result["summary"]["assigned"], 1)
        self.assertNotIn(str(self.path).encode(), raw)
        self.assertIn("application/json", headers["Content-Type"])
        status, _headers, raw = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True, "read_only": True})
        self.source["rows"].append(task("T02"))
        self.save()
        self.assertEqual(
            json.loads(self.request("/api/snapshot")[2])["summary"]["assigned"], 2
        )

    def test_api_and_static_routes_have_no_store_csp_and_no_cors(self):
        for route in (
            "/",
            "/assets/app.js",
            "/assets/styles.css",
            "/api/snapshot",
            "/api/health",
        ):
            with self.subTest(route=route):
                status, headers, raw = self.request(route)
                self.assertEqual(status, 200)
                self.assertTrue(raw)
                self.assertIn("no-store", headers["Cache-Control"])
                policy = headers["Content-Security-Policy"]
                self.assertIn("default-src 'none'", policy)
                self.assertIn("script-src 'self'", policy)
                self.assertIn("connect-src 'self'", policy)
                self.assertIn("frame-ancestors 'none'", policy)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                status, head_headers, body = self.request(route, method="HEAD")
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertEqual(head_headers["Content-Type"], headers["Content-Type"])

        for method in ("GET", "HEAD"):
            with self.subTest(route="/favicon.ico", method=method):
                status, headers, body = self.request("/favicon.ico", method=method)
                self.assertEqual(status, 204)
                self.assertEqual(body, b"")
                self.assertIn("no-store", headers["Cache-Control"])

    def test_host_and_cross_origin_requests_are_rejected(self):
        for headers in (
            {"Host": "evil.invalid"},
            {"Origin": "https://evil.invalid"},
            {"Origin": "null"},
            {"Host": "127.0.0.1:1"},
        ):
            with self.subTest(headers=headers):
                status, _headers, _raw = self.request("/api/snapshot", headers=headers)
                self.assertGreaterEqual(status, 400)
        origin = f"http://127.0.0.1:{self.port}"
        self.assertEqual(
            self.request("/api/snapshot", headers={"Origin": origin})[0], 200
        )

    def test_traversal_arbitrary_paths_and_writes_do_not_read_or_change_private_files(
        self,
    ):
        private = self.root / "private.txt"
        private.write_text("PRIVATE_SENTINEL")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        for target in (
            "/../private.txt",
            "/%2e%2e/private.txt",
            "/assets/../../private.txt",
            "/api/snapshot/../../private.txt",
            "/api/snapshot?path=private.txt",
        ):
            with self.subTest(target=target):
                status, _headers, raw = self.request(target)
                if target.startswith("/api/snapshot?"):
                    self.assertNotIn(b"PRIVATE_SENTINEL", raw)
                else:
                    self.assertGreaterEqual(status, 400)
                self.assertNotIn(b"PRIVATE_SENTINEL", raw)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assertGreaterEqual(
                    self.request("/api/snapshot", method=method)[0], 400
                )
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_missing_and_duplicate_hosts_fail_closed(self):
        for hosts in ([], [f"127.0.0.1:{self.port}", "evil.invalid"]):
            with self.subTest(hosts=hosts):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", self.port, timeout=3
                )
                try:
                    connection.putrequest("GET", "/api/snapshot", skip_host=True)
                    for host in hosts:
                        connection.putheader("Host", host)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertGreaterEqual(response.status, 400)
                    self.assertNotIn(str(self.path).encode(), response.read())
                finally:
                    connection.close()

    def test_nonloopback_bind_is_refused(self):
        for host in ("0.0.0.0", "192.0.2.1", "example.invalid"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                make_server(self.path, host=host, port=0)
