"""Offline scale comparisons expose saturation, validation holds and CI outcomes."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from swarm_control import cli
from swarm_control.rehearsal import PRESETS, parse_scenario, simulate
from tests.support import make_project


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.ids = [f"T{index:03}" for index in range(1, 101)]

    def test_provider_bottleneck_increases_queue_without_improving_delivery(self):
        scenario = parse_scenario(PRESETS["provider-constrained"], self.ids)
        small, large = (simulate(self.ids, scenario, workers) for workers in (25, 100))
        self.assertEqual(small["finished_seconds"], large["finished_seconds"])
        self.assertEqual(large["accepted"], 100)
        self.assertEqual(large["peak_active_workers"], 100)
        self.assertEqual(large["queues"]["model"]["peak_in_service"], 10)
        self.assertEqual(small["queues"]["model"]["p95_wait_seconds"], 140)
        self.assertEqual(large["queues"]["model"]["p95_wait_seconds"], 810)

    def test_worker_fault_holds_ramp_and_leaves_no_delivery_evidence(self):
        scenario = parse_scenario(PRESETS["worker-fault"], self.ids)
        fixed = simulate(self.ids, scenario, 100)
        ramp = simulate(self.ids, scenario, 100, ramp_start=5, trace=True)
        self.assertEqual(fixed["attempted"], 100)
        self.assertEqual(ramp["attempted"], 5)
        self.assertEqual(ramp["not_dispatched_task_ids"], self.ids[5:])
        self.assertEqual(ramp["hold_reason"], "rollout_validation_hold")
        self.assertEqual(ramp["accepted"], 0)
        self.assertIsNone(ramp["first_accepted_seconds"])
        self.assertIsNone(ramp["queues"]["review"]["p95_wait_seconds"])

    def test_inclusive_validation_threshold_and_independent_delivery_outcomes(self):
        ids = self.ids[:6]
        scenario = parse_scenario({"review_slots": 1, "ci_slots": 1,
                                   "review_release": "as_validated", "tasks": {
            ids[0]: {"worker_outcome": "needs_repair"},
            ids[1]: {"delivery_outcome": "review_rejected"},
            ids[2]: {"delivery_outcome": "ci_failed"},
            ids[5]: {"setup_seconds": 400},
        }}, ids)
        ramp = simulate(ids, scenario, 6, ramp_start=5, trace=True)
        self.assertEqual(ramp["validated"], 5)
        self.assertEqual(ramp["waves"][1]["started_at"], 130)
        self.assertEqual(ramp["accepted"], 3)
        self.assertEqual(ramp["review_rejected"], 1)
        self.assertEqual(ramp["ci_failed"], 1)
        self.assertLess(ramp["first_accepted_seconds"], ramp["workers_finished_seconds"])
        self.assertNotIn("ci_start", ramp["tasks"][1])
        self.assertEqual(ramp, simulate(ids, scenario, 6, ramp_start=5, trace=True))

    def test_rejects_unbounded_and_unknown_scenario_input(self):
        for config in ({"provider_slots": True}, {"tasks": {"OUTSIDE": {}}},
                       {"defaults": {"model_seconds": 0}},
                       {"defaults": {"review_seconds": 86401}},
                       {"defaults": {"delivery_outcome": ["accepted"]}}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                parse_scenario(config, self.ids)

    def test_cli_rehearses_real_frontier_read_only_and_rejects_blocked_cohort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = make_project(root / "target")
            second = project.tasks_dir / "A2.json"
            task = {"id": "A2", "ownership": ["lib/other.py"], "status": "ready"}
            second.write_text(json.dumps(task))
            manifest = root / "cohort.json"
            manifest.write_text(json.dumps({"taskIds": ["A1", "A2"]}))
            args = ["swarm-control", "rehearse", "--project", str(project.path),
                    "--manifest", str(manifest), "--base", "HEAD",
                    "--workers", "2", "--ramp-start", "1"]
            output = io.StringIO()
            with patch("sys.argv", args.copy()), redirect_stdout(output):
                self.assertEqual(cli.main(), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["safe_frontier_count"], 2)
            self.assertEqual(report["scenario"]["provider_slots"], project.gateway_concurrency)
            self.assertFalse(report["live_capacity_verified"])
            self.assertFalse(report["authorizes_spend"])
            self.assertNotIn(str(root), output.getvalue())
            self.assertFalse((project.root / ".swarm").exists())
            task["dependsOn"] = ["A1"]
            second.write_text(json.dumps(task))
            with patch("sys.argv", args.copy()), self.assertRaisesRegex(SystemExit, "Dependency preparation"):
                cli.main()
            self.assertFalse((project.root / ".swarm").exists())
