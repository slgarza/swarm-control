"""Offline faults exercise admission and restart-safe remote-action boundaries."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from swarm_control import modal_runner, pilot_integrate
from swarm_control.health import FailurePolicy, classify_worker_result
from swarm_control.operations import OperationsJournal
from tests.support import make_project


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "state" / "operations.sqlite3"
        self.now = datetime.now(UTC)
        self.journal = OperationsJournal(self.path)
        self.policy = FailurePolicy()

    def fail(self, task):
        return self.journal.observe("batch", task, {"status": "needs_repair", "agent_exit": -1}, now=self.now)

    def test_status_does_not_create_state_or_money(self):
        result = self.journal.status()
        self.assertFalse(result["admission_held"])
        self.assertFalse(result["authorizes_spend"])
        self.assertFalse(self.path.parent.exists())

    def test_brake_survives_restart_and_window_and_clear_preserves_evidence_and_money(self):
        self.path.parent.mkdir()
        budget = self.path.parent / "control.sqlite3"
        budget.write_bytes(b"untouched budget allocations")
        self.assertFalse(self.fail("T1")["admission_held"])
        self.assertFalse(self.fail("T2")["admission_held"])
        self.assertTrue(self.fail("T3")["admission_held"])
        restarted = OperationsJournal(self.path)
        later = self.now + timedelta(hours=1)
        self.assertTrue(restarted.status(now=later)["persistent_hold"])
        with self.assertRaisesRegex(ValueError, "held"):
            restarted.require_admission()
        restarted.clear("Reviewed and fixed the worker contract", now=later)
        self.assertFalse(restarted.status(now=later)["admission_held"])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM controls").fetchone()[0], 2)
        self.assertEqual(budget.read_bytes(), b"untouched budget allocations")

    def test_replayed_checkpoint_cannot_inflate_failures_or_rewrite_original(self):
        for _ in range(5):
            result = self.fail("T1")
        self.assertFalse(result["admission_held"])
        self.assertEqual(result["observed_outcomes"], 1)
        with self.assertRaisesRegex(ValueError, "rewritten"):
            self.journal.observe("batch", "T1", {"status": "validated"}, now=self.now)

    def test_nested_scope_violation_and_contradictory_validated_report_fail(self):
        self.assertEqual(classify_worker_result({"status": "needs_repair", "changes": {
            "ownership_violations": ["outside.py"]}})["cause"], "ownership_violation")
        self.assertEqual(classify_worker_result({"status": "validated", "recovery_errors": ["error"]})["status"], "failure")

    def test_journal_rejects_budget_aliases(self):
        self.path.parent.mkdir()
        budget = self.path.parent / "control.sqlite3"
        budget.write_bytes(b"budget")
        for kind in ("direct", "symlink", "hardlink"):
            path = self.path.parent / kind
            if kind == "direct":
                path = budget
            elif kind == "symlink":
                path.symlink_to(budget)
            else:
                path.hardlink_to(budget)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                OperationsJournal(path).status()
        self.assertEqual(budget.read_bytes(), b"budget")

    def test_action_is_reserved_once_across_concurrent_clients_and_restart(self):
        def reserve(_):
            try:
                return OperationsJournal(self.path).reserve_action("merge_pr", "1:" + "a" * 40, now=self.now)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = [token for token in pool.map(reserve, range(8)) if token]
        self.assertEqual(len(tokens), 1)
        self.journal.finish_action(tokens[0], "uncertain")
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            OperationsJournal(self.path).reserve_action("merge_pr", "1:" + "a" * 40,
                now=self.now + timedelta(days=1), maximum_attempts=10, cooldown_seconds=0)
        self.journal.finish_action(tokens[0], "succeeded")
        with self.assertRaisesRegex(ValueError, "cap"):
            self.journal.reserve_action("merge_pr", "1:" + "a" * 40)

    def test_known_failed_actions_still_obey_cooldown_and_cap(self):
        token = self.journal.reserve_action("rerun_ci", "head", now=self.now, maximum_attempts=2)
        self.journal.finish_action(token, "failed")
        with self.assertRaisesRegex(ValueError, "cooldown"):
            self.journal.reserve_action("rerun_ci", "head", now=self.now, maximum_attempts=2)
        second = self.journal.reserve_action("rerun_ci", "head", now=self.now + timedelta(seconds=601), maximum_attempts=2)
        self.journal.finish_action(second, "failed")
        with self.assertRaisesRegex(ValueError, "cap"):
            self.journal.reserve_action("rerun_ci", "head", maximum_attempts=2)

    def test_dispatch_brakes_new_work_but_drains_and_checkpoints_admitted_workers(self):
        tasks = [{"id": f"T{i}"} for i in range(12)]
        executed, completed = [], []

        def execute(task):
            executed.append(task["id"])
            return {"status": "needs_repair"}

        def checkpoint(task, report):
            completed.append(task["id"])
            if report["status"] != "not_dispatched":
                self.journal.observe("batch", task["id"], report)

        results = modal_runner.dispatch_tasks(tasks, 2, execute, checkpoint,
            admit=lambda: not self.journal.status()["admission_held"])
        self.assertLess(len(executed), len(tasks))
        self.assertEqual(set(completed), {task["id"] for task in tasks})
        self.assertEqual(sum(r["status"] == "not_dispatched" for r in results), len(tasks) - len(executed))
        self.assertEqual(self.journal.status()["observed_outcomes"], len(executed))

    def test_hold_blocks_live_runner_before_provider_lookup_or_budget_initialization(self):
        project = make_project(self.root / "held-target")
        project.path.write_text(project.path.read_text().replace("live_enabled = false", "live_enabled = true"))
        journal = OperationsJournal(project.root / ".swarm/operations.sqlite3")
        for task_id in ("T1", "T2", "T3"):
            journal.observe("batch", task_id, {"status": "needs_repair"})
        original = {name: getattr(modal_runner, name) for name in ("ROOT", "REPOSITORY", "APP", "PROJECT")}
        with patch.multiple(modal_runner, **original), \
             patch.object(sys, "argv", ["swarm-control", "smoke", "A1", "--project", str(project.path), "--base", "HEAD"]), \
             patch("modal.App.lookup") as lookup:
            with self.assertRaisesRegex(ValueError, "held"):
                modal_runner.main()
            lookup.assert_not_called()
        self.assertFalse((project.root / ".swarm/control.sqlite3").exists())

    def test_project_policy_defaults_and_optional_override_do_not_change_allowance(self):
        project = make_project(self.root / "repo")
        self.assertEqual(project.failure_policy, FailurePolicy())
        with project.path.open("a") as stream:
            stream.write("\n[operations]\nminimum_tasks=2\nminimum_failures=2\n")
        from swarm_control.project import load_project
        loaded = load_project(project.path)
        self.assertEqual(loaded.failure_policy.minimum_tasks, 2)
        self.assertEqual((loaded.model_limit_cents, loaded.infra_limit_cents), (1800, 700))
        self.assertFalse(loaded.live_enabled)


class MergeFaultTests(unittest.TestCase):
    def test_uncertain_put_is_not_repeated_and_actual_merge_can_reconcile_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = make_project(root / "repo")
            directory = root / "artifact"
            directory.mkdir()
            base = pilot_integrate.git(project.root, "rev-parse", "HEAD")
            task = json.loads((project.tasks_dir / "A1.json").read_text())
            source = project.root / "src/item.txt"
            source.write_text("Useful reviewed change\n")
            patch_bytes = subprocess.check_output(["git", "diff", "--binary", "--full-index"], cwd=project.root)
            source.write_text("baseline\n")
            digest = hashlib.sha256(patch_bytes).hexdigest()
            report = {"task_id": task["id"], "base_sha": base, "status": "validated",
                      "agent_exit": 0, "check_exit": 0, "patch_sha256": digest,
                      "checked_patch_sha256": digest, "changes": {"head_unchanged": True,
                      "ownership_violations": [], "prohibited_files": []}}
            (directory / "changes.patch").write_bytes(patch_bytes)
            (directory / "manifest.json").write_text(json.dumps({"repository": project.clone_url,
                "base_sha": base, "task": task}))
            (directory / "report.json").write_text(json.dumps(report))
            state = {"status": "published", "pr_number": 1, "head_sha": "a" * 40,
                     "branch": "swarm/task", "tested_base_sha": base, "patch_sha256": digest,
                     "baseline_checks_exit": 0, "checks_finished_at": datetime.now(UTC).isoformat(),
                     "acceptance_configured": False}
            merged, writes = False, []

            def github(path, *, method="GET", body=None):
                if method == "PUT":
                    writes.append(body)
                    raise TimeoutError("uncertain response")
                if path == "git/ref/heads/main":
                    return {"object": {"sha": base}}
                return {"state": "closed" if merged else "open", "merged": merged, "draft": False,
                        "base": {"ref": "main"}, "head": {"sha": "a" * 40, "ref": "swarm/task"},
                        "merged_at": datetime.now(UTC).isoformat() if merged else None,
                        "merge_commit_sha": "c" * 40 if merged else None}

            checks = {"total_count": 1, "check_runs": [{"app": {"slug": "github-actions"},
                "name": "checks", "status": "completed", "conclusion": "success", "head_sha": "a" * 40}]}
            with patch.multiple(pilot_integrate, ROOT=project.root, PROJECT=project,
                                REPO=project.repository, BASE_BRANCH="main"), \
                 patch.object(pilot_integrate, "candidate"), patch.object(pilot_integrate, "github", side_effect=github), \
                 patch.object(pilot_integrate, "all_checks", return_value=checks):
                state["source_bindings"] = pilot_integrate.source_bindings(directory, task, report, base)
                with self.assertRaises(TimeoutError):
                    pilot_integrate.merge(directory, task, state)
                with self.assertRaisesRegex(ValueError, "reconciliation"):
                    pilot_integrate.merge(directory, task, state)
                self.assertEqual(len(writes), 1)
                merged = True
                result = pilot_integrate.merge(directory, task, state)
                self.assertEqual(result["merge_commit_sha"], "c" * 40)
                self.assertEqual(len(writes), 1)
                self.assertEqual(json.loads((directory / "integration.json").read_text())["status"], "merged")
