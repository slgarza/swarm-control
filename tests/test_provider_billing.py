"""Synthetic provider billing evidence; no real credentials, accounts or calls."""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx

from swarm_control import provider_billing as billing

NOW = datetime(2026, 1, 2, tzinfo=UTC)
START = int((NOW - timedelta(days=1)).timestamp())
END = int(NOW.timestamp())
SECRET = "sk-admin-synthetic-never-persist-this-value"


def observation(**changes):
    value = {
        "version": 1, "provider": "openai", "source_kind": "dashboard_observation",
        "recorded_at_utc": "2026-01-02T00:00:00Z", "observed_at_utc": None,
        "period": {"start_utc": None, "end_utc": None, "label": "Unspecified dashboard interval"},
        "scope": {"level": "organization", "project_ids": [], "attribution": "unassigned"},
        "spend_usd": "1.23", "monthly_reference_usd": "10.00",
        "reference_kind": "dashboard_reference", "source_sha256": "0" * 64,
        "verified_by_provider_api": False, "billing_reconciled": False,
        "authorizes_spend": False, "provider_credit_balance_usd": None,
    }
    value.update(changes)
    return value


def page(amount=0.125, *, project="proj_synthetic", start=START, end=END,
         more=False, cursor=None, currency="usd"):
    return {
        "object": "page", "data": [{"object": "bucket", "start_time": start, "end_time": end,
                                     "results": [{"object": "organization.costs.result",
                                                  "project_id": project,
                                                  "amount": {"value": amount, "currency": currency}}]}],
        "has_more": more, "next_page": cursor,
    }


class ProviderBillingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        (self.root / ".gitignore").write_text(".swarm/\n")

    def sync(self, payload=None, *, handler=None, **changes):
        if handler is None:
            handler = lambda request: httpx.Response(200, json=payload if payload is not None else page())
        options = {"start_time": START, "end_time": END, "project_ids": ("proj_synthetic",),
                   "admin_key": SECRET, "transport": httpx.MockTransport(handler), "now": NOW}
        options.update(changes)
        return billing.sync_openai(**options)

    def test_snapshot_requires_explicit_provenance_and_cannot_claim_credit_or_authorization(self):
        value = billing.validate_snapshot(observation())
        self.assertEqual(value["spend_usd"], "1.23")
        self.assertIsNone(value["observed_at_utc"])
        self.assertFalse(value["billing_reconciled"])
        self.assertIsNone(value["provider_credit_balance_usd"])
        for change in ({"authorizes_spend": True}, {"billing_reconciled": True},
                       {"provider_credit_balance_usd": "9.00"}, {"verified_by_provider_api": True},
                       {"provider": []}, {"scope": []}, {"spend_usd": "NaN"},
                       {"spend_usd": True}, {"extra": "unapproved raw billing field"}):
            with self.subTest(change=change), self.assertRaises(billing.BillingError):
                billing.validate_snapshot(observation(**change))

    def test_explicit_admin_key_is_required_without_using_model_worker_keys(self):
        calls = []
        with (
            patch.dict(os.environ, {"CODEX_API_KEY": SECRET, "OPENAI_API_KEY": SECRET}, clear=True),
            self.assertRaisesRegex(billing.BillingError, "admin_key_missing"),
        ):
            self.sync(admin_key=None, handler=lambda request: calls.append(request))
        self.assertEqual(calls, [])
        self.assertFalse((self.root / ".swarm").exists())

    def test_api_uses_only_read_only_get_and_preserves_exact_usd_decimal_scope(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url).split("?")[0], billing.API_URL)
            self.assertEqual(request.headers["Authorization"], "Bearer " + SECRET)
            self.assertEqual(request.url.params.get_list("project_ids"), ["proj_synthetic"])
            self.assertEqual(request.url.params["group_by"], "project_id")
            self.assertEqual(request.url.params["bucket_width"], "1d")
            return httpx.Response(200, content=json.dumps(page(0.125)).encode())

        value = self.sync(handler=handler)
        self.assertEqual(len(calls), 1)
        self.assertEqual(value["spend_usd"], "0.125")
        self.assertTrue(value["verified_by_provider_api"])
        self.assertFalse(value["billing_reconciled"])
        self.assertEqual(value["scope"], {"level": "project", "project_ids": ["proj_synthetic"],
                                          "attribution": "unassigned"})
        self.assertIsNone(value["monthly_reference_usd"])
        self.assertIsNone(value["provider_credit_balance_usd"])

    def test_organization_scope_requires_explicit_opt_in_and_never_attributes_run_cost(self):
        for changes in ({"project_ids": ()}, {"organization_scope": True},
                        {"project_ids": (), "organization_scope": "true"}):
            with self.subTest(changes=changes), self.assertRaises(billing.BillingError):
                self.sync(**changes)
        value = self.sync(project_ids=(), organization_scope=True)
        self.assertEqual(value["scope"]["level"], "organization")
        self.assertEqual(value["scope"]["project_ids"], [])
        self.assertEqual(value["scope"]["attribution"], "unassigned")

    def test_all_pages_count_once_with_decimal_precision_and_preserved_original_bytes(self):
        first = page(0.1, start=START, end=START + 43200, more=True, cursor="next-synthetic")
        second = page(0.2, start=START + 43200, end=END)
        raws = [json.dumps(first).encode(), json.dumps(second).encode()]
        calls = []

        def handler(request):
            calls.append(request)
            if len(calls) == 2:
                self.assertEqual(request.url.params["page"], "next-synthetic")
            return httpx.Response(200, content=raws[len(calls) - 1])

        value = self.sync(handler=handler)
        self.assertEqual(value["spend_usd"], "0.3")
        saved = billing.save_snapshot(self.root, value)
        stored = json.loads(saved.read_text())["snapshot"]
        source = saved.parent / f"source-{stored['source_sha256']}.json"
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), stored["source_sha256"])
        preserved = json.loads(source.read_bytes())
        self.assertEqual([base64.b64decode(raw) for raw in preserved["responses_base64"]], raws)
        self.assertNotIn(SECRET, source.read_text())

    def test_many_decimal_places_are_not_silently_rounded(self):
        raw = json.dumps(page()).replace('0.125', '0.123456789012345678901234567890')
        value = self.sync(handler=lambda request: httpx.Response(200, content=raw.encode()))
        self.assertEqual(Decimal(value["spend_usd"]), Decimal("0.123456789012345678901234567890"))

    def test_duplicate_buckets_cursors_groups_and_incomplete_pages_are_rejected(self):
        duplicate = page(more=True, cursor="duplicate")
        for payload in (duplicate, page(more=True, cursor=None),
                        {"object": "page", "data": [], "has_more": True, "next_page": "empty"},
                        page(more=False, cursor="unexpected")):
            with self.subTest(payload=payload), self.assertRaises(billing.BillingError):
                self.sync(payload)
        groups = page()
        groups["data"][0]["results"] *= 2
        with self.assertRaisesRegex(billing.BillingError, "provider_pagination_invalid"):
            self.sync(groups)
        with patch.object(billing, "MAX_PAGES", 1), self.assertRaisesRegex(billing.BillingError, "pagination"):
            self.sync(page(more=True, cursor="unread-next-page"))

    def test_mismatched_currency_scope_period_and_missing_amount_do_not_become_zero(self):
        missing = page()
        del missing["data"][0]["results"][0]["amount"]
        for payload, code in ((page(currency="eur"), "currency"), (page(project="proj_other"), "scope"),
                              (page(start=START - 1), "period"), (page(end=END + 1), "period"),
                              (missing, "currency")):
            with self.subTest(code=code), self.assertRaisesRegex(billing.BillingError, code):
                self.sync(payload)
        empty = self.sync({"object": "page", "data": [], "has_more": False, "next_page": None})
        self.assertEqual(empty["spend_usd"], "0")

    def test_partial_windows_are_accepted_only_for_exact_bounded_provider_buckets(self):
        value = self.sync(page(start=START + 123, end=END - 123),
                          start_time=START + 123, end_time=END - 123)
        self.assertEqual(value["spend_usd"], "0.125")
        with self.assertRaisesRegex(billing.BillingError, "provider_period_mismatch"):
            self.sync(page(), start_time=START + 123, end_time=END - 123)

    def test_api_permissions_errors_redirects_and_invalid_json_never_leak_server_or_key(self):
        for status, expected in ((401, "permission"), (403, "permission"), (429, "rate_limited"),
                                 (500, "request_failed"), (302, "request_failed")):
            with self.subTest(status=status), self.assertRaises(billing.BillingError) as error:
                self.sync(handler=lambda request, status=status: httpx.Response(status, text=SECRET,
                                                                 headers={"location": "https://example.invalid"}))
            self.assertIn(expected, str(error.exception))
            self.assertNotIn(SECRET, str(error.exception))
        with self.assertRaisesRegex(billing.BillingError, "provider_response_invalid"):
            self.sync(handler=lambda request: httpx.Response(200, text=SECRET))
        with self.assertRaisesRegex(billing.BillingError, "provider_request_failed"):
            self.sync(handler=lambda request: (_ for _ in ()).throw(httpx.ReadTimeout(SECRET)))

    def test_response_and_cache_size_bounds_fail_closed(self):
        with patch.object(billing, "MAX_PAGE_BYTES", 10), self.assertRaisesRegex(billing.BillingError, "too_large"):
            self.sync()
        with patch.object(billing, "MAX_TOTAL_BYTES", 10), self.assertRaisesRegex(billing.BillingError, "too_large"):
            self.sync()
        with patch.object(billing, "MAX_RECORDS", 0), self.assertRaisesRegex(billing.BillingError, "response_invalid"):
            self.sync()

    def test_manual_import_preserves_source_but_cannot_forge_api_verification_or_future_recording(self):
        source = self.root / "synthetic-observation.json"
        raw = json.dumps(observation(recorded_at_utc="2099-01-01T00:00:00Z"), indent=3).encode()
        source.write_bytes(raw)
        before = datetime.now(UTC)
        saved = billing.import_snapshot(self.root, source)
        value = billing.load_latest_snapshot(self.root)
        self.assertLess(datetime.fromisoformat(value["recorded_at_utc"]),
                        before + timedelta(seconds=3))
        self.assertIsNone(value["observed_at_utc"])
        self.assertEqual(value["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual((saved.parent / f"source-{value['source_sha256']}.json").read_bytes(), raw)
        for forged in (observation(source_kind="openai_costs_api"), observation(verified_by_provider_api=True)):
            source.write_text(json.dumps(forged))
            with self.assertRaisesRegex(billing.BillingError, "cannot_claim_api"):
                billing.import_snapshot(self.root, source)

    def test_cache_uses_latest_ingestion_and_preserves_local_ledger_bytes(self):
        state = self.root / ".swarm"
        state.mkdir()
        ledger = state / "control.sqlite3"
        ledger.write_bytes(b"retained synthetic allocation history")
        first = billing.save_snapshot(self.root, observation(), now=NOW)
        second = billing.save_snapshot(self.root, observation(spend_usd="2.25"), now=NOW + timedelta(seconds=1))
        self.assertNotEqual(first, second)
        self.assertEqual(billing.load_latest_snapshot(self.root)["spend_usd"], "2.25")
        self.assertEqual(ledger.read_bytes(), b"retained synthetic allocation history")
        self.assertIsNone(billing.load_latest_snapshot(self.root, "modal"))
        for path in (first, second, *first.parent.glob("source-*.json")):
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_new_failed_sync_is_visible_without_deleting_previous_api_spend(self):
        billing.save_snapshot(self.root, self.sync(), now=NOW)
        self.assertEqual(billing.load_latest_sync_attempt(self.root)["status"], "success")
        billing.record_failure(self.root, "provider_permission_denied")
        latest = billing.load_latest_sync_attempt(self.root)
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["error_code"], "provider_permission_denied")
        self.assertEqual(billing.load_latest_snapshot(self.root)["spend_usd"], "0.125")
        billing.record_failure(self.root, SECRET)
        self.assertEqual(billing.load_latest_sync_attempt(self.root)["error_code"], "provider_sync_failed")
        for path in (self.root / ".swarm/billing").glob("*.json"):
            self.assertNotIn(SECRET, path.read_text())

    def test_status_loaders_do_not_create_missing_state_or_make_network_calls(self):
        with patch.object(httpx, "Client", side_effect=AssertionError("unexpected network client")):
            self.assertIsNone(billing.load_latest_snapshot(self.root))
            self.assertIsNone(billing.load_latest_sync_attempt(self.root))
        self.assertFalse((self.root / ".swarm").exists())

    def test_symlinks_and_unignored_or_public_cache_are_rejected(self):
        external = self.root / "external"
        external.mkdir()
        (self.root / ".swarm").symlink_to(external)
        with self.assertRaisesRegex(billing.BillingError, "directory_unsafe"):
            billing.save_snapshot(self.root, observation())
        (self.root / ".swarm").unlink()
        (self.root / ".gitignore").write_text("")
        with self.assertRaisesRegex(billing.BillingError, "not_ignored"):
            billing.save_snapshot(self.root, observation())
        (self.root / ".gitignore").write_text(".swarm/\n")
        directory = self.root / ".swarm/billing"
        directory.chmod(0o777)
        with self.assertRaisesRegex(billing.BillingError, "must_be_private"):
            billing.load_latest_snapshot(self.root)

    def test_corrupt_or_public_receipt_source_cannot_claim_a_verified_cache(self):
        saved = billing.save_snapshot(self.root, self.sync())
        saved.chmod(0o644)
        with self.assertRaises(billing.BillingError):
            billing.load_latest_snapshot(self.root)
        saved.chmod(0o600)
        value = json.loads(saved.read_text())["snapshot"]
        source = saved.parent / f"source-{value['source_sha256']}.json"
        source.write_text("tampered original")
        with self.assertRaisesRegex(billing.BillingError, "digest_mismatch"):
            billing.load_latest_snapshot(self.root)

    def test_concurrent_saves_publish_complete_append_only_receipts_and_deduplicate_source(self):
        billing.save_snapshot(self.root, observation())
        value = self.sync()
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(lambda _: billing.save_snapshot(self.root, value), range(8)))
        self.assertEqual(len(set(paths)), 8)
        self.assertTrue(all(json.loads(path.read_text())["snapshot"]["spend_usd"] == "0.125" for path in paths))
        self.assertEqual(billing.load_latest_snapshot(self.root)["spend_usd"], "0.125")
        self.assertEqual(len(list(paths[0].parent.glob(f"source-{value['source_sha256']}.json"))), 1)
        self.assertEqual(list(paths[0].parent.glob(".pending-*")), [])

    def test_receipt_integrity_rejects_changed_normalized_spend_scope_or_period(self):
        for original in (observation(), self.sync()):
            for field, replacement in (("spend_usd", "0"),
                                       ("scope", {"level": "organization", "project_ids": [], "attribution": "unassigned"}),
                                       ("period", {"start_utc": None, "end_utc": None, "label": "changed"})):
                with self.subTest(api=original["verified_by_provider_api"], field=field):
                    saved = billing.save_snapshot(self.root, original)
                    receipt = json.loads(saved.read_text())
                    # Make a meaningful scope modification even for an org observation.
                    if receipt["snapshot"][field] == replacement:
                        replacement = {"level": "project", "project_ids": ["proj_other"], "attribution": "unassigned"}
                    receipt["snapshot"][field] = replacement
                    saved.write_text(json.dumps(receipt))
                    with self.assertRaisesRegex(billing.BillingError, "snapshot_digest_mismatch"):
                        billing.load_latest_snapshot(self.root)
                    saved.unlink()


if __name__ == "__main__":
    unittest.main()
