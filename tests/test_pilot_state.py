import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from swarm_control.pilot_state import Ledger, peak_intervals, verify_github_tasks


class LedgerTests(unittest.TestCase):
    def test_concurrent_claims_only_one_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "state.db")

            def claim(index):
                try:
                    ledger.claim([{"id": "T01"}], str(index), "a" * 40)
                    return True
                except ValueError:
                    return False

            with ThreadPoolExecutor(max_workers=10) as pool:
                self.assertEqual(sum(pool.map(claim, range(100))), 1)
            self.assertEqual(len(ledger.rows()), 1)

    def test_claim_batch_is_atomic_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            ledger = Ledger(path)
            ledger.claim([{"id": "T01"}], "batch1", "a" * 40)
            reopened = Ledger(path)
            with self.assertRaises(ValueError):
                reopened.claim([{"id": "T02"}, {"id": "T01"}], "batch2", "a" * 40)
            self.assertEqual([r["task_id"] for r in ledger.rows()], ["T01"])
            with self.assertRaises(ValueError):
                ledger.finish("T01", "wrong", "merged", "report")

    def test_failed_attempt_requires_explicit_retry_and_merged_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "state.db")
            ledger.claim([{"id": "T01"}], "one", "a" * 40)
            ledger.finish("T01", "one", "failed", "report")
            with self.assertRaises(ValueError):
                ledger.claim([{"id": "T01"}], "two", "a" * 40)
            ledger.claim([{"id": "T01"}], "two", "a" * 40, retry=True)
            ledger.finish("T01", "two", "merged", "report")
            with self.assertRaises(ValueError):
                ledger.claim([{"id": "T01"}], "three", "a" * 40, retry=True)

    def test_closed_issues_and_existing_prs_block_dispatch(self):
        task = {"id": "T01", "issue": 1}

        def query(path):
            return [] if "/pulls?" in path else {"state": "closed"}

        with self.assertRaises(ValueError):
            verify_github_tasks([task], Path("."), "a" * 40, query=query, repository="example/project")

        def open_pr(path):
            return (
                [{"title": "[T01] existing work"}]
                if "/pulls?" in path
                else {"state": "open"}
            )

        with self.assertRaises(ValueError):
            verify_github_tasks([task], Path("."), "a" * 40, query=open_pr, repository="example/project")

    def test_delayed_worker_checkpoint_cannot_demote_a_merged_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "state.db")
            ledger.claim([{"id": "T01"}], "batch1", "a" * 40)
            ledger.finish("T01", "batch1", "merged", "integration.json")
            ledger.finish("T01", "batch1", "awaiting_integration", "worker-report.json")
            self.assertEqual(ledger.rows()[0]["state"], "merged")
            self.assertEqual(ledger.rows()[0]["report"], "integration.json")
            with self.assertRaises(ValueError):
                ledger.finish("T01", "wrong-batch", "failed", "report.json")

    def test_duplicate_issues_are_rejected_before_any_github_query(self):
        query = Mock()
        with self.assertRaisesRegex(ValueError, "duplicate GitHub issue"):
            verify_github_tasks(
                [{"id": "T01", "issue": 12}, {"id": "T02", "issue": 12}],
                Path("."), "a" * 40, query=query, repository="example/project",
            )
        query.assert_not_called()

    def test_duplicate_work_on_second_pr_page_blocks_admission(self):
        def query(path):
            if "/pulls?" in path:
                if "page=2" in path:
                    return [{"number": 101, "title": "[T01] already in progress"}]
                return [{"number": index + 1, "title": "unrelated"} for index in range(100)]
            return {"state": "open"}

        with self.assertRaisesRegex(ValueError, "already has an open PR"):
            verify_github_tasks([{"id": "T01", "issue": 1}], Path("."), "a" * 40,
                                query=query, repository="example/project")

    def test_full_pr_pages_are_read_to_completion_and_repeated_pages_fail_closed(self):
        first = [{"number": index + 1, "title": "unrelated"} for index in range(100)]
        query = Mock(side_effect=[first, [], {"state": "open"}])
        verify_github_tasks([{"id": "T01", "issue": 1}], Path("."), "a" * 40,
                            query=query, repository="example/project")
        self.assertIn("page=2", query.call_args_list[1].args[0])
        with self.assertRaisesRegex(ValueError, "changed during pagination"):
            verify_github_tasks([{"id": "T01", "issue": 1}], Path("."), "a" * 40,
                                query=Mock(side_effect=[first, first]), repository="example/project")

    def test_missing_or_unmerged_dependency_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = root / "swarm/tasks"
            tasks.mkdir(parents=True)
            (tasks / "P03.json").write_text(json.dumps({"pullRequest": 5}))

            def query(path):
                if "/pulls?" in path:
                    return []
                return {"state": "open"} if "/issues/" in path else {"merged": False}

            with self.assertRaises(ValueError):
                verify_github_tasks(
                    [{"id": "T01", "issue": 1, "dependsOn": ["P03"]}],
                    root,
                    "a" * 40,
                    query=query, repository="example/project",
                )

    def test_peak_counts_overlap_not_total_sessions(self):
        self.assertEqual(peak_intervals([(1, 3), (2, 4), (3, 5), (None, 8)]), 2)
        self.assertEqual(peak_intervals([(1, 2), (2, 3), (3, 4)]), 1)


class BudgetAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.db"
        self.ledger = Ledger(self.path)

    def allocations(self):
        with self.ledger.connect() as db:
            return {
                "model": dict(db.execute("SELECT run_id,cents FROM pilot_allocations")),
                "infra": dict(db.execute("SELECT run_id,cents FROM pilot_infra_allocations")),
            }

    def test_probe_plus_ten_agent_batch_survive_reopen_and_keep_failed_reserves(self):
        self.ledger.allocate("probe", 50, infra_cents=100)
        tasks = [{"id": f"T{index:02}"} for index in range(1, 11)]
        self.ledger.claim(tasks, "pilot", "a" * 40, budget_cents=1750, infra_cents=450)
        self.ledger = Ledger(self.path)
        self.assertEqual(self.allocations(), {
            "model": {"probe": 50, "pilot": 1750},
            "infra": {"probe": 100, "pilot": 450},
        })
        self.assertEqual(len(self.ledger.rows()), 10)
        self.ledger.finish("T01", "pilot", "failed", "failed-report")
        with self.assertRaisesRegex(ValueError, "model allowance is exhausted"):
            self.ledger.claim([tasks[0]], "retry", "a" * 40, retry=True,
                              budget_cents=1, infra_cents=1)
        self.assertEqual(self.ledger.rows()[0]["state"], "failed")
        self.assertNotIn("retry", self.allocations()["model"])
        self.assertEqual(sum(self.allocations()["infra"].values()), 550)

    def test_budget_rejection_leaves_no_running_tasks_or_events_and_can_retry(self):
        self.ledger.allocate("probe", 100, infra_cents=100)
        tasks = [{"id": "T01"}, {"id": "T02"}]
        with self.assertRaisesRegex(ValueError, "model allowance is exhausted"):
            self.ledger.claim(tasks, "over-budget", "a" * 40,
                              budget_cents=1750, infra_cents=450)
        self.assertEqual(self.ledger.rows(), [])
        with self.ledger.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.allocations(), {"model": {"probe": 100}, "infra": {"probe": 100}})
        self.ledger.claim(tasks, "corrected", "a" * 40, budget_cents=1700, infra_cents=450)
        self.assertEqual([row["task_id"] for row in self.ledger.rows()], ["T01", "T02"])

    def test_infrastructure_rejection_rolls_back_model_reserve_and_claims(self):
        self.ledger.allocate("earlier", 100, infra_cents=650)
        with self.assertRaisesRegex(ValueError, "infrastructure allowance is exhausted"):
            self.ledger.claim([{"id": "T01"}], "too-much-infra", "a" * 40,
                              budget_cents=500, infra_cents=51)
        self.assertEqual(self.ledger.rows(), [])
        self.assertEqual(self.allocations(), {"model": {"earlier": 100}, "infra": {"earlier": 650}})

    def test_smoke_runs_consume_the_same_cumulative_infrastructure_allowance(self):
        self.ledger.allocate("probe", 50, infra_cents=100)
        self.ledger.allocate("pilot", 1750, infra_cents=450)
        self.ledger.allocate("smoke", 0, infra_cents=135)
        with self.assertRaisesRegex(ValueError, "infrastructure allowance is exhausted"):
            self.ledger.allocate("extra-smoke", 0, infra_cents=135)
        self.ledger.allocate("final-reserve", 0, infra_cents=15)
        with self.assertRaisesRegex(ValueError, "infrastructure allowance is exhausted"):
            self.ledger.allocate("one-cent-over", 0, infra_cents=1)
        self.assertEqual(sum(self.allocations()["model"].values()), 1800)
        self.assertEqual(sum(self.allocations()["infra"].values()), 700)
        self.assertNotIn("extra-smoke", self.allocations()["model"])

    def test_duplicate_claims_and_run_ids_cannot_leak_or_replace_allocations(self):
        self.ledger.claim([{"id": "T01"}], "first", "a" * 40,
                          budget_cents=50, infra_cents=100)
        original = self.allocations()
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.ledger.claim([{"id": "T02"}, {"id": "T01"}], "conflict", "a" * 40,
                              budget_cents=50, infra_cents=100)
        with self.assertRaisesRegex(ValueError, "already has an allocation"):
            self.ledger.allocate("first", 0, infra_cents=0)
        with self.assertRaisesRegex(ValueError, "duplicate task IDs"):
            self.ledger.claim([{"id": "T02"}, {"id": "T02"}], "duplicate", "a" * 40,
                              budget_cents=50, infra_cents=100)
        self.assertEqual(self.allocations(), original)
        self.assertEqual([row["task_id"] for row in self.ledger.rows()], ["T01"])

    def test_concurrent_admissions_cannot_overdraw_either_allowance(self):
        def attempt(index):
            try:
                self.ledger.claim([{"id": f"T{index:02}"}], f"run-{index}", "a" * 40,
                                  budget_cents=180, infra_cents=70)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=10) as pool:
            self.assertEqual(sum(pool.map(attempt, range(30))), 10)
        self.assertEqual(len(self.ledger.rows()), 10)
        self.assertEqual(sum(self.allocations()["model"].values()), 1800)
        self.assertEqual(sum(self.allocations()["infra"].values()), 700)
        self.assertEqual(len(self.allocations()["model"]), 10)

    def test_cents_are_nonnegative_finite_integers_and_invalid_inputs_do_not_claim(self):
        for amount in (-1, True, False, 1.0, float("nan"), float("inf"), "1", None):
            with self.subTest(amount=amount):
                with self.assertRaisesRegex(ValueError, "invalid model allowance"):
                    self.ledger.allocate("bad-model", amount)
                with self.assertRaisesRegex(ValueError, "invalid infrastructure allowance"):
                    self.ledger.claim([{"id": "T01"}], "bad-infra", "a" * 40,
                                      budget_cents=0, infra_cents=amount)
                if amount is not None:
                    with self.assertRaisesRegex(ValueError, "invalid model allowance"):
                        self.ledger.claim([{"id": "T01"}], "bad-model-claim", "a" * 40,
                                          budget_cents=amount)
        for model, infra in ((1801, 0), (0, 701)):
            with self.assertRaises(ValueError):
                self.ledger.allocate("above-limit", model, infra_cents=infra)
        self.assertEqual(self.ledger.rows(), [])
        self.assertEqual(self.allocations(), {"model": {}, "infra": {}})


if __name__ == "__main__":
    unittest.main()
