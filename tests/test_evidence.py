"""Synthetic offline evidence tests; these fixtures are not live agent runs."""

import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from swarm_control.evidence import (
    collect,
    compare,
    cost_summary,
    digest,
    overlap,
    timestamp,
)

BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40
START = "2026-11-10T20:00:00+00:00"
END = "2026-11-10T20:01:00+00:00"
PROTOCOL = {"comparison_id": "paired-1", "baseline_configuration_sha256": "d" * 64,
            "acceptance_policy_sha256": "e" * 64, "cost_policy_sha256": "f" * 64,
            "clock_definition": "workload release through final accepted delivery", "observation_window_seconds": 60}
COSTS = {"reconciliation_status": "reconciled", "gross_total": 5,
         "confirmed_internal_token_exemption": 1, "contest_eligible_total": 4,
         "exemption_evidence_sha256": "2" * 64}


def save(path, data):
    path.write_text(json.dumps(data))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.batch = Path(self.temporary.name) / "batch-offline"
        self.batch.mkdir()

    def fixture(self, count=1, *, active=True, merged=False):
        task_ids = [f"T{index:03}" for index in range(count)]
        save(self.batch / "plan.json", {"repository": "example/project", "base_sha": BASE, "tasks": task_ids})
        for index, task_id in enumerate(task_ids):
            directory = self.batch / task_id
            directory.mkdir()
            task = {"id": task_id, "title": "private title", "prompt": "private source context",
                    "acceptance": ["private acceptance"], "ownership": ["src/shared.py"]}
            save(directory / "manifest.json", {"repository": "https://github.com/example/project.git",
                                               "base_sha": BASE, "task": task})
            patch = f"synthetic offline patch {task_id}".encode()
            (directory / "changes.patch").write_bytes(patch)
            patch_hash = digest(patch)
            save(directory / "report.json", {"task_id": task_id, "base_sha": BASE, "status": "validated",
                                             "sandbox_id": f"synthetic-session-{index}",
                                             "agent_started_at": START, "agent_finished_at": END,
                                             "started_at": START, "finished_at": END,
                                             "checked_patch_sha256": patch_hash, "patch_sha256": patch_hash,
                                             "agent_exit": 0, "check_exit": 0, "recovery_errors": [],
                                             "changes": {"head_unchanged": True, "ownership_violations": [], "prohibited_files": []},
                                             "error": "private secret never export"})
            if active:
                events = [{"type": "item.completed", "item": {"type": "command_execution", "command": "private source"},
                           "swarm_received_at": "2026-11-10T20:00:01+00:00"},
                          {"type": "turn.completed", "usage": {"input_tokens": 123},
                           "swarm_received_at": "2026-11-10T20:00:09+00:00"}]
                (directory / "agent.jsonl").write_text("\n".join(json.dumps(event) for event in events))
            if merged:
                save(directory / "integration.json", {
                    "task_id": task_id, "batch_id": self.batch.name, "status": "merged",
                    "patch_sha256": patch_hash, "head_sha": HEAD, "green_head_sha": HEAD,
                    "checks_started_at": "2026-11-10T20:00:50+00:00",
                    "checks_finished_at": "2026-11-10T20:00:54+00:00",
                    "merge_commit_sha": MERGE, "merged_at": "2026-11-10T20:00:55+00:00"})
        return task_ids

    def receipt(self, task_id="T000"):
        return {"task_id": task_id, "patch_sha256": digest((self.batch / task_id / "changes.patch").read_bytes()),
                "reviewed_head_sha": HEAD, "merge_commit_sha": MERGE, "accepted": True,
                "post_merge_checks": "success", "reverted": False, "evidence_sha256": "1" * 64,
                "observation_ended_at": "2026-11-10T20:02:00+00:00"}

    def measured(self, **kwargs):
        return collect(self.batch, started_at=START, ended_at=END, protocol=PROTOCOL, costs=COSTS, **kwargs)

    def test_100_active_synthetic_sessions_have_bounded_sampled_overlap(self):
        self.fixture(100)
        result = self.measured()
        activity = result["concurrency"]
        self.assertEqual(activity["observed_peak_sessions"], 100)
        self.assertEqual(activity["session_overlap_at_or_above_100_seconds"], 60)
        self.assertEqual(activity["sampled_contribution_peak"], 100)
        self.assertEqual(activity["sampled_contribution_overlap_at_or_above_100_seconds"], 8)
        self.assertFalse(activity["scale_requirement_verified"])
        self.assertIsNone(result["work"]["merged_green_prs"])
        self.assertIsNone(result["rates"]["merged_green_prs_per_hour"])

    def test_100_idle_containers_do_not_count_as_sampled_contributions(self):
        self.fixture(100, active=False)
        result = self.measured()
        self.assertEqual(result["concurrency"]["observed_peak_sessions"], 100)
        self.assertEqual(result["concurrency"]["sampled_contribution_peak"], 0)

    def test_duplicate_identities_exclude_all_occurrences(self):
        self.fixture(2)
        report = self.batch / "T001/report.json"
        value = json.loads(report.read_text())
        value["sandbox_id"] = "synthetic-session-0"
        save(report, value)
        self.assertEqual(self.measured()["concurrency"]["observed_peak_sessions"], 0)

    def test_missing_worker_or_wrong_base_leaves_manifest_unknown(self):
        self.fixture(2)
        (self.batch / "T001/report.json").unlink()
        result = self.measured()
        self.assertIsNone(result["task_manifest_sha256"])
        self.assertIsNone(result["work"]["merged_green_prs"])
        self.assertEqual(len(result["concurrency"]["evidence_gaps"]), 1)

    def test_export_contains_hashes_and_never_raw_worker_text(self):
        self.fixture()
        exported = json.dumps(self.measured())
        for private in ("private title", "private source", "private acceptance", "private secret", "synthetic-session"):
            self.assertNotIn(private, exported)
        self.assertIn("source_artifacts", exported)

    def test_merge_receipt_alone_is_not_accepted_delivery(self):
        self.fixture(merged=True)
        result = self.measured()
        self.assertEqual(result["work"]["observed_merged_prs"], 1)
        self.assertIsNone(result["work"]["merged_green_prs"])
        missing = self.measured(acceptance=[])
        self.assertIsNone(missing["work"]["merged_green_prs"])

    def test_delivery_requires_independent_acceptance_and_post_merge_quality(self):
        self.fixture(merged=True)
        result = self.measured(acceptance=[self.receipt()])
        self.assertEqual(result["work"]["merged_green_prs"], 1)
        self.assertEqual(result["rates"]["merged_green_prs_per_hour"], 60)
        self.assertEqual(result["rates"]["merged_green_prs_per_eligible_dollar"], 0.25)
        for key, value in (("accepted", False), ("reverted", True), ("post_merge_checks", "failure"),
                           ("reviewed_head_sha", "0" * 40), ("merge_commit_sha", "0" * 40),
                           ("observation_ended_at", "2026-11-10T20:01:00+00:00")):
            with self.subTest(key=key):
                receipt = self.receipt()
                receipt[key] = value
                self.assertEqual(self.measured(acceptance=[receipt])["work"]["merged_green_prs"], 0)

    def test_tampered_patch_or_stale_head_never_counts(self):
        self.fixture(merged=True)
        integration = self.batch / "T000/integration.json"
        state = json.loads(integration.read_text())
        state["green_head_sha"] = "0" * 40
        save(integration, state)
        self.assertEqual(self.measured(acceptance=[self.receipt()])["work"]["merged_green_prs"], 0)
        (self.batch / "T000/changes.patch").write_text("tampered")
        self.assertEqual(self.measured()["concurrency"]["sampled_contribution_peak"], 0)

    def test_invalid_clock_event_and_duplicate_acceptance_fail_safely(self):
        self.fixture()
        for start, end in ((START, None), (START, START), ("2026-11-10T20:00:00", END)):
            with self.assertRaises(ValueError):
                collect(self.batch, started_at=start, ended_at=end)
        with self.assertRaises(ValueError):
            self.measured(acceptance=[self.receipt(), self.receipt()])
        (self.batch / "T000/agent.jsonl").write_text('null\n{"type":"item.completed"}\n')
        self.assertEqual(self.measured()["concurrency"]["sampled_contribution_peak"], 0)

    def test_mixed_timezone_offsets_and_boundary_ties(self):
        start = timestamp(START)
        same = timestamp("2026-11-10T14:00:00-06:00")
        self.assertEqual(start, same)
        intervals = [(start, start + timedelta(seconds=1)), (same + timedelta(seconds=1), same + timedelta(seconds=2))]
        self.assertEqual(overlap(intervals)["peak"], 1)

    def test_comparison_requires_frozen_protocol_and_both_positive_rates(self):
        self.fixture(merged=True)
        swarm = self.measured(acceptance=[self.receipt()])
        afk = copy.deepcopy(swarm)
        afk.update(pipeline="afk", run_id="baseline-offline")
        afk["clock"]["elapsed_seconds"] = 120
        afk["clock"]["ended_at_utc"] = "2026-11-10T20:02:00+00:00"
        afk["cost_usd"]["gross_total"] = 9
        afk["cost_usd"]["contest_eligible_total"] = 8
        result = compare(swarm, afk)
        self.assertTrue(result["comparable"])
        self.assertTrue(result["efficiency_win"])
        self.assertFalse(result["contest_win_proven"])
        self.assertEqual(set(result["rate_ratios"].values()), {2.0})
        afk["task_manifest_sha256"] = "0" * 64
        self.assertIsNone(compare(swarm, afk)["efficiency_win"])

    def test_unknown_or_zero_cost_denominators_never_imply_infinite_efficiency(self):
        self.fixture(merged=True)
        swarm = self.measured(acceptance=[self.receipt()])
        afk = copy.deepcopy(swarm)
        afk.update(pipeline="afk", run_id="baseline-offline")
        for costs in (cost_summary(), {**COSTS, "gross_total": 1, "contest_eligible_total": 0}):
            afk["cost_usd"] = costs
            self.assertIsNone(compare(swarm, afk)["efficiency_win"])
        for bad in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(ValueError):
                cost_summary({**COSTS, "gross_total": bad})
        with self.assertRaises(ValueError):
            cost_summary({**COSTS, "contest_eligible_total": 100})

    def test_tampered_totals_and_contradictory_clock_cannot_claim_win(self):
        self.fixture(merged=True)
        swarm = self.measured(acceptance=[self.receipt()])
        afk = copy.deepcopy(swarm)
        afk.update(pipeline="afk", run_id="baseline-offline")
        for field in ("elapsed", "count", "deliveries"):
            modified = copy.deepcopy(swarm)
            if field == "elapsed":
                modified["clock"]["elapsed_seconds"] = 1
            elif field == "count":
                modified["work"]["merged_green_prs"] = 1_000_000
            else:
                modified["deliveries"] = []
            self.assertIsNone(compare(modified, afk)["efficiency_win"])

    def test_malformed_identity_and_failed_validation_do_not_count(self):
        self.fixture(merged=True)
        path = self.batch / "T000/report.json"
        original = json.loads(path.read_text())
        for field, value in (("sandbox_id", ["malformed"]), ("agent_exit", 1), ("check_exit", None),
                             ("patch_redacted", True), ("recovery_errors", ["failed"])):
            save(path, {**original, field: value})
            self.assertEqual(self.measured()["concurrency"]["sampled_contribution_peak"], 0)

    def test_symlink_task_directory_is_rejected(self):
        self.fixture()
        path = self.batch / "T000"
        moved = self.batch.parent / "private-original"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.measured()

    def test_exemption_needs_confirmation_and_original_cost_hash_is_preserved(self):
        self.fixture()
        receipt = {**COSTS, "private_invoice_reference": "not for publication"}
        result = collect(self.batch, costs=receipt)
        self.assertNotIn("not for publication", json.dumps(result))
        from swarm_control.evidence import canonical
        self.assertEqual(result["input_receipts_sha256"]["cost"], digest(canonical(receipt)))
        with self.assertRaises(ValueError):
            cost_summary({key: value for key, value in COSTS.items() if key != "exemption_evidence_sha256"})

    def test_clock_cannot_exclude_worker_startup_or_integration_checks(self):
        self.fixture(merged=True)
        report_path = self.batch / "T000/report.json"
        report = json.loads(report_path.read_text())
        report["started_at"] = "2026-11-10T19:59:00+00:00"
        save(report_path, report)
        result = self.measured(acceptance=[self.receipt()])
        self.assertFalse(result["clock"]["coverage_validated"])
        self.assertIsNone(result["rates"]["merged_green_prs_per_hour"])


if __name__ == "__main__":
    unittest.main()
