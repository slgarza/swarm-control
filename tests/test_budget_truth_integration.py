"""Synthetic end-to-end budget evidence tests; no provider or real ledger access."""

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from swarm_control import budget_policy, budget_reporting, cli, provider_billing
from swarm_control.pilot_state import Ledger
from swarm_control.project import load_project
from tests.support import make_project


class BudgetTruthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = make_project(self.root / "target")
        (self.project.root / ".gitignore").write_text(".swarm/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "Ignore private runtime evidence")
        self.ledger = Ledger(self.project.root / ".swarm/control.sqlite3")
        self.ledger.allocate("prior_failed_attempt", 1800, infra_cents=670)
        with self.ledger.connect() as database:
            database.execute("DROP TABLE budget_policy")
        self.before = budget_policy.inspect(self.project)
        self.profile_bytes = self.project.path.read_bytes()

    def git(self, *arguments):
        subprocess.run(["git", *arguments], cwd=self.project.root, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def observation(self):
        return {
            "version": 1, "provider": "openai", "source_kind": "dashboard_observation",
            "recorded_at_utc": "2026-11-11T12:00:00Z", "observed_at_utc": None,
            "period": {"start_utc": None, "end_utc": None, "label": "Displayed reporting window"},
            "scope": {"level": "organization", "project_ids": [], "attribution": "unassigned"},
            "spend_usd": "0.75", "monthly_reference_usd": "10.00",
            "reference_kind": "dashboard_monthly_reference",
            "source_sha256": "0" * 64, "verified_by_provider_api": False,
            "billing_reconciled": False, "authorizes_spend": False,
            "provider_credit_balance_usd": None,
        }

    def write_observation(self, value=None):
        path = self.root / "private-observation.json"
        path.write_text(json.dumps(value or self.observation()))
        path.chmod(0o600)
        return path

    def assert_history_preserved(self):
        after = budget_policy.inspect(self.project)
        self.assertEqual(after["state_sha256"], self.before["state_sha256"])
        self.assertEqual(after["allocation_totals"], {"model_cents": 1800, "infra_cents": 670})
        self.assertEqual(after["remaining_allowances"], {"model_cents": 0, "infra_cents": 30})
        self.assertFalse(after["policy_table_present"])
        self.assertEqual(self.project.path.read_bytes(), self.profile_bytes)

    def run_cli(self, *arguments):
        output = io.StringIO()
        with patch("sys.argv", ["swarm-control", "budget", *arguments]), redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        return json.loads(output.getvalue())

    def test_cli_manual_evidence_cannot_release_legacy_reservations_or_invent_provider_funds(self):
        source = self.write_observation()
        with (
            patch("swarm_control.pilot_state.Ledger", side_effect=AssertionError("ledger initialized")),
            patch.object(provider_billing.httpx, "Client", side_effect=AssertionError("network requested")),
        ):
            imported = self.run_cli("import-spend", "--project", str(self.project.path), "--file", str(source))
            status = self.run_cli("status", "--project", str(self.project.path), "--json")
        self.assertTrue(imported["imported"])
        evidence = status["reported_provider_spend"]["openai"]
        self.assertEqual(evidence["reported_spend_usd"], "0.75")
        self.assertEqual(evidence["monthly_reference_usd"], "10.00")
        self.assertEqual(evidence["freshness"]["status"], "unknown")
        self.assertFalse(evidence["verified_by_provider_api"])
        self.assertEqual(evidence["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertIsNone(evidence["provider_budget_remaining_usd"])
        self.assertIsNone(status["available_provider_funds_usd"])
        self.assertIsNone(status["repository_actual_cost_usd"])
        self.assertFalse(status["authorizes_spend"])
        self.assert_history_preserved()

    def test_provider_api_zero_stays_scoped_unreconciled_and_cannot_be_forged_by_import(self):
        start = int(datetime(2026, 11, 10, tzinfo=UTC).timestamp())
        end = start + 86400
        requests = []

        def response(request):
            requests.append(request)
            return httpx.Response(200, json={
                "object": "page", "has_more": False, "next_page": None,
                "data": [{"object": "bucket", "start_time": start, "end_time": end,
                          "results": [{"object": "organization.costs.result", "project_id": "proj_test",
                                       "amount": {"value": 0, "currency": "usd"}}]}],
            })

        snapshot = provider_billing.sync_openai(
            start_time=start, end_time=end, project_ids=["proj_test"],
            admin_key="synthetic-billing-admin", transport=httpx.MockTransport(response),
            now=datetime.fromtimestamp(end, UTC),
        )
        provider_billing.save_snapshot(self.project.root, snapshot)
        status = budget_reporting.build_status(self.project, now=datetime.fromtimestamp(end, UTC))
        evidence = status["reported_provider_spend"]["openai"]
        self.assertEqual(evidence["reported_spend_usd"], "0")
        self.assertTrue(evidence["verified_by_provider_api"])
        self.assertEqual(evidence["scope"], {"level": "project", "project_ids": ["proj_test"], "attribution": "unassigned"})
        self.assertFalse(evidence["billing_reconciled"])
        self.assertIsNone(evidence["repository_attributed_spend_usd"])
        self.assertIsNone(status["reported_provider_spend"]["modal"]["reported_spend_usd"])
        self.assertEqual([(request.method, str(request.url).split("?")[0]) for request in requests],
                         [("GET", provider_billing.API_URL)])
        self.assertEqual(requests[0].headers["Authorization"], "Bearer synthetic-billing-admin")
        directory = self.project.root / ".swarm/billing"
        source = directory / f"source-{evidence['source_sha256']}.json"
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), evidence["source_sha256"])
        self.assertTrue(all(b"synthetic-billing-admin" not in path.read_bytes() for path in directory.iterdir()))
        forged = self.write_observation(dict(snapshot))
        with self.assertRaisesRegex(ValueError, "cannot_claim_api_verification"):
            budget_reporting.import_spend(self.project, forged)
        self.assertEqual(budget_reporting.build_status(self.project)["reported_provider_spend"]["openai"]["source_sha256"],
                         evidence["source_sha256"])
        self.assert_history_preserved()

    def test_worktrees_read_and_write_one_ignored_common_evidence_cache(self):
        worktree_root = self.root / "worktree"
        self.git("worktree", "add", "-q", "--detach", str(worktree_root), "HEAD")
        worktree = load_project(worktree_root / "swarm.toml")
        imported = budget_reporting.import_spend(worktree, self.write_observation())
        self.assertEqual(Path(imported["receipt"]).parent, self.project.root / ".swarm/billing")
        self.assertFalse((worktree_root / ".swarm").exists())
        original = budget_reporting.build_status(self.project)
        linked = budget_reporting.build_status(worktree)
        self.assertEqual(original["reported_provider_spend"], linked["reported_provider_spend"])
        self.assertEqual(original["local_admission_policy"], linked["local_admission_policy"])
        self.assert_history_preserved()
