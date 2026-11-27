"""Offline tests for checkpointed rollout scheduling and admission holds."""

import io
import json
import tempfile
import unittest
from concurrent.futures import wait
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from swarm_control import modal_runner as runner
from swarm_control.pilot_state import Ledger
from swarm_control.rollout import rollout_waves, wave_passes
from tests.support import make_project


def tasks(count):
    return [{"id": f"T{index:03}"} for index in range(count)]


class RolloutTests(unittest.TestCase):
    def test_rollout_wave_sizes_and_validation_boundary(self):
        self.assertEqual(rollout_waves(100, 100, 5), [5, 10, 20, 40, 25])
        self.assertEqual(rollout_waves(17, 4, 3), [3, 4, 4, 4, 2])
        self.assertTrue(wave_passes(4, 5))
        self.assertFalse(wave_passes(3, 5))
        self.assertFalse(wave_passes(0, 0))
        for limit, first in ((2, True), (2, 0), (2, 2), (3, 2)):
            with self.subTest(limit=limit, first=first), self.assertRaises(ValueError):
                rollout_waves(2, limit, first)

    def test_expansion_waits_for_every_previous_checkpoint(self):
        cohort = tasks(17)
        checkpointed = set()

        def execute(task):
            index = int(task["id"][1:])
            cutoff = 0 if index < 5 else 5 if index < 13 else 13
            self.assertTrue({item["id"] for item in cohort[:cutoff]} <= checkpointed)
            return {"status": "needs_repair" if index == 0 else "validated"}

        reports = runner.dispatch_tasks(cohort, 8, execute,
                                        lambda task, report: checkpointed.add(task["id"]), ramp_start=5)
        self.assertEqual(len(reports), 17)
        self.assertEqual({report["status"] for report in reports}, {"validated", "needs_repair"})
        self.assertEqual(checkpointed, {task["id"] for task in cohort})

    def test_hold_after_first_wave_releases_no_new_work(self):
        for hold_type, expected_reason in (
            ("validation", "rollout_validation_hold"),
            ("operations", "operations_hold"),
            ("gateway", "rollout_gateway_window_hold"),
        ):
            with self.subTest(hold_type=hold_type):
                self.assert_held_after_first_wave(hold_type, expected_reason)

    def assert_held_after_first_wave(self, hold_type, expected_reason):
        cohort = tasks(7)
        executed = []
        checkpointed = []

        def execute(task):
            executed.append(task["id"])
            return {"status": "needs_repair" if hold_type != "gateway" else "validated"}

        reports = runner.dispatch_tasks(
            cohort, 4, execute, lambda task, report: checkpointed.append(task["id"]), ramp_start=1,
            admit=lambda: hold_type != "operations" or not checkpointed,
            gateway_window_open=lambda: hold_type != "gateway" or not checkpointed,
        )
        self.assertEqual(executed, ["T000"])
        self.assertEqual(checkpointed, [task["id"] for task in cohort])
        self.assertEqual({report["reason"] for report in reports[1:]}, {expected_reason})
        self.assertTrue(all(report["status"] == "not_dispatched" for report in reports[1:]))

    def test_admission_hold_partway_through_wave_preserves_first_skipped_reason(self):
        cohort = tasks(11)
        opportunities = iter([True, True, False])
        reports = runner.dispatch_tasks(cohort, 8, lambda task: {"status": "validated"},
                                        lambda *_: None, ramp_start=4,
                                        gateway_window_open=lambda: next(opportunities))
        self.assertEqual(sum(report["status"] == "validated" for report in reports), 2)
        self.assertEqual({report["reason"] for report in reports[2:]}, {"rollout_gateway_window_hold"})

    def test_gateway_window_uses_elapsed_receipt_lifetime_and_rejects_bad_evidence(self):
        receipt = {"state": "active", "created_at": 123000, "expires_at": 125300}
        self.assertEqual(runner.rollout_gateway_deadline(receipt, 21, 3600), 2321)
        for invalid in (
            {"state": "stopped"}, {"created_at": None}, {"expires_at": 123000},
            {"expires_at": 126601}, {"expires_at": float("inf")},
        ):
            self.assertEqual(runner.rollout_gateway_deadline({**receipt, **invalid}, 21, 3600), 21)

    def test_fixed_live_queue_holds_when_gateway_window_is_closed_or_short(self):
        for expiry in ("before_launch", "before_refill"):
            with self.subTest(expiry=expiry):
                self._assert_fixed_live_queue_hold(expiry)

    def _assert_fixed_live_queue_hold(self, expiry):
        with tempfile.TemporaryDirectory() as temporary:
            project = make_project(Path(temporary))
            project.path.write_text(project.path.read_text().replace("live_enabled = false", "live_enabled = true"))
            template = json.loads((project.tasks_dir / "A1.json").read_text())
            cohort = tasks(3)
            for index, task in enumerate(cohort):
                (project.tasks_dir / f"{task['id']}.json").write_text(json.dumps({
                    **template, **task, "issue": index + 2, "ownership": [f"planned/{task['id']}.txt"],
                }))

            clock = 30
            expected_attempts = int(expiry == "before_refill")

            def issue_access(_batch, ids, _budget, ttl):
                nonlocal clock
                self.assertEqual(ttl, 2700)
                if expiry == "before_launch":
                    clock += ttl
                return {"state": "active", "created_at": 5000, "expires_at": 5000 + ttl,
                        "tokens": {task_id: "fake-token" for task_id in ids}}

            def execute(task, *_):
                nonlocal clock
                clock += 700
                return {"task_id": task["id"], "status": "validated", "sandbox_terminated": True}

            gateway = {"create_run": Mock(), "stop_run": Mock(), "run_status": Mock()}
            gateway["create_run"].remote.side_effect = issue_access
            gateway["stop_run"].remote.return_value = {"state": "stopped"}
            modal = Mock()
            modal.Function.from_name.side_effect = lambda _app, name, **_opts: gateway[name]
            with (
                patch.multiple(runner, ROOT=project.root, PROJECT=None, REPOSITORY=None, APP=None),
                patch.dict("sys.modules", {"modal": modal}),
                patch.object(runner, "time", SimpleNamespace(monotonic=lambda: clock)),
                patch.object(runner, "verify_github_tasks"),
                patch.object(runner, "execute_task", side_effect=execute) as execute_worker,
                patch("sys.argv", ["swarm-control", "run", *(task["id"] for task in cohort),
                                   "--project", str(project.path), "--base", "HEAD", "--workers", "1",
                                   "--model", "gpt-6.1-sol", "--model-budget", "1.00", "--timeout", "1800",
                                   "--gateway-url", "https://fake.modal.run"]),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(runner.main(), 1)
                self.assertEqual(execute_worker.call_count, expected_attempts)

            gateway["stop_run"].remote.assert_called_once()
            summary = json.loads(next((project.root / ".swarm").rglob("summary.json")).read_text())
            held_count = len(cohort) - expected_attempts
            self.assertEqual([report["status"] for report in summary["tasks"]],
                             ["validated"] * expected_attempts + ["not_dispatched"] * held_count)
            ledger = Ledger(project.root / ".swarm/control.sqlite3")
            rows = ledger.rows()
            self.assertEqual([row["state"] for row in rows],
                             ["awaiting_integration"] * expected_attempts + ["not_dispatched"] * held_count)
            for row in rows[expected_attempts:]:
                report = json.loads(Path(row["report"]).read_text())
                self.assertEqual(report["reason"], "gateway_window_hold")
            with ledger.connect() as db:
                self.assertEqual(db.execute("SELECT cents FROM pilot_allocations").fetchone()[0], 100)
                self.assertEqual(db.execute("SELECT cents FROM pilot_infra_allocations").fetchone()[0], 190)

    def test_fixed_dispatch_checkpoints_all_observed_finishes_before_refilling(self):
        executed, checkpointed = [], []

        def execute(task):
            executed.append(task["id"])
            return {"task_id": task["id"], "status": "needs_repair"}

        def checkpoint(task, report):
            if report["status"] == "needs_repair":
                checkpointed.append(task["id"])

        with patch.object(runner, "wait", side_effect=lambda pending, **_: wait(pending)):
            reports = runner.dispatch_tasks(tasks(4), 2, execute, checkpoint,
                                             admit=lambda: len(checkpointed) < 2)

        self.assertEqual(set(executed), {"T000", "T001"})
        self.assertEqual(checkpointed, ["T000", "T001"])
        self.assertEqual([report["reason"] for report in reports[2:]], ["operations_hold"] * 2)

    def test_missing_cleanup_evidence_for_an_attempt_does_not_count_as_terminated(self):
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary)
            for task_id, status in (("T000", "not_dispatched"), ("T001", "launch_failed")):
                directory = batch / task_id
                directory.mkdir()
                (directory / "report.json").write_text(json.dumps({"task_id": task_id, "status": status}))
            self.assertFalse(runner.batch_summary(batch)["all_sandboxes_terminated"])

    def test_cli_plan_reports_actual_maximum_wave_and_fixed_gateway_lifetime(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            project = make_project(Path(temporary))
            template = json.loads((project.tasks_dir / "A1.json").read_text())
            cohort = tasks(5)
            for index, task in enumerate(cohort):
                (project.tasks_dir / f"{task['id']}.json").write_text(json.dumps({
                    **template, **task, "issue": index + 2, "ownership": [f"planned/{task['id']}.txt"],
                }))
            with (
                patch.multiple(runner, ROOT=project.root, PROJECT=None, REPOSITORY=None, APP=None),
                patch("sys.argv", ["swarm-control", "plan", *(task["id"] for task in cohort),
                                   "--project", str(project.path), "--base", "HEAD",
                                   "--workers", "5", "--ramp-start", "1"]),
                redirect_stdout(output),
            ):
                self.assertEqual(runner.main(), 0)
            self.assertFalse((project.root / ".swarm").exists())
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["rollout"]["wave_sizes"], [1, 2, 2])
        self.assertEqual(plan["rollout"]["minimum_validated_percent"], 80)
        self.assertEqual(plan["maximum_active_agents"], 2)
        self.assertEqual(plan["gateway_run_ttl_seconds"], 3600)

    def test_ramp_is_rejected_before_project_access_for_incompatible_commands(self):
        for mode, flags in (("smoke", []), ("run", ["--synchronized"])):
            output = io.StringIO()
            with (
                self.subTest(mode=mode),
                patch("sys.argv", ["swarm-control", mode, "T01", "T02", "--project", "missing.toml",
                                   "--workers", "2", "--ramp-start", "1", *flags]),
                redirect_stderr(output),
                self.assertRaises(SystemExit) as error,
            ):
                runner.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("--ramp-start is available for unsynchronized plan and run only", output.getvalue())
