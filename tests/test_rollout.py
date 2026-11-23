"""Offline tests for checkpointed rollout scheduling and admission holds."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from swarm_control import modal_runner as runner
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
