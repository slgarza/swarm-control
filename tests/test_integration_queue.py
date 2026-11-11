"""Independent candidates advance around unresolved CI without bypassing gates."""

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from swarm_control.integration_queue import plan_queue


class IntegrationQueueTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 11, 20, tzinfo=UTC)
        self.source = {"base_sha": "a" * 40, "lanes": 2, "ci_wait_seconds": 600, "items": [
            {"task_id": "T01", "stage": "waiting_ci", "ownership": ["src/blocked"],
             "waiting_since_utc": (self.now - timedelta(minutes=20)).isoformat()},
            {"task_id": "T02", "stage": "ready", "ownership": ["src/two"]},
            {"task_id": "T03", "stage": "ready", "ownership": ["src/three"]},
        ]}

    def test_pending_head_does_not_stall_independent_candidates(self):
        original = deepcopy(self.source)
        result = plan_queue(self.source, now=self.now)
        self.assertEqual(result["prepare"], ["T02", "T03"])
        self.assertEqual(result["decisions"][0]["action"], "quarantine")
        self.assertEqual(self.source, original)
        self.assertFalse(result["authorizes_mutation"])
        self.assertFalse(result["authorizes_spend"])
        self.assertFalse(result["automatic_ci_retry"])
        self.assertTrue(result["revalidate_changed_base"])
        self.assertTrue(all(row["required_base_sha"] == self.source["base_sha"]
                            for row in result["decisions"] if row["action"] == "prepare"))

    def test_dependencies_and_shared_file_work_stay_held(self):
        self.source["items"][1]["depends_on"] = ["T01"]
        self.source["items"][2]["ownership"] = ["src/blocked/consumer.py"]
        result = plan_queue(self.source, now=self.now)
        self.assertEqual(result["prepare"], [])
        self.assertEqual(result["decisions"][1]["reason"], "dependency_not_qualified_merged")
        self.assertEqual(result["decisions"][2]["reason"], "ownership_in_flight")

    def test_declared_merge_without_acceptance_is_not_a_dependency_release(self):
        self.source["items"][0]["stage"] = "merged"
        self.source["items"][1]["depends_on"] = ["T01"]
        self.assertNotIn("T02", plan_queue(self.source, now=self.now)["prepare"])
        self.source["items"][0].update(merge_commit_sha="b" * 40, independent_acceptance=True,
                                         combined_ci_green=True)
        self.assertIn("T02", plan_queue(self.source, now=self.now)["prepare"])

    def test_already_preparing_uses_a_lane_and_overlapping_new_work_is_held(self):
        self.source["items"][0]["stage"] = "preparing"
        self.source["items"].append({"task_id": "T04", "stage": "ready", "ownership": ["src/two/child"]})
        result = plan_queue(self.source, now=self.now)
        self.assertEqual(result["prepare"], ["T02"])
        self.assertEqual(result["decisions"][2]["reason"], "preparation_lanes_full")
        self.assertEqual(result["decisions"][3]["reason"], "ownership_in_flight")

    def test_unknown_or_future_wait_does_not_authorize_rerun(self):
        for value in (None, "invalid", "2026-11-11T20:00:00", (self.now + timedelta(seconds=1)).isoformat()):
            with self.subTest(value=value):
                self.source["items"][0]["waiting_since_utc"] = value
                result = plan_queue(self.source, now=self.now)
                self.assertEqual(result["decisions"][0]["action"], "hold")
                self.assertEqual(result["prepare"], ["T02", "T03"])

    def test_cycle_duplicate_unknown_stage_and_missing_dependencies_fail_closed(self):
        mutations = [
            lambda x: x["items"].append(deepcopy(x["items"][0])),
            lambda x: x["items"][1].update(depends_on=["MISSING"]),
            lambda x: x["items"][1].update(stage="green-ish"),
            lambda x: x["items"][1].update(ownership=["../private"]),
            lambda x: x["items"][1].update(depends_on=["T03"]),
        ]
        for index, mutate in enumerate(mutations):
            source = deepcopy(self.source)
            mutate(source)
            if index == 4:
                source["items"][2]["depends_on"] = ["T02"]
            with self.subTest(index=index), self.assertRaises(ValueError):
                plan_queue(source, now=self.now)

    def test_long_acyclic_queue_uses_no_python_recursion(self):
        source = {"base_sha": "a" * 40, "items": [
            {"task_id": f"T{i:03}", "stage": "ready", "ownership": [f"src/{i}"],
             "depends_on": [f"T{i-1:03}"] if i else []} for i in range(1000)]}
        self.assertEqual(plan_queue(source, now=self.now)["prepare"], ["T000"])


if __name__ == "__main__":
    unittest.main()
