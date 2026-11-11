"""CI classifications use synthetic metadata and perform no service calls."""

import json
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from swarm_control.ci_health import RECOMMENDED_ACTIONS, classify_ci


class CIHealthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 2, 12, tzinfo=UTC)
        self.sha = "a" * 40
        self.check = {
            "id": 1, "name": "checks", "head_sha": self.sha,
            "app": {"slug": "github-actions"}, "status": "completed",
            "conclusion": "success", "completed_at": self.now.isoformat(),
        }
        self.run = {
            "id": 2, "head_sha": self.sha, "status": "completed",
            "conclusion": "success", "updated_at": self.now.isoformat(),
        }

    def classify(self, checks=None, runs=None, **kwargs):
        return classify_ci(
            self.sha, ["checks"],
            {"total_count": 1, "check_runs": [self.check]} if checks is None else checks,
            [self.run] if runs is None else runs,
            observed_at=kwargs.pop("observed_at", self.now),
            fetched_at=kwargs.pop("fetched_at", self.now), **kwargs,
        )

    def assert_closed(self, result, state=None, action=None):
        self.assertFalse(result["allowed_to_merge"])
        self.assertFalse(result["authorizes_mutation"])
        self.assertFalse(result["authorizes_spend"])
        self.assertIn(result["recommended_action"], RECOMMENDED_ACTIONS)
        if state:
            self.assertEqual(result["state"], state)
        if action:
            self.assertEqual(result["recommended_action"], action)

    def test_complete_fresh_trusted_exact_head_success(self):
        result = self.classify()
        self.assertEqual(result["state"], "green")
        self.assertTrue(result["allowed_to_merge"])
        self.assertEqual(result["recommended_action"], "none")
        self.assertFalse(result["authorizes_mutation"])
        json.dumps(result)

    def test_no_inputs_mutated(self):
        before = deepcopy((self.check, self.run))
        self.classify()
        self.assertEqual(before, (self.check, self.run))

    def test_missing_check_and_no_event_requires_inspection(self):
        self.assert_closed(self.classify({"total_count": 0, "check_runs": []}, []),
                           "missing_required_checks", "inspect_metadata")

    def test_queued_null_conclusion_zero_jobs_waits_without_kick(self):
        self.run.update(status="queued", conclusion=None, jobs=[])
        self.assert_closed(self.classify({"total_count": 0, "check_runs": []}),
                           "queued", "wait")

    def test_requested_check_waits(self):
        self.check.update(status="requested", conclusion=None)
        self.assert_closed(self.classify(), "queued", "wait")

    def test_pending_steps_success_is_not_authoritative_green(self):
        self.run.update(status="in_progress", conclusion=None, jobs=[{
            "status": "in_progress", "conclusion": None,
            "steps": [{"status": "completed", "conclusion": "success"}],
        }])
        self.assert_closed(self.classify(), "metadata_unresolved", "wait")

    def test_pending_workflow_without_jobs_waits(self):
        self.run.update(status="in_progress", conclusion=None)
        self.assert_closed(self.classify(), "in_progress", "wait")

    def test_completed_failure_explicit_zero_jobs_proposes_manual_rerun(self):
        self.run.update(conclusion="failure", jobs={"total_count": 0, "jobs": []})
        self.assert_closed(self.classify(), "ci_not_started", "propose_manual_rerun")

    def test_unknown_jobs_are_not_zero_jobs(self):
        self.run.update(conclusion="failure")
        self.assert_closed(self.classify(), "ci_failed", "review_or_repair")

    def test_failed_jobs_require_review(self):
        self.run.update(conclusion="failure", jobs=[{
            "status": "completed", "conclusion": "failure",
        }])
        self.assert_closed(self.classify(), "ci_failed", "review_or_repair")

    def test_cancelled_job_cannot_hide_under_green_aggregate(self):
        self.run["jobs"] = [{"status": "completed", "conclusion": "cancelled"}]
        self.assert_closed(self.classify(), "ci_failed")

    def test_failed_step_cannot_hide_under_green_job(self):
        self.run["jobs"] = [{"status": "completed", "conclusion": "success",
                             "steps": [{"status": "completed", "conclusion": "failure"}]}]
        self.assert_closed(self.classify(), "metadata_unresolved")

    def test_pending_job_cannot_hide_under_green_aggregate(self):
        self.run["jobs"] = [{"status": "in_progress", "conclusion": None}]
        self.assert_closed(self.classify(), "metadata_unresolved")

    def test_cancelled_check_is_not_green(self):
        self.check["conclusion"] = "cancelled"
        self.assert_closed(self.classify(), "cancelled", "inspect_metadata")

    def test_neutral_skipped_timedout_not_success(self):
        for value in ("neutral", "skipped", "timed_out", "action_required"):
            with self.subTest(value=value):
                self.check["conclusion"] = value
                self.assert_closed(self.classify(), "ci_failed")

    def test_incomplete_inventory_fails_closed(self):
        self.assert_closed(self.classify({"total_count": 2, "check_runs": [self.check]}),
                           "metadata_invalid")

    def test_duplicate_ids_fail_closed_even_when_all_green(self):
        self.assert_closed(self.classify({"total_count": 2,
                                         "check_runs": [self.check, deepcopy(self.check)]}),
                           "metadata_invalid")

    def test_duplicate_workflow_ids_fail_closed(self):
        self.assert_closed(self.classify(runs=[self.run, deepcopy(self.run)]),
                           "metadata_invalid")

    def test_foreign_check_or_workflow_head_fails_closed(self):
        for key in ("check", "run"):
            with self.subTest(key=key):
                record = getattr(self, key)
                record["head_sha"] = "b" * 40
                self.assert_closed(self.classify(), "head_mismatch")
                record["head_sha"] = self.sha

    def test_stale_and_future_source_fail_closed(self):
        for seconds, state in ((-601, "stale_metadata"), (1, "future_metadata")):
            with self.subTest(seconds=seconds):
                fetched = self.now + timedelta(seconds=seconds)
                self.assert_closed(self.classify(fetched_at=fetched), state)

    def test_old_successful_events_freshly_observed_remain_green(self):
        self.check["completed_at"] = (self.now - timedelta(days=7)).isoformat()
        self.run["updated_at"] = self.check["completed_at"]
        result = self.classify()
        self.assertEqual(result["state"], "green")
        self.assertEqual(result["fetched_at"], self.now.isoformat())

    def test_missing_or_naive_fetch_time_is_not_fresh_evidence(self):
        for fetched in (None, self.now.replace(tzinfo=None), "invalid"):
            with self.subTest(fetched=fetched):
                self.assert_closed(self.classify(fetched_at=fetched), "metadata_invalid")

    def test_event_cannot_postdate_actual_fetch_even_before_evaluation(self):
        self.assert_closed(self.classify(fetched_at=self.now - timedelta(seconds=1)),
                           "future_metadata")

    def test_long_running_workflow_freshly_observed_remains_pending(self):
        self.check.update(status="in_progress", conclusion=None, completed_at=None,
                          started_at=(self.now - timedelta(hours=2)).isoformat())
        self.run.update(status="in_progress", conclusion=None,
                        updated_at=(self.now - timedelta(hours=2)).isoformat())
        self.assert_closed(self.classify(), "in_progress", "wait")

    def test_naive_or_missing_source_time_fails_closed(self):
        for value in (None, "2026-01-02T12:00:00"):
            with self.subTest(value=value):
                self.check["completed_at"] = value
                self.assert_closed(self.classify(), "metadata_invalid")

    def test_naive_observation_fails_closed(self):
        self.assert_closed(self.classify(observed_at=self.now.replace(tzinfo=None)),
                           "metadata_invalid")

    def test_invalid_timestamp_other_than_selected_one_fails_closed(self):
        self.check["created_at"] = (self.now + timedelta(days=1)).isoformat()
        self.assert_closed(self.classify(), "metadata_invalid")

    def test_required_name_from_other_app_cannot_pass(self):
        self.check["app"] = {"slug": "untrusted"}
        self.assert_closed(self.classify(), "missing_required_checks")

    def test_unrelated_failed_check_does_not_allow_green(self):
        extra = {**self.check, "id": 3, "name": "another-check", "conclusion": "failure"}
        self.assert_closed(self.classify({"total_count": 2, "check_runs": [self.check, extra]}),
                           "ci_failed")

    def test_pending_conclusion_or_unknown_status_invalid(self):
        for status, conclusion in (("queued", "success"), ("unknown", None), ("completed", None)):
            with self.subTest(status=status, conclusion=conclusion):
                self.check.update(status=status, conclusion=conclusion)
                self.assert_closed(self.classify(), "metadata_invalid")

    def test_bad_limits_or_input_shapes_fail_closed(self):
        for value in (0, -1, float("nan"), float("inf"), True, "600"):
            with self.subTest(limit=value):
                self.assert_closed(self.classify(stale_after_seconds=value), "metadata_invalid")
        for value in ({}, {"total_count": True, "check_runs": [self.check]},
                      {"total_count": 1, "check_runs": [None]}):
            self.assert_closed(self.classify(checks=value), "metadata_invalid")

    def test_full_sha_and_nonempty_unique_requirements(self):
        for sha, names in (("a" * 7, ["checks"]), (self.sha, []),
                           (self.sha, ["checks", "checks"])):
            self.assert_closed(classify_ci(sha, names, {}, [], observed_at=self.now),
                               "metadata_invalid")

    def test_complete_workflow_inventory_and_zulu_times(self):
        self.check["completed_at"] = "2026-01-02T12:00:00Z"
        result = self.classify(runs={"total_count": 1, "workflow_runs": [self.run]})
        self.assertTrue(result["allowed_to_merge"])
        self.assert_closed(self.classify(runs={"total_count": 2, "workflow_runs": [self.run]}),
                           "metadata_invalid")


if __name__ == "__main__":
    unittest.main()
