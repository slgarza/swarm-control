"""Preflight checks source facts without spending or mutating durable admission."""

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_control import budget_policy, cli, preflight
from swarm_control.pilot_state import Ledger
from swarm_control.project import load_project
from tests.support import make_project


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = make_project(self.root)
        (self.root / ".gitignore").write_text(".swarm/\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.requests = []
        self.issue_state = "open"
        self.open_prs = []
        self.remote_base = self.base
        self.ci_head = self.base

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "preparation")

    def query(self, path):
        self.requests.append(path)
        if "/pulls?" in path:
            return self.open_prs
        if "/issues/" in path:
            return {"state": self.issue_state}
        if "/compare/" in path:
            return {"status": "identical", "merge_base_commit": {"sha": self.remote_base}}
        if "/check-runs?" in path:
            return {"total_count": 1, "check_runs": [{
                "id": 1, "name": "checks", "app": {"slug": "github-actions"},
                "head_sha": self.ci_head, "status": "completed", "conclusion": "success",
            }]}
        raise AssertionError(f"unexpected test query: {path}")

    def check(self, **kwargs):
        return preflight.build_preflight(
            self.project, ["A1"], base=self.base, query=self.query, **kwargs,
        )

    def test_offline_does_not_query_services_or_initialize_missing_state(self):
        with (
            patch("swarm_control.pilot_state.Ledger", side_effect=AssertionError("Ledger created")),
            patch("swarm_control.preflight._github", side_effect=AssertionError("GitHub called")),
            patch("swarm_control.provider_billing.sync_openai", side_effect=AssertionError("billing called")),
        ):
            result = self.check()
        self.assertTrue(result["local_workload_ready"])
        self.assertTrue(result["preparation_checks_passed"])
        self.assertIsNone(result["github_source_ready"])
        self.assertEqual(self.requests, [])
        self.assertFalse((self.root / ".swarm").exists())
        self.assertFalse(result["launch_ready"])
        self.assertFalse(result["authorizes_spend"])
        self.assertFalse(result["cloud_resources_created"])
        self.assertIn("durable_unreserved_allowance_not_observed", result["local_admission_blockers"])
        self.assertIsNone(result["budget_status"]["available_provider_funds_usd"])

    def test_online_checks_actual_issue_pr_base_and_exact_commit_ci_without_launch(self):
        result = self.check(online=True)
        self.assertTrue(result["local_workload_ready"])
        self.assertTrue(result["github_source_ready"])
        self.assertTrue(result["github"]["issues_and_dependencies_verified"])
        self.assertTrue(result["github"]["base_reachable_from_remote_branch"])
        self.assertTrue(result["github"]["exact_base_ci_green"])
        self.assertEqual(result["github"]["github_get_requests"], 4)
        self.assertFalse(result["launch_ready"])
        self.assertFalse(result["provider_quota_verified"])
        self.assertFalse(result["deployed_gateway_capacity_verified"])
        self.assertIn("owner_launch_authorization_not_verified", result["launch_blockers"])

    def test_closed_missing_issue_or_existing_pr_blocks_real_source_readiness(self):
        for response in ("closed", "missing", "existing_pr"):
            with self.subTest(response=response):
                self.issue_state = "closed" if response == "closed" else "open"
                self.open_prs = [{"number": 10, "title": "[A1] duplicate work"}] if response == "existing_pr" else []

                def query(path, response=response):
                    if response == "missing" and "/issues/" in path:
                        raise ValueError("synthetic-private-API-error")
                    return self.query(path)

                result = preflight.build_preflight(
                    self.project, ["A1"], base=self.base, online=True, query=query,
                )
                self.assertFalse(result["github_source_ready"])
                self.assertFalse(result["preparation_checks_passed"])
                self.assertIn("github_issues_and_dependencies_verified_failed", result["github"]["blockers"])
                self.assertNotIn("synthetic-private", json.dumps(result))

    def test_stale_green_ci_and_unreachable_base_do_not_pass(self):
        self.ci_head = "a" * 40
        self.remote_base = "b" * 40
        result = self.check(online=True)
        self.assertFalse(result["github_source_ready"])
        self.assertFalse(result["github"]["exact_base_ci_green"])
        self.assertFalse(result["github"]["base_reachable_from_remote_branch"])

    def test_incomplete_ci_pagination_cannot_pass(self):
        def query(path):
            payload = self.query(path)
            if "/check-runs?" in path:
                payload["total_count"] = 101
            return payload

        result = preflight.build_preflight(
            self.project, ["A1"], base=self.base, online=True, query=query,
        )
        self.assertFalse(result["github_source_ready"])
        self.assertFalse(result["github"]["exact_base_ci_green"])

    def test_missing_issue_and_declared_blockers_fail_local_metadata(self):
        path = self.project.tasks_dir / "A1.json"
        task = json.loads(path.read_text())
        task.pop("issue")
        task["blockedBy"] = ["design decision"]
        path.write_text(json.dumps(task))
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        result = self.check()
        self.assertFalse(result["local_workload_ready"])
        reasons = result["local_admission"]["blocked_tasks"]["A1"]
        self.assertIn("missing_issue", reasons)
        self.assertIn("declared_blockers", reasons)

    def test_dependency_requires_local_merge_and_real_merge_in_selected_base(self):
        path = self.project.tasks_dir / "A1.json"
        task = json.loads(path.read_text())
        task["dependsOn"] = ["D1"]
        path.write_text(json.dumps(task))
        dependency = self.project.tasks_dir / "D1.json"
        dependency.write_text(json.dumps({"id": "D1", "status": "ready"}))
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        result = self.check()
        self.assertFalse(result["local_workload_ready"])
        dependency.write_text(json.dumps({"id": "D1", "status": "merged", "pullRequest": 10}))
        self.commit()
        self.base = self.git("rev-parse", "HEAD")

        def query(path):
            if path.endswith("/pulls/10"):
                return {"merged": True, "merge_commit_sha": "f" * 40}
            return self.query(path)

        result = preflight.build_preflight(
            self.project, ["A1"], base=self.base, online=True, query=query,
        )
        self.assertTrue(result["local_workload_ready"])
        self.assertFalse(result["github"]["issues_and_dependencies_verified"])

    def test_dirty_or_different_base_is_not_frozen_workload(self):
        (self.root / "src/item.txt").write_text("uncommitted edit")
        result = self.check()
        self.assertIn("target_checkout_has_uncommitted_changes", result["local_workload_blockers"])
        self.commit()
        result = self.check()
        self.assertIn("checkout_head_differs_from_selected_base", result["local_workload_blockers"])

    def test_existing_ledger_bytes_schema_reservations_and_claims_are_preserved(self):
        path = self.root / ".swarm/control.sqlite3"
        ledger = Ledger(path)
        ledger.claim([{"id": "A1"}], "prior-batch", self.base, budget_cents=1800, infra_cents=670)
        with ledger.connect() as db:
            db.execute("DROP TABLE budget_policy")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        state = budget_policy.inspect(self.project)
        with patch("swarm_control.pilot_state.Ledger", side_effect=AssertionError("Ledger created")):
            result = self.check(model_budget="1.00")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(budget_policy.inspect(self.project)["state_sha256"], state["state_sha256"])
        self.assertFalse(budget_policy.inspect(self.project)["policy_table_present"])
        self.assertEqual(result["selected_existing_claims"], {"A1": "running"})
        self.assertFalse(result["local_workload_ready"])
        self.assertIn("local_model_reserve_insufficient", result["local_admission_blockers"])
        self.assertIn("existing_active_or_awaiting_tasks", result["local_admission_blockers"])
        local = result["budget_status"]["local_admission_policy"]
        self.assertEqual(local["unreserved_cents"], {"model": 0, "infrastructure": 30})
        self.assertIsNone(result["budget_status"]["available_provider_funds_usd"])
        self.assertFalse(result["local_allocations_changed"])

    def test_100_worker_requests_caps_and_container_inventory_do_not_assert_quota(self):
        text = self.project.path.read_text().replace(
            "[budget]", "[runtime]\ngateway_concurrency = 100\n[budget]\nmax_live_agents = 100",
        )
        self.project.path.write_text(text)
        self.project = load_project(self.project.path)
        ids = [f"B{number:03d}" for number in range(1, 101)]
        for number, task_id in enumerate(ids, 1):
            (self.project.tasks_dir / f"{task_id}.json").write_text(json.dumps({
                "id": task_id, "title": "Useful leaf", "prompt": "Implement contract",
                "ownership": [f"src/{task_id}"], "acceptance": ["Contract passes"],
                "issue": number, "status": "ready",
            }))
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        result = preflight.build_preflight(
            self.project, ids, workers=100, synchronized=True, base=self.base,
        )
        self.assertTrue(result["local_workload_ready"])
        self.assertEqual(result["local_admission"]["ready_task_count"], 100)
        self.assertTrue(result["configured_gateway_capacity_sufficient"])
        self.assertFalse(result["deployed_gateway_capacity_verified"])
        resources = result["resource_plan"]
        self.assertEqual(resources["logical_container_inventory"]["minimum_count"], 101)
        self.assertEqual(resources["logical_container_inventory"]["recommended_with_one_admin_helper"], 102)
        self.assertEqual(resources["worker_requests"]["total_cpu"], 50)
        self.assertEqual(resources["worker_requests"]["total_memory_mib"], 51200)
        self.assertEqual(resources["worker_caps"]["total_cpu"], 200)
        self.assertEqual(resources["worker_caps"]["total_memory_mib"], 204800)
        self.assertEqual(resources["gateway_request"], {"cpu": 0.25, "memory_mib": 256})
        self.assertFalse(resources["provider_quota_pools_verified"])
        self.assertFalse(resources["effective_capacity_verified"])
        self.assertEqual(result["local_admission"]["minimum_infrastructure_reserve_cents"], 3100)
        self.assertFalse(result["launch_ready"])
        self.assertTrue(result["preparation_checks_passed"])
        self.assertFalse(result["local_admission_reserve_sufficient"])
        self.assertFalse((self.root / ".swarm").exists())

    def test_duplicate_task_or_issue_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate task"):
            preflight.build_preflight(self.project, ["A1", "A1"], base=self.base)
        task = json.loads((self.project.tasks_dir / "A1.json").read_text())
        task.update(id="A2", ownership=["other/path"])
        (self.project.tasks_dir / "A2.json").write_text(json.dumps(task))
        result = preflight.build_preflight(self.project, ["A1", "A2"], base=self.base)
        self.assertFalse(result["local_workload_ready"])
        self.assertIn("duplicate_issue", result["local_admission"]["blocked_tasks"]["A1"])

    def test_model_reserve_is_optional_proposal_and_never_implicitly_selected(self):
        self.assertIsNone(self.check()["proposed_model_reserve_cents"])
        self.assertEqual(self.check(model_budget="1.20")["proposed_model_reserve_cents"], 120)
        for value in ("NaN", "Infinity", "0", "1.001", "180.01"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.check(model_budget=value)

    def test_long_window_and_configured_pacing_do_not_authorize_launch_or_prove_quota(self):
        self.project.path.write_text(self.project.path.read_text().replace(
            "[budget]", "[runtime]\ngateway_tokens_per_minute = 800000\ngateway_requests_per_minute = 4000\n[budget]"))
        self.project = load_project(self.project.path)
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        result = self.check(timeout=1800)
        self.assertEqual(result["runtime_constraints"]["agent_timeout_seconds"], 1800)
        self.assertEqual(result["runtime_constraints"]["gateway_run_ttl_seconds"], 2700)
        self.assertEqual(result["runtime_constraints"]["sandbox_lifecycle_timeout_seconds"], 2700)
        self.assertEqual(result["configured_provider_pacing"]["tokens_per_minute"], 800000)
        self.assertFalse(result["configured_provider_pacing"]["deployed_match_verified"])
        self.assertFalse(result["provider_quota_verified"])
        self.assertFalse(result["launch_ready"])
        self.assertFalse(result["authorizes_spend"])
        default = self.check()
        self.assertEqual(default["runtime_constraints"]["agent_timeout_seconds"], 300)
        self.assertFalse((self.root / ".swarm").exists())

    def test_cli_routes_without_run_or_deploy_and_reports_stage_success(self):
        output = io.StringIO()
        argv = ["swarm-control", "preflight", "A1", "--project", str(self.project.path), "--base", self.base]
        with patch("sys.argv", argv), patch("sys.stdout", output):
            self.assertEqual(cli.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["kind"], "cohort_preflight")
        self.assertTrue(result["preparation_checks_passed"])
        self.assertEqual(result["exit_status_scope"], "checked_source_preparation_only_not_budget_or_launch_readiness")
        self.assertFalse(result["launch_ready"])
        self.assertFalse(result["credentials"]["provider_authentication_verified"])
