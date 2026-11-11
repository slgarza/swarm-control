"""Offline status evidence stays separate from authorization and reservations."""

import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from swarm_control import budget_policy as budget
from swarm_control import budget_reporting as reporting
from swarm_control.pilot_state import Ledger
from tests.support import make_project

NOW = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)


def observation(*, provider="openai", api=False, observed_at=None, spend="1.23", monthly="50"):
    return {
        "version": 1, "provider": provider,
        "source_kind": "openai_costs_api" if api else "dashboard_observation",
        "recorded_at_utc": NOW.isoformat(), "observed_at_utc": observed_at,
        "period": {"start_utc": "2026-05-01T00:00:00+00:00" if api else None,
                   "end_utc": NOW.isoformat() if api else None,
                   "label": "Current UTC month" if api else "Dashboard selected period"},
        "scope": {"level": "organization", "project_ids": [], "attribution": "unassigned"},
        "spend_usd": spend, "monthly_reference_usd": monthly,
        "reference_kind": "displayed_monthly_reference" if monthly is not None else None,
        "source_sha256": "a" * 64,
        "verified_by_provider_api": api, "billing_reconciled": False,
        "authorizes_spend": False, "provider_credit_balance_usd": None,
    }


def api_snapshot():
    from swarm_control.provider_billing import sync_openai

    start = int(datetime(2026, 5, 1, tzinfo=UTC).timestamp())
    end = int(datetime(2026, 5, 12, tzinfo=UTC).timestamp())
    def reply(_request):
        return httpx.Response(200, json={"object": "page", "has_more": False, "next_page": None,
            "data": [{"object": "bucket", "start_time": start, "end_time": start + 86400,
                      "results": [{"object": "organization.costs.result", "project_id": "proj_synthetic",
                                   "amount": {"value": 1.23, "currency": "usd"}}]}]})
    return sync_openai(start_time=start, end_time=end, organization_scope=True,
                       admin_key="synthetic-admin-key", transport=httpx.MockTransport(reply), now=NOW)


class BudgetReportingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = make_project(self.root)
        (self.root / ".gitignore").write_text(".swarm/\n")
        self.path = self.root / ".swarm/control.sqlite3"
        ledger = Ledger(self.path)
        ledger.allocate("retained-synthetic-batch", 1800, infra_cents=670)

    def status(self, receipt=None, **kwargs):
        loader = lambda _root, provider: receipt if provider == "openai" else None
        return reporting.build_status(self.project, now=NOW, snapshot_loader=loader, **kwargs)

    def test_local_zero_allowance_never_becomes_zero_provider_balance(self):
        before = budget.inspect(self.project)["state_sha256"]
        status = self.status()
        local = status["local_admission_policy"]
        self.assertEqual(local["limits_cents"], {"model": 1800, "infrastructure": 700})
        self.assertEqual(local["reserved_cents"], {"model": 1800, "infrastructure": 670})
        self.assertEqual(local["unreserved_cents"], {"model": 0, "infrastructure": 30})
        self.assertFalse(local["provider_balance"])
        self.assertFalse(local["measures_actual_spend"])
        for key in ("provider_credit_balance_usd", "available_provider_funds_usd", "repository_actual_cost_usd"):
            self.assertIsNone(status[key])
        self.assertFalse(status["authorizes_spend"])
        self.assertFalse(status["launch_readiness_verified"])
        self.assertFalse(status["local_allocations_changed"])
        self.assertEqual(budget.inspect(self.project)["state_sha256"], before)

    def test_dashboard_observation_retains_real_reported_spend_without_invented_headroom(self):
        status = self.status(observation())
        source = status["reported_provider_spend"]["openai"]
        self.assertTrue(source["available"])
        self.assertEqual(source["reported_spend_usd"], "1.23")
        self.assertEqual(source["monthly_reference_usd"], "50")
        self.assertFalse(source["verified_by_provider_api"])
        self.assertEqual(source["scope"]["attribution"], "unassigned")
        self.assertIsNone(source["provider_budget_remaining_usd"])
        self.assertIsNone(source["provider_credit_balance_usd"])
        self.assertIsNone(source["repository_attributed_spend_usd"])
        self.assertFalse(source["billing_reconciled"])
        self.assertEqual(source["freshness"]["status"], "unknown")
        self.assertFalse(status["reported_provider_spend"]["modal"]["available"])
        text = reporting.format_status(status)
        self.assertIn("reported spend USD 1.23", text)
        self.assertIn("operator-supplied observation/export; not API verified", text)
        self.assertIn("unreserved local allowance USD 0.00", text)
        self.assertNotIn("USD 48.77", text)

    def test_api_evidence_shows_exact_scope_window_and_reporting_age(self):
        source = observation(api=True, observed_at=(NOW - timedelta(hours=2)).isoformat(), monthly=None)
        source["scope"] = {"level": "project", "project_ids": ["proj_synthetic"], "attribution": "unassigned"}
        status = self.status(source)
        report = status["reported_provider_spend"]["openai"]
        self.assertTrue(report["verified_by_provider_api"])
        self.assertEqual(report["freshness"]["status"], "recent")
        self.assertEqual(report["freshness"]["age_seconds"], 7200)
        self.assertEqual(report["scope"]["project_ids"], ["proj_synthetic"])
        self.assertEqual(reporting._aware(report["period"]["start_utc"]), reporting._aware(source["period"]["start_utc"]))
        self.assertIsNone(report["repository_attributed_spend_usd"])
        self.assertIn("scope: project", reporting.format_status(status))
        self.assertIn("proj_synthetic", reporting.format_status(status))

    def test_fresh_import_time_cannot_make_an_old_observation_current(self):
        source = observation(observed_at=(NOW - timedelta(days=3)).isoformat())
        status = self.status(source)
        self.assertEqual(status["reported_provider_spend"]["openai"]["freshness"]["status"], "stale")
        self.assertEqual(reporting._aware(status["reported_provider_spend"]["openai"]["recorded_at_utc"]), NOW)
        self.assertIn("freshness: stale", reporting.format_status(status))

    def test_invalid_or_mismatched_provider_receipts_are_unknown_not_zero(self):
        for change in ({"provider": "modal"}, {"spend_usd": "NaN"}, {"authorizes_spend": True},
                       {"source_kind": "untrusted-source-with-secret"}):
            source = observation()
            source.update(change)
            status = self.status(source)
            report = status["reported_provider_spend"]["openai"]
            self.assertFalse(report["available"])
            self.assertIsNone(report["reported_spend_usd"])
            self.assertNotIn("secret", json.dumps(report))

    def test_unreadable_cache_is_unknown_and_raw_error_is_not_exposed(self):
        def inaccessible(_root, _provider):
            raise ValueError("synthetic-private-credential-output")

        status = reporting.build_status(self.project, now=NOW, snapshot_loader=inaccessible)
        self.assertIsNone(status["reported_provider_spend"]["openai"]["reported_spend_usd"])
        self.assertNotIn("synthetic-private-credential", json.dumps(status))
        self.assertFalse(status["authorizes_spend"])

    def test_default_status_never_calls_provider_or_initializes_a_missing_ledger(self):
        project = make_project(self.root / "new-target")
        with (
            patch("swarm_control.pilot_state.Ledger", side_effect=AssertionError("ledger constructor called")),
            patch("swarm_control.provider_billing.sync_openai", side_effect=AssertionError("provider called")),
        ):
            status = reporting.build_status(project, now=NOW)
        self.assertFalse((project.root / ".swarm").exists())
        self.assertFalse(status["local_admission_policy"]["ledger_initialized"])
        self.assertIsNone(status["local_admission_policy"]["reserved_cents"])
        self.assertIsNone(status["local_admission_policy"]["unreserved_cents"])
        self.assertEqual(status["network_requests_made"], 0)

    def test_profile_policy_mismatch_is_reported_separately_from_provider_funds(self):
        self.project.path.write_text(self.project.path.read_text().replace("model_limit_cents = 1800", "model_limit_cents = 9000"))
        from swarm_control.project import load_project

        changed = load_project(self.project.path)
        status = reporting.build_status(changed, now=NOW, snapshot_loader=lambda _root, _provider: None)
        self.assertFalse(status["local_admission_policy"]["profile_matches_policy"])
        self.assertEqual(status["local_admission_policy"]["limits_cents"]["model"], 1800)
        self.assertEqual(status["local_admission_policy"]["configured_limits_cents"]["model"], 9000)
        self.assertIsNone(status["available_provider_funds_usd"])
        self.assertIn("Profile limits differ", reporting.format_status(status))

    def test_cli_status_text_and_json_keep_reservation_scope_explicit(self):
        for flag in ([], ["--json"]):
            output = io.StringIO()
            with patch("sys.argv", ["budget", "status", "--project", str(self.project.path), *flag]), patch("sys.stdout", output):
                self.assertEqual(budget.main(), 0)
            if flag:
                parsed = json.loads(output.getvalue())
                self.assertIsNone(parsed["provider_credit_balance_usd"])
                self.assertFalse(parsed["authorizes_spend"])
            else:
                self.assertIn("LOCAL ADMISSION POLICY (not provider funds or actual spend)", output.getvalue())
                self.assertIn("Provider credit balance / available funds: unknown", output.getvalue())

    def test_cli_sync_requires_explicit_scope_and_rejects_ambiguous_dates_before_query(self):
        for extra in ([], ["--organization-scope", "--start", "2026-05-11T12:00:00"]):
            with (patch("sys.argv", ["budget", "sync-openai", "--project", str(self.project.path), *extra]),
                  patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as raised):
                budget.main()
            self.assertEqual(raised.exception.code, 2)
        self.assertEqual(reporting.parse_provider_time("2026-05-01"), int(datetime(2026, 5, 1, tzinfo=UTC).timestamp()))
        self.assertEqual(reporting.parse_provider_time("2026-05-01T02:00:00+02:00"), reporting.parse_provider_time("2026-05-01"))

    def test_explicit_sync_saves_provider_evidence_without_releasing_local_reservations(self):
        source = api_snapshot()
        before = budget.inspect(self.project)["state_sha256"]
        with patch("swarm_control.provider_billing.sync_openai", return_value=source) as sync:
            result = reporting.sync_spend(self.project, organization_scope=True, now=NOW)
        self.assertTrue(result["synced"])
        call = sync.call_args.kwargs
        self.assertEqual(call["start_time"], int(datetime(2026, 5, 1, tzinfo=UTC).timestamp()))
        self.assertEqual(call["end_time"], int(datetime(2026, 5, 12, tzinfo=UTC).timestamp()))
        self.assertFalse(result["authorizes_spend"])
        self.assertEqual(budget.inspect(self.project)["state_sha256"], before)

    def test_failed_sync_retains_previous_evidence_and_never_persists_secret_exception(self):
        from swarm_control.provider_billing import save_snapshot

        previous = save_snapshot(self.project.root, observation())
        before_evidence = previous.read_bytes()
        before_ledger = budget.inspect(self.project)["state_sha256"]
        secret = "synthetic-secret-in-unsafe-exception"
        with (patch("swarm_control.provider_billing.sync_openai", side_effect=ValueError(secret)),
              self.assertRaisesRegex(ValueError, "provider_sync_failed") as raised):
            reporting.sync_spend(self.project, organization_scope=True, now=NOW)
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(previous.read_bytes(), before_evidence)
        self.assertEqual(budget.inspect(self.project)["state_sha256"], before_ledger)
        for path in (self.root / ".swarm/billing").glob("*.json"):
            self.assertNotIn(secret, path.read_text())

    def test_import_cannot_promote_operator_data_to_api_verified_or_change_policy(self):
        source = observation()
        path = self.root / "spend-observation.json"
        path.write_text(json.dumps(source))
        before = budget.inspect(self.project)["state_sha256"]
        result = reporting.import_spend(self.project, path)
        self.assertTrue(result["imported"])
        self.assertFalse(result["authorizes_spend"])
        self.assertEqual(budget.inspect(self.project)["state_sha256"], before)
        source["verified_by_provider_api"] = True
        source["source_kind"] = "openai_costs_api"
        source["observed_at_utc"] = NOW.isoformat()
        path.write_text(json.dumps(source))
        with self.assertRaises(ValueError):
            reporting.import_spend(self.project, path)

    def test_clock_and_freshness_settings_require_safe_unambiguous_types(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            reporting.build_status(self.project, now=datetime(2026, 5, 11), snapshot_loader=lambda _r, _p: None)
        for maximum in (0, True, -1, 32 * 86400):
            with self.assertRaisesRegex(ValueError, "freshness"):
                self.status(max_age_seconds=maximum)

    def test_a_failed_refresh_is_visible_beside_recent_retained_spend(self):
        source = observation(api=True, observed_at=(NOW - timedelta(hours=1)).isoformat(), monthly=None)
        failure = {"status": "failed", "recorded_at_utc": NOW.isoformat(), "error_code": "provider_permission_denied"}
        status = reporting.build_status(self.project, now=NOW,
            snapshot_loader=lambda _r, provider: source if provider == "openai" else None,
            sync_attempt_loader=lambda _r, provider: failure if provider == "openai" else None)
        report = status["reported_provider_spend"]["openai"]
        self.assertEqual(report["reported_spend_usd"], "1.23")
        self.assertEqual(report["freshness"]["status"], "recent")
        self.assertEqual(report["last_sync_attempt"]["status"], "failed")
        self.assertIn("The refresh failed", reporting.format_status(status))
        self.assertFalse(status["authorizes_spend"])

    def test_invalid_sync_attempt_metadata_cannot_expose_raw_error_or_hide_cost(self):
        def invalid(_root, _provider):
            raise ValueError("synthetic-private-sync-error")

        status = reporting.build_status(self.project, now=NOW,
            snapshot_loader=lambda _r, provider: observation() if provider == "openai" else None,
            sync_attempt_loader=invalid)
        self.assertEqual(status["reported_provider_spend"]["openai"]["reported_spend_usd"], "1.23")
        self.assertEqual(status["reported_provider_spend"]["openai"]["last_sync_attempt"]["status"], "unknown")
        self.assertNotIn("synthetic-private-sync-error", json.dumps(status))

    def test_window_including_current_day_is_explicitly_not_final(self):
        source = observation(api=True, observed_at=NOW.isoformat(), monthly=None)
        source["period"]["end_utc"] = "2026-05-12T00:00:00Z"
        status = self.status(source)
        self.assertTrue(status["reported_provider_spend"]["openai"]["period_in_progress"])
        text = reporting.format_status(status)
        self.assertIn("includes the current UTC day", text)
        self.assertIn("not a final invoice", text)
        self.assertIsNone(status["provider_credit_balance_usd"])
