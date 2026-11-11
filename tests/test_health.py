"""Failure feedback is task-scoped, privacy-preserving and never authorizes spend."""

import json
import math
import unittest
from datetime import UTC, datetime, timedelta

from swarm_control.health import (
    FailurePolicy,
    classify_worker_result,
    failure_decision,
    scaling_advice,
)

NOW = datetime(2026, 11, 11, 20, tzinfo=UTC)


def observation(
    task, cause="worker_incomplete", *, seconds=0, batch="batch", status="failure"
):
    return {
        "batch_id": batch,
        "task_id": task,
        "observed_at_utc": (NOW - timedelta(seconds=seconds)).isoformat(),
        "status": status,
        "cause": cause,
    }


class FailureTests(unittest.TestCase):
    def test_shared_cause_needs_three_tasks_and_half_the_observed_tasks(self):
        bad = [observation(str(i)) for i in range(3)]
        good = [
            observation(str(i), "validated_success", status="success")
            for i in range(3, 6)
        ]
        self.assertTrue(failure_decision(bad + good, FailurePolicy(), now=NOW)["hold"])
        self.assertFalse(
            failure_decision(
                bad
                + good
                + [observation("six", "validated_success", status="success")],
                FailurePolicy(),
                now=NOW,
            )["hold"]
        )
        self.assertFalse(failure_decision(bad[:2], FailurePolicy(), now=NOW)["hold"])

    def test_duplicates_and_cross_batch_repeats_cannot_fake_three_tasks(self):
        rows = [observation("same")] * 10
        self.assertEqual(
            failure_decision(rows, FailurePolicy(), now=NOW)["observations"], 1
        )
        rows = [observation("same", batch=str(i)) for i in range(4)]
        result = failure_decision(rows, FailurePolicy(), now=NOW)
        self.assertEqual(result["distinct_tasks"], 1)
        self.assertFalse(result["hold"])

    def test_newest_task_observation_replaces_old_failure(self):
        rows = [
            observation("a", seconds=20),
            observation("b"),
            observation("c"),
            observation("a", "validated_success", status="success"),
        ]
        self.assertFalse(failure_decision(rows, FailurePolicy(), now=NOW)["hold"])
        self.assertFalse(
            failure_decision(list(reversed(rows)), FailurePolicy(), now=NOW)["hold"]
        )

    def test_old_history_is_not_a_current_failure_signal(self):
        rows = [observation(str(i), seconds=301) for i in range(3)]
        self.assertFalse(failure_decision(rows, FailurePolicy(), now=NOW)["hold"])
        self.assertEqual(
            failure_decision([], FailurePolicy(), now=NOW)["reason"],
            "no_recent_observations",
        )

    def test_unknown_future_malformed_and_conflicting_data_hold_without_payloads(self):
        cases = [
            [observation("a", seconds=-1)],
            [observation("a", "unknown", status="unknown")],
            [{"secret": "PRIVATE_SENTINEL"}],
            [observation("a"), observation("a", "validation_failure")],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                result = failure_decision(rows, FailurePolicy(), now=NOW)
                self.assertTrue(result["hold"])
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))
                self.assertFalse(result["authorizes_spend"])
        with self.assertRaises(ValueError):
            failure_decision([], FailurePolicy(), now=NOW.replace(tzinfo=None))

    def test_policy_rejects_nonfinite_boolean_and_out_of_bounds_values(self):
        cases = [
            {"window_seconds": True},
            {"window_seconds": 0},
            {"window_seconds": 86401},
            {"minimum_failures": 0},
            {"minimum_tasks": 0},
            {"minimum_share": True},
            {"minimum_share": math.nan},
            {"minimum_share": math.inf},
            {"minimum_share": 0},
            {"minimum_share": 1.1},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                FailurePolicy(**kwargs)

    def test_fixed_classification_does_not_infer_timeout_or_copy_logs(self):
        cases = [
            ({"status": "validated"}, "success", "validated_success"),
            (
                {"status": "needs_repair", "recovery_errors": ["PRIVATE_SENTINEL"]},
                "failure",
                "recovery_failure",
            ),
            (
                {"status": "needs_repair", "violations": ["private/path"]},
                "failure",
                "ownership_violation",
            ),
            (
                {"status": "needs_repair", "prohibited_files": ["private/path"]},
                "failure",
                "ownership_violation",
            ),
            (
                {"status": "needs_repair", "check_exit": 1},
                "failure",
                "validation_failure",
            ),
            (
                {
                    "status": "needs_repair",
                    "agent_exit": -1,
                    "logs": "PRIVATE_SENTINEL",
                },
                "failure",
                "worker_incomplete",
            ),
            ({"status": "launch_failed"}, "failure", "launch_failure"),
            ({"status": "working"}, "unknown", "unknown"),
        ]
        for report, status, cause in cases:
            with self.subTest(report=report):
                result = classify_worker_result(report)
                self.assertEqual(result, {"status": status, "cause": cause})
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(result))


class ScalingTests(unittest.TestCase):
    def stats(self):
        return {
            "current_workers": 3,
            "merged_prs": 3,
            "agent_hours": 1.0,
            "previous_merges_per_agent_hour": 2.0,
            "previous_rate_measured": True,
            "comparison_matched": True,
            "queue_pressure": False,
            "failure_share": 0.0,
        }

    def test_growth_is_only_advice_and_never_crosses_maximum(self):
        result = scaling_advice(self.stats(), maximum_workers=4)
        self.assertFalse(result["hold"])
        self.assertEqual(result["target_workers"], 4)
        self.assertTrue(result["advisory_only"])
        self.assertFalse(result["authorizes_spend"])
        self.assertTrue(scaling_advice(self.stats(), maximum_workers=3)["hold"])

    def test_missing_unmatched_or_zero_hour_data_cannot_infer_efficiency(self):
        for key, value in [
            ("agent_hours", 0),
            ("agent_hours", True),
            ("agent_hours", math.nan),
            ("previous_rate_measured", False),
            ("comparison_matched", False),
            ("previous_merges_per_agent_hour", None),
            ("failure_share", None),
        ]:
            with self.subTest(key=key, value=value):
                stats = self.stats()
                stats[key] = value
                self.assertTrue(scaling_advice(stats, maximum_workers=10)["hold"])
        self.assertTrue(scaling_advice({}, maximum_workers=10)["hold"])

    def test_queue_failures_no_merges_and_lost_efficiency_hold_growth(self):
        for key, value in [
            ("queue_pressure", True),
            ("failure_share", 0.1),
            ("merged_prs", 0),
            ("previous_merges_per_agent_hour", 4),
        ]:
            with self.subTest(key=key, value=value):
                stats = self.stats()
                stats[key] = value
                result = scaling_advice(stats, maximum_workers=10)
                self.assertTrue(result["hold"])
                self.assertEqual(result["target_workers"], 3)

    def test_cap_and_nonfinite_agent_inputs_are_strict(self):
        for value in [True, 0, 101]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                scaling_advice({}, maximum_workers=value)
        stats = self.stats()
        stats["agent_hours"] = math.inf
        self.assertTrue(scaling_advice(stats, maximum_workers=10)["hold"])
