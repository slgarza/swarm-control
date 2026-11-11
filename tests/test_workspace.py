"""Repository isolation, immutable history and explicit execution boundaries."""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from swarm_control import modal_runner, workspace
from swarm_control.pilot_state import Ledger
from swarm_control.workspace import Workspace
from tests.support import make_project
from tests.test_dashboard import task


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = make_project(self.root / "target")
        (self.target.root / ".gitignore").write_text(".swarm/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "private runtime ignored")
        self.path = self.root / "private/workspace.json"
        self.workspace = Workspace(self.path)
        self.options = {"task_ids": ["A1"], "workers": 1, "model_budget": 0.50}

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.target.root, text=True).strip()

    def register(self):
        return self.workspace.add_project("target", self.target.path, label_text="Target")

    def live(self):
        self.target.path.write_text(self.target.path.read_text().replace("live_enabled = false", "live_enabled = true"))
        self.git("add", ".")
        self.git("commit", "-qm", "explicit fixture live policy")

    def source(self, repository="example/project", source_id="actual-original-run"):
        now = datetime.now(UTC)
        return {"repository": repository, "run_id": source_id,
                "as_of_utc": (now - timedelta(seconds=2)).isoformat(),
                "clock": {"T0_utc": (now - timedelta(hours=1)).isoformat(), "T1_utc": None,
                          "full_delivery_finished": False}, "rows": [task()]}

    def requested_run(self, *, two_tasks=False):
        if two_tasks:
            second = json.loads((self.target.tasks_dir / "A1.json").read_text())
            second["id"] = "A2"
            (self.target.tasks_dir / "A2.json").write_text(json.dumps(second))
            self.git("add", ".")
            self.git("commit", "-qm", "second observation fixture")
        self.register()
        options = {**self.options}
        if two_tasks:
            options.update(task_ids=["A1", "A2"], allow_overlap=True)
        saved = self.workspace.prepare_run("target", options, save=True)
        requested = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        with self.workspace._write() as value:
            stored = self.workspace._run(self.workspace._project(value, "target"), saved["id"])
            stored.update(status="launching", launch_requested_at_utc=requested,
                          attempt={"id": "synthetic", "worker_outcomes": {}, "exit_code": None})
        directory = self.target.root / ".swarm/modal-runs/batch-observed"
        directory.mkdir(parents=True)
        plan = {"run_id": saved["id"], "repository": self.target.repository,
                "base_sha": saved["base_sha"], "tasks": options["task_ids"]}
        (directory / "plan.json").write_text(json.dumps(plan))
        return saved, directory, plan

    @staticmethod
    def report(saved, task_id="A1", status="working"):
        now = datetime.now(UTC).isoformat()
        return {"task_id": task_id, "run_id": saved["id"], "base_sha": saved["base_sha"],
                "status": status, "agent_started_at": now,
                "progress": {"phases": [{"phase": "prepare", "started_at_utc": now},
                                         {"phase": "agent", "started_at_utc": now}],
                             "last_event_received_at_utc": now},
                "error": "private raw exception", "prompt": "customer code must stay private",
                "sandbox_id": "private-sandbox", "model_cost_usd": 12345}

    def test_reading_missing_workspace_creates_no_files(self):
        self.assertEqual(self.workspace.public_catalog()["projects"], [])
        self.assertEqual(self.workspace.historical_snapshot(), (None, "", None, None))
        self.assertFalse(self.path.parent.exists())

    def test_registration_projects_and_preview_are_private_read_only_projections(self):
        context = self.register()
        before = self.path.read_bytes()
        original_globals = {name: getattr(modal_runner, name) for name in ("ROOT", "REPOSITORY", "APP", "PROJECT")}
        with patch("swarm_control.pilot_state.Ledger", side_effect=AssertionError("ledger initialized")):
            result = self.workspace.prepare_run("target", self.options)
        self.assertEqual(result["status"], "preview")
        self.assertTrue(result["preflight"]["preparation_checks_passed"])
        self.assertFalse(result["launch_ready"])
        self.assertFalse(result["authorizes_spend"])
        self.assertIsNone(result["started_at_utc"])
        self.assertIsNone(result["launch_requested_at_utc"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.target.root / ".swarm").exists())
        self.assertEqual(context["limits"]["max_workers"], 10)
        self.assertEqual(context["tasks"][0]["id"], "A1")
        combined = json.dumps([context, result, self.workspace.public_catalog()])
        for forbidden in (str(self.root), '"project_path"', '"prompt"', '"install"', '"setup_commands"'):
            self.assertNotIn(forbidden, combined)
        for name, value in original_globals.items():
            self.assertIs(getattr(modal_runner, name), value)

    def test_agent_count_flags_caps_and_unknown_options_are_rejected(self):
        self.register()
        before = self.path.read_bytes()
        for change in ({"workers": 0}, {"workers": 2}, {"workers": True}, {"workers": 1.5},
                       {"workers": 101}, {"timeout": 29}, {"timeout": 1801},
                       {"task_ids": []}, {"task_ids": ["A1", "A1"]},
                       {"task_ids": ["../../credential"]}, {"execute": True},
                       {"base": "HEAD; command"}, {"base": "--help"},
                       {"model_budget": "NaN"}, {"model_budget": 0.001},
                       {"model_budget": "1e999999999"}, {"model_budget": "9" * 65},
                       {"model_budget": True}, {"model_budget": 180.01},
                       {"context_mode": []}, {"allow_overlap": "false"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.workspace.prepare_run("target", {**self.options, **change})
        with self.assertRaisesRegex(ValueError, "context_files"):
            self.workspace.prepare_run("target", {**self.options, "context_mode": "task"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.target.root / ".swarm/control.sqlite3").exists())

    def test_project_limit_is_not_increased_to_fit_requested_run(self):
        task = json.loads((self.target.tasks_dir / "A1.json").read_text())
        task["id"] = "A2"
        (self.target.tasks_dir / "A2.json").write_text(json.dumps(task))
        self.target.path.write_text(self.target.path.read_text() + "max_live_agents = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "one worker fixture cap")
        self.register()
        before = self.target.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.workspace.prepare_run("target", {**self.options, "task_ids": ["A1", "A2"], "workers": 2})
        self.assertEqual(self.target.path.read_bytes(), before)

    def test_draft_restart_history_and_default_launch_do_not_deploy(self):
        self.register()
        saved = self.workspace.prepare_run("target", self.options, save=True)
        restored = Workspace(self.path)
        with patch("subprocess.run", side_effect=AssertionError("unexpected child process")):
            result = restored.launch("target", saved["id"])
        self.assertFalse(result["execution_requested"])
        self.assertEqual(result["status"], "draft")
        self.assertEqual(restored.run_detail("target", saved["id"])["workers"], 1)
        self.assertIsNone(result["actual_billed_cost_usd"])
        self.assertIn("'<workspace.json>'", result["launch_command"])
        self.assertNotIn("--execute", result["launch_command"])
        self.assertNotIn("--gateway-url", result["launch_command"])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertFalse((self.target.root / ".swarm/control.sqlite3").exists())

    def test_source_drift_blocks_before_runner_or_local_admission(self):
        self.live()
        self.register()
        saved = self.workspace.prepare_run("target", self.options, save=True)
        (self.target.root / "src/item.txt").write_text("changed after freeze\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.workspace.launch("target", saved["id"], execute=True, gateway_url="https://fixture.modal.run")
        self.assertEqual(self.workspace.run_detail("target", saved["id"])["status"], "draft")
        self.assertFalse((self.target.root / ".swarm/control.sqlite3").exists())

    def test_disabled_live_policy_preserves_draft_without_launch_attempt(self):
        self.register()
        saved = self.workspace.prepare_run("target", self.options, save=True)
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.workspace.launch("target", saved["id"], execute=True, gateway_url="https://fixture.modal.run")
        result = self.workspace.run_detail("target", saved["id"])
        self.assertEqual(result["status"], "draft")
        self.assertIsNone(result["launch_requested_at_utc"])
        self.assertIsNone(result["started_at_utc"])

    def test_preview_preserves_existing_accounting_and_reports_only_retained_allowance(self):
        self.register()
        database = self.target.root / ".swarm/control.sqlite3"
        ledger = Ledger(database, model_limit_cents=1800, infra_limit_cents=700)
        ledger.allocate("historical", 100, infra_cents=50)

        def rows():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                return (db.execute("SELECT * FROM budget_policy").fetchall(),
                        db.execute("SELECT * FROM pilot_allocations").fetchall(),
                        db.execute("SELECT * FROM pilot_infra_allocations").fetchall())

        before = rows()
        result = self.workspace.prepare_run("target", self.options, save=True)
        self.assertEqual(result["preflight"]["remaining_model_allowance_cents"], 1700)
        self.assertEqual(result["preflight"]["remaining_infrastructure_allowance_cents"], 650)
        self.assertIsNone(result["actual_billed_cost_usd"])
        self.assertEqual(rows(), before)
        self.assertFalse((database.parent / "operations.sqlite3").exists())

    def test_real_runner_boundary_uses_exact_frozen_count_and_only_one_attempt(self):
        self.live()
        self.register()
        saved = self.workspace.prepare_run("target", self.options, save=True)
        real_run = subprocess.run
        launches = []

        def intercepted(argv, **kwargs):
            if argv[:3] == [sys.executable, "-m", "swarm_control"]:
                launches.append(argv)
                directory = self.target.root / ".swarm/modal-runs/batch-fixture"
                (directory / "A1").mkdir(parents=True)
                (directory / "plan.json").write_text(json.dumps({"run_id": saved["id"],
                    "repository": self.target.repository, "base_sha": saved["base_sha"], "tasks": ["A1"]}))
                (directory / "A1/report.json").write_text(json.dumps({"status": "needs_repair",
                    "agent_started_at": datetime.now(UTC).isoformat(), "error": "private raw worker body"}))
                return SimpleNamespace(returncode=1)
            return real_run(argv, **kwargs)

        with patch("swarm_control.workspace.subprocess.run", side_effect=intercepted):
            result = self.workspace.launch("target", saved["id"], execute=True,
                                            gateway_url="https://fixture.modal.run")
        self.assertEqual(len(launches), 1)
        self.assertEqual(launches[0][launches[0].index("--workers") + 1], "1")
        self.assertEqual(launches[0][launches[0].index("--run-id") + 1], saved["id"])
        self.assertEqual(result["status"], "runner_finished")
        self.assertEqual(result["runner_exit_code"], 1)
        self.assertEqual(result["worker_outcomes"], {"needs_repair": 1})
        self.assertIsNotNone(result["launch_requested_at_utc"])
        self.assertIsNotNone(result["started_at_utc"])
        self.assertNotIn("private raw worker body", json.dumps(result))
        self.assertFalse(result["has_snapshot"])
        with self.assertRaisesRegex(ValueError, "already been attempted"):
            self.workspace.launch("target", saved["id"], execute=True, gateway_url="https://fixture.modal.run")

    def test_runner_interruption_is_durable_and_does_not_invent_worker_start(self):
        self.live()
        self.register()
        saved = self.workspace.prepare_run("target", self.options, save=True)
        real_run = subprocess.run

        def intercepted(argv, **kwargs):
            if argv[:3] == [sys.executable, "-m", "swarm_control"]:
                raise KeyboardInterrupt()
            return real_run(argv, **kwargs)

        with patch("swarm_control.workspace.subprocess.run", side_effect=intercepted), self.assertRaises(KeyboardInterrupt):
            self.workspace.launch("target", saved["id"], execute=True, gateway_url="https://fixture.modal.run")
        restored = Workspace(self.path).run_detail("target", saved["id"])
        self.assertEqual(restored["status"], "interrupted")
        self.assertIsNotNone(restored["launch_requested_at_utc"])
        self.assertIsNone(restored["started_at_utc"])
        self.assertIsNone(restored["runner_exit_code"])

    def test_in_progress_report_projection_is_bound_safe_and_read_only(self):
        saved, directory, _ = self.requested_run(two_tasks=True)
        (directory / "A1").mkdir()
        report = self.report(saved)
        (directory / "A1/report.json").write_text(json.dumps(report))
        before = self.path.read_bytes()
        result = self.workspace.run_detail("target", saved["id"])
        execution = result["execution"]
        self.assertEqual(result["status"], "launching")
        self.assertEqual((execution["observed_tasks"], execution["expected_tasks"], execution["missing_tasks"]), (1, 2, 1))
        self.assertEqual(execution["worker_outcomes"], {"working": 1, "unknown": 1})
        self.assertEqual(execution["phase_counts"], {"agent": 1})
        self.assertEqual(execution["first_agent_started_at_utc"], report["agent_started_at"])
        self.assertEqual(result["started_at_utc"], report["agent_started_at"])
        self.assertFalse(execution["merged_delivery_verified"])
        self.assertIsNone(execution["actual_billed_cost_usd"])
        for forbidden in ("private raw exception", "customer code", "private-sandbox", "12345", str(self.root), "active_workers"):
            self.assertNotIn(forbidden, json.dumps(result))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.workspace._run(self.workspace._project(self.workspace._read(), "target"), saved["id"])["started_at_utc"])
        self.assertFalse((self.target.root / ".swarm/control.sqlite3").exists())

    def test_foreign_batch_is_not_projected_as_progress(self):
        saved, directory, plan = self.requested_run()
        for change in ({"repository": "example/foreign"}, {"run_id": "foreign"},
                       {"base_sha": "e" * 40}, {"tasks": ["OTHER"]}):
            with self.subTest(change=change):
                (directory / "plan.json").write_text(json.dumps({**plan, **change}))
                self.assertIsNone(self.workspace.run_detail("target", saved["id"])["execution"])
        for body in ("{broken", "[" * 2000 + "]" * 2000):
            (directory / "plan.json").write_text(body)
            self.assertIsNone(self.workspace.run_detail("target", saved["id"])["execution"])

    def test_unsafe_corrupt_or_foreign_worker_reports_stay_unknown(self):
        saved, directory, _ = self.requested_run()
        worker = directory / "A1"
        worker.mkdir()
        path = worker / "report.json"
        foreign = self.root / "private-report.json"
        foreign.write_text(json.dumps(self.report(saved)))
        invalid = ["{broken", "[" * 2000 + "]" * 2000, "x" * (workspace.MAX_TASK_BYTES + 1),
                   json.dumps({**self.report(saved), "run_id": "foreign"}),
                   json.dumps({**self.report(saved), "task_id": "A2"}),
                   json.dumps({**self.report(saved), "base_sha": "b" * 40}),
                   json.dumps({**self.report(saved), "status": "untrusted-status"})]
        for body in invalid:
            with self.subTest(length=len(body)):
                path.write_text(body)
                execution = self.workspace.run_detail("target", saved["id"])["execution"]
                self.assertEqual(execution["observed_tasks"], 0)
                self.assertEqual(execution["missing_tasks"], 1)
                self.assertEqual(execution["worker_outcomes"], {"unknown": 1})
                self.assertEqual(execution["phase_counts"], {})
        path.unlink()
        path.symlink_to(foreign)
        self.assertEqual(self.workspace.run_detail("target", saved["id"])["execution"]["observed_tasks"], 0)

    def test_run_detail_during_cli_execution_observes_callback_without_rewriting_registry(self):
        self.live()
        self.register()
        saved = self.workspace.prepare_run("target", self.options, save=True)
        real_run = subprocess.run
        observed = []

        def intercepted(argv, **kwargs):
            if argv[:3] == [sys.executable, "-m", "swarm_control"]:
                directory = self.target.root / ".swarm/modal-runs/batch-callback"
                (directory / "A1").mkdir(parents=True)
                (directory / "plan.json").write_text(json.dumps({"run_id": saved["id"],
                    "repository": self.target.repository, "base_sha": saved["base_sha"], "tasks": ["A1"]}))
                (directory / "A1/report.json").write_text(json.dumps(self.report(saved, status="checking")))
                before = self.path.read_bytes()
                observed.append(Workspace(self.path).run_detail("target", saved["id"]))
                self.assertEqual(self.path.read_bytes(), before)
                return SimpleNamespace(returncode=1)
            return real_run(argv, **kwargs)

        with patch("swarm_control.workspace.subprocess.run", side_effect=intercepted):
            self.workspace.launch("target", saved["id"], execute=True, gateway_url="https://fixture.modal.run")
        self.assertEqual(observed[0]["status"], "launching")
        self.assertEqual(observed[0]["execution"]["worker_outcomes"], {"checking": 1})
        self.assertIsNone(observed[0]["runner_exit_code"])
        self.assertFalse((self.target.root / ".swarm/control.sqlite3").exists())

    def test_multiple_saved_drafts_are_atomic_across_concurrent_clients(self):
        self.register()
        with ThreadPoolExecutor(max_workers=5) as pool:
            rows = list(pool.map(lambda _: Workspace(self.path).prepare_run("target", self.options, save=True), range(8)))
        self.assertEqual(len({row["id"] for row in rows}), 8)
        self.assertEqual(len(self.workspace.public_catalog()["projects"][0]["runs"]), 8)

    def test_parallel_repository_planning_restores_legacy_runner_globals(self):
        self.register()
        second = make_project(self.root / "second", repository="example/second")
        self.workspace.add_project("second", second.path)
        original = {name: getattr(modal_runner, name) for name in ("ROOT", "REPOSITORY", "APP", "PROJECT")}
        barrier = threading.Barrier(2)

        def inspect(project_id):
            barrier.wait(timeout=5)
            return self.workspace.prepare_run(project_id, self.options)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(inspect, ["target", "second"]))
        self.assertEqual([row["repository"] for row in results], ["example/project", "example/second"])
        for name, value in original.items():
            self.assertIs(getattr(modal_runner, name), value)

    def test_historical_import_is_an_immutable_copy_bound_to_repo_and_source_run(self):
        self.register()
        source = self.root / "curated.json"
        source.write_text(json.dumps(self.source()))
        original = source.read_bytes()
        imported = self.workspace.import_snapshot("target", source, run_id="pilot", label_text="Historical pilot")
        source.write_text(json.dumps(self.source(source_id="changed-original-source")))
        sealed, repo, project_id, run_id = self.workspace.historical_snapshot("target", "pilot")
        self.assertEqual((repo, project_id, run_id), ("example/project", "target", "pilot"))
        self.assertEqual(sealed.read_bytes(), original)
        self.assertEqual(imported["source_run_id"], "actual-original-run")
        self.assertEqual(imported["status"], "historical")
        self.assertIsNone(imported["execution"])
        self.assertTrue(imported["has_snapshot"])
        self.assertNotIn(str(sealed), json.dumps(imported))
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.workspace.import_snapshot("target", source, run_id="pilot")
        sealed.write_text("{}")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.workspace.historical_snapshot("target", "pilot")

    def test_history_only_repository_is_selectable_but_cannot_prepare_a_run(self):
        result = self.workspace.add_project("history", repository="example/history", label_text="History")
        self.assertFalse(result["configurable"])
        self.assertEqual(result["tasks"], [])
        with self.assertRaisesRegex(ValueError, "historical evidence only"):
            self.workspace.prepare_run("history", self.options)
        self.assertEqual(self.workspace.historical_snapshot("history"), (None, "example/history", "history", None))

    def test_mismatched_snapshot_and_unknown_project_cannot_access_another_source(self):
        self.register()
        source = self.root / "foreign.json"
        source.write_text(json.dumps(self.source(repository="example/foreign")))
        with self.assertRaisesRegex(ValueError, "different repository"):
            self.workspace.import_snapshot("target", source)
        for project_id, run_id in (("unknown", None), ("../../credential", None), ("target", "../../credential")):
            with self.subTest(project_id=project_id), self.assertRaises(ValueError):
                self.workspace.historical_snapshot(project_id, run_id)

    def test_duplicate_registry_keys_and_symlinks_fail_closed(self):
        self.path.parent.mkdir()
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError):
            self.workspace.public_catalog()
        self.path.unlink()
        foreign = self.root / "foreign.json"
        foreign.write_text("{}")
        self.path.symlink_to(foreign)
        with self.assertRaises(ValueError):
            self.workspace.public_catalog()
        self.assertEqual(foreign.read_text(), "{}")

    def test_cli_commands_prepare_runs_without_execution_and_accept_agents_alias(self):
        with patch.object(sys, "argv", ["swarm-control", "init", "--workspace", str(self.path)]), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(workspace.workspace_main(), 0)
        with patch.object(sys, "argv", ["swarm-control", "add", str(self.target.path), "--id", "target",
                                       "--workspace", str(self.path)]), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(workspace.workspace_main(), 0)
        with patch.object(sys, "argv", ["swarm-control", "create", "target", "A1", "--agents", "1",
                                       "--workspace", str(self.path)]), patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(workspace.runs_main(), 0)
            self.assertEqual(json.loads(out.getvalue())["workers"], 1)
        self.assertFalse((self.target.root / ".swarm/control.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
