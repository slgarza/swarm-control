"""Real graph width, explainable inference and bounded frozen-source review."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_control.dependencies import analyze_dependencies, validate_task_resources


def task(task_id, *, ownership=None, **extra):
    return {"id": task_id, "ownership": ownership or [f"src/{task_id}.py"], **extra}


class DependencyGraphTests(unittest.TestCase):
    def test_hundred_independent_tasks_supply_hundred_worker_frontier(self):
        tasks = [task(f"T{i:03}") for i in range(100)]
        original = copy.deepcopy(tasks)
        with patch("subprocess.run", side_effect=AssertionError("metadata analysis ran a process")):
            report = analyze_dependencies(tasks, workers=100)
        self.assertEqual(report["ready_count"], 100)
        self.assertEqual(report["safe_frontier_count"], 100)
        self.assertEqual(len(report["suggested_dispatch_ids"]), 100)
        self.assertEqual(report["critical_path_length"], 1)
        self.assertEqual(report["wave_sizes"], [100])
        self.assertTrue(report["supports_100_now"])
        self.assertTrue(report["requested_parallelism_possible_now"])
        self.assertFalse(report["semantic_independence_verified"])
        self.assertFalse(report["provider_capacity_verified"])
        self.assertFalse(report["authorizes_spend"])
        self.assertEqual(tasks, original)

    def test_hundred_task_serial_chain_does_not_claim_hundred_agents(self):
        tasks = [task(f"T{i:03}", dependsOn=[f"T{i-1:03}"] if i else []) for i in range(100)]
        report = analyze_dependencies(tasks, workers=100)
        self.assertEqual(report["ready_task_ids"], ["T000"])
        self.assertEqual(report["safe_frontier_count"], 1)
        self.assertEqual(report["critical_path_length"], 100)
        self.assertEqual(report["wave_sizes"], [1] * 100)
        self.assertEqual(report["shortfall"]["missing"], 99)
        self.assertFalse(report["supports_100_now"])

    def test_wide_after_bootstrap_is_distinct_from_wide_now(self):
        tasks = [task("BOOT")] + [task(f"T{i:03}", dependsOn=["BOOT"]) for i in range(100)]
        report = analyze_dependencies(tasks)
        self.assertEqual(report["safe_frontier_ids"], ["BOOT"])
        self.assertEqual(report["wave_sizes"], [1, 100])
        self.assertEqual(report["max_wave_width"], 100)
        self.assertFalse(report["supports_100_now"])
        self.assertIn("merge_prerequisites_then_replan_from_new_base", report["staging_advice"])
        self.assertTrue(report["waves_are_theoretical"])
        self.assertFalse(report["max_wave_width_is_exact_graph_width"])

    def test_large_backlog_uses_iterative_graph_and_caps_dispatch_not_width(self):
        independent = analyze_dependencies([task(f"T{i:04}") for i in range(1000)], workers=100)
        self.assertEqual(independent["safe_frontier_count"], 1000)
        self.assertEqual(len(independent["suggested_dispatch_ids"]), 100)
        chain = [task(f"T{i:04}", dependsOn=[f"T{i-1:04}"] if i else []) for i in range(1500)]
        serial = analyze_dependencies(chain, workers=100)
        self.assertEqual(serial["critical_path_length"], 1500)
        self.assertEqual(serial["safe_frontier_count"], 1)

    def test_cycle_and_self_dependency_have_witnesses_and_no_dispatch(self):
        for tasks in ([task("A1", dependsOn=["B1"]), task("B1", dependsOn=["A1"])],
                      [task("A1", dependsOn=["A1"])]):
            with self.subTest(tasks=tasks):
                report = analyze_dependencies(tasks)
                self.assertTrue(report["cycles"])
                self.assertEqual(report["cycles"][0][0], report["cycles"][0][-1])
                self.assertIsNone(report["critical_path_length"])
                self.assertEqual(report["suggested_dispatch_ids"], [])
                self.assertIn("dependency_cycle", [error["code"] for error in report["errors"]])

    def test_selection_ignores_unrelated_cycle_and_ownership_conflict(self):
        tasks = [task("A1"), task("B1", ownership=["shared"], dependsOn=["C1"]),
                 task("C1", ownership=["shared"], dependsOn=["B1"])]
        report = analyze_dependencies(tasks, selected_ids=["A1"], workers=1)
        self.assertEqual(report["task_count"], 1)
        self.assertEqual(report["inventory_task_count"], 3)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["review_hints"], [])
        self.assertEqual(report["safe_frontier_ids"], ["A1"])

    def test_selection_expands_actual_prerequisites_and_holds_dependent_task(self):
        tasks = [task("A1"), task("B1", dependsOn=["A1"]), task("C1")]
        report = analyze_dependencies(tasks, selected_ids=["B1"], workers=1)
        self.assertEqual(report["task_count"], 2)
        self.assertEqual(report["selected_task_ids"], ["B1"])
        self.assertEqual(report["ready_task_ids"], [])
        self.assertEqual(report["selected_blockers"], [
            {"task_id": "B1", "code": "dependency_not_merged", "dependency_ids": ["A1"]}])

    def test_unknown_dependency_and_missing_selection_are_explained(self):
        report = analyze_dependencies([task("A1", dependsOn=["MISSING"])], workers=1)
        self.assertEqual(report["errors"][0]["dependency_id"], "MISSING")
        self.assertFalse(report["requested_parallelism_possible_now"])
        missing = analyze_dependencies([task("A1")], selected_ids=["B1"], workers=1)
        self.assertEqual(missing["errors"][0]["code"], "unknown_task")

    def test_completed_dependencies_need_locally_merged_pr_metadata_or_caller_claim(self):
        tasks = [task("A1", status="merged"), task("B1", dependsOn=["A1"])]
        pending = analyze_dependencies(tasks, selected_ids=["B1"], workers=1)
        self.assertEqual(pending["ready_count"], 0)
        tasks[0]["pullRequest"] = 123
        ready = analyze_dependencies(tasks, selected_ids=["B1"], workers=1)
        self.assertEqual(ready["ready_task_ids"], ["B1"])
        self.assertEqual(ready["critical_path_length"], 1)
        self.assertFalse(ready["semantic_independence_verified"])
        del tasks[0]["pullRequest"]
        claimed = analyze_dependencies(tasks, selected_ids=["B1"], completed_ids=["A1"], workers=1)
        self.assertEqual(claimed["ready_task_ids"], ["B1"])

    def test_minimal_historical_merged_prerequisite_remains_compatible(self):
        tasks = [{"id": "D1", "status": "merged", "pullRequest": 10},
                 task("A1", dependsOn=["D1"])]
        report = analyze_dependencies(tasks, selected_ids=["A1"], workers=1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["ready_task_ids"], ["A1"])
        self.assertIn("verify_actual_merged_base", report["completion_scope"])
        completed = analyze_dependencies(tasks, selected_ids=["D1"], workers=1)
        self.assertEqual(completed["ready_count"], 0)
        self.assertEqual(completed["blocked_tasks"]["D1"], [{"code": "task_already_completed"}])

    def test_artifact_consumption_infers_directed_dependency_without_mutating_tasks(self):
        tasks = [task("A1", produces=["protocol:memory-v1"]),
                 task("B1", consumes=["protocol:memory-v1"])]
        report = analyze_dependencies(tasks, selected_ids=["B1"], workers=1)
        self.assertEqual(report["edges"], [{"producer": "A1", "consumer": "B1",
                          "kind": "artifact", "artifact": "protocol:memory-v1"}])
        self.assertEqual(report["ready_count"], 0)
        self.assertNotIn("dependsOn", tasks[1])

    def test_unknown_or_ambiguous_artifact_providers_and_self_cycles_fail_closed(self):
        cases = [([task("A1", consumes=["api:v1"])], "unknown_artifact_provider"),
                 ([task("A1", produces=["api:v1"]), task("B1", produces=["api:v1"]),
                   task("C1", consumes=["api:v1"])], "ambiguous_artifact_provider"),
                 ([task("A1", produces=["api:v1"], consumes=["api:v1"])], "self_artifact_dependency")]
        for tasks, expected in cases:
            with self.subTest(expected=expected):
                report = analyze_dependencies(tasks, workers=1)
                self.assertIn(expected, [item["code"] for item in report["errors"]])
                self.assertEqual(report["suggested_dispatch_ids"], [])

    def test_shared_writes_are_conflicts_without_fabricated_edge_direction(self):
        for allow_overlap in (False, True):
            report = analyze_dependencies([task("A1", ownership=["src"]),
                                           task("B1", ownership=["src/shared.py"])],
                                          workers=2, allow_overlap=allow_overlap)
            self.assertEqual(report["ready_count"], 2)
            self.assertEqual(report["safe_frontier_count"], 1)
            self.assertEqual(report["edges"], [])
            self.assertFalse(report["requested_parallelism_possible_now"])
            self.assertTrue(report["safe_frontier_is_maximal_not_maximum"])

    def test_broad_ownership_task_does_not_hide_hundred_independent_leaves(self):
        tasks = [task("A000", ownership=["src"])] + [
            task(f"L{i:03}", ownership=[f"src/F{i:03}.ts"]) for i in range(100)]
        report = analyze_dependencies(tasks, workers=100)
        self.assertEqual(report["ready_count"], 101)
        self.assertEqual(report["safe_frontier_count"], 100)
        self.assertNotIn("A000", report["safe_frontier_ids"])
        self.assertTrue(report["supports_100_now"])

    def test_shared_reads_are_independent_but_reader_writer_is_a_review_conflict(self):
        readers = [task("A1", reads=["shared/contract.json"]),
                   task("B1", reads=["shared/contract.json"])]
        report = analyze_dependencies(readers, workers=2)
        self.assertEqual(report["safe_frontier_count"], 2)
        readers[1]["ownership"] = ["shared/contract.json"]
        report = analyze_dependencies(readers, workers=2)
        self.assertEqual(report["safe_frontier_count"], 1)
        self.assertEqual(report["edges"], [])
        self.assertIn("read_write_dependency_review", [item["code"] for item in report["review_hints"]])

    def test_invalid_optional_metadata_and_secret_looking_resources_are_rejected(self):
        invalid = [{"reads": ["../outside"]}, {"produces": ["valid", "valid"]},
                   {"consumes": "api:v1"}, {"produces": ["ghp_" + "x" * 24]},
                   {"reads": ["/home/private"]}, {"reads": [["nested"]]}]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_task_resources(task("A1", **fields))
            report = analyze_dependencies([task("A1", **fields)], workers=1)
            self.assertEqual(report["errors"][0]["code"], "invalid_resource_metadata")
            encoded = json.dumps(report)
            self.assertNotIn("ghp_", encoded)
            self.assertNotIn("/home/private", encoded)
        self.assertIsNone(validate_task_resources(task("A1")))

    def test_produced_file_paths_must_stay_inside_ownership(self):
        report = analyze_dependencies([task("A1", ownership=["src/a.py"],
                                            produces=["other/generated.py"])])
        self.assertEqual(report["errors"][0]["code"], "produced_path_outside_ownership")
        with self.assertRaisesRegex(ValueError, "ownership"):
            validate_task_resources(task("A1", produces=["other/generated.py"]))
        self.assertIsNone(validate_task_resources(task("A1", ownership=["generated"],
                                                     produces=["generated/api.json", "contract:api/v1.json"])))

    def test_duplicate_ids_selection_and_declared_dependencies_are_errors(self):
        reports = [analyze_dependencies([task("A1"), task("A1")]),
                   analyze_dependencies([task("A1")], selected_ids=["A1", "A1"]),
                   analyze_dependencies([task("A1"), task("B1", dependsOn=["A1", "A1"])])]
        for report in reports:
            self.assertTrue(report["errors"])
            self.assertEqual(report["suggested_dispatch_ids"], [])

    def test_task_status_and_declared_blockers_do_not_count_ready(self):
        report = analyze_dependencies([task("A1", status="blocked"),
                                       task("B1", blockedBy=["needs-design"])])
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(report["safe_frontier_count"], 0)

    def test_frozen_input_validation_and_graph_output_are_deterministic(self):
        tasks = [task("B1", dependsOn=["A1"]), task("A1")]
        self.assertEqual(analyze_dependencies(tasks), analyze_dependencies(list(reversed(tasks))))
        for changes in ({"root": "/tmp"}, {"base_sha": "a" * 40}, {"workers": True},
                        {"workers": 0}, {"root": "/tmp", "base_sha": "HEAD"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                analyze_dependencies(tasks, **changes)

    def test_empty_workload_and_diagnostic_bounds_are_explicit(self):
        empty = analyze_dependencies([])
        self.assertEqual(empty["wave_sizes"], [])
        self.assertFalse(empty["supports_100_now"])
        shared = [task(f"T{i:03}", ownership=["shared"]) for i in range(150)]
        report = analyze_dependencies(shared)
        self.assertEqual(report["safe_frontier_count"], 1)
        self.assertTrue(any(item.get("task_ids_truncated") for item in report["review_hints"]))
        self.assertTrue(all(len(item["task_ids"]) <= 100 for item in report["review_hints"]))


class FrozenImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "target with spaces"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Offline test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("README.md", "Synthetic source; no providers.\n")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "frozen fixture")
        return self.git("rev-parse", "HEAD")

    def analyze(self, tasks, **options):
        return analyze_dependencies(tasks, root=self.root, base_sha=self.commit(), **options)

    def test_hundred_new_owned_files_are_expected_and_do_not_block_frontier(self):
        report = self.analyze([task(f"T{i:03}") for i in range(100)])
        self.assertTrue(report["supports_100_now"])
        self.assertEqual(report["static_scan"]["files_scanned"], 0)
        self.assertEqual(report["static_scan"]["new_owned_paths_not_in_base"], 100)
        self.assertFalse(report["static_scan"]["incomplete"])
        self.assertFalse(report["semantic_independence_verified"])

    def test_js_relative_import_and_typescript_js_suffix_find_future_writer_conflict(self):
        self.write("src/use.ts", 'import { api } from "./api.js";\nexport const result = api;\n')
        self.write("src/api.ts", 'export const api = "baseline";\n')
        report = self.analyze([task("A1", ownership=["src/use.ts"]),
                               task("B1", ownership=["src/api.ts"])], workers=2)
        self.assertEqual(report["edges"], [])
        self.assertEqual(report["safe_frontier_count"], 1)
        hint = next(item for item in report["review_hints"]
                    if item["code"] == "static_import_dependency_review")
        self.assertEqual(hint["task_ids"], ["A1", "B1"])
        self.assertEqual(hint["paths"], ["src/use.ts", "src/api.ts"])

    def test_relative_python_imports_are_parsed_without_executing_source(self):
        self.write("pkg/use.py", 'from .helper import value\nraise RuntimeError("TARGET_EXECUTED")\n')
        self.write("pkg/helper.py", 'value = "private-synthetic-source-marker"\n')
        report = self.analyze([task("A1", ownership=["pkg/use.py"]),
                               task("B1", ownership=["pkg/helper.py"])], workers=2)
        self.assertEqual(report["safe_frontier_count"], 1)
        encoded = json.dumps(report)
        self.assertNotIn("TARGET_EXECUTED", encoded)
        self.assertNotIn("private-synthetic-source-marker", encoded)
        self.assertNotIn(str(self.root), encoded)

    def test_declared_missing_future_input_with_unique_producer_becomes_hard_edge(self):
        tasks = [task("BUILD", ownership=["generated/api.json"], produces=["generated/api.json"]),
                 task("USE", reads=["generated/api.json"])]
        report = self.analyze(tasks, selected_ids=["USE"], workers=1)
        self.assertEqual(report["edges"], [{"producer": "BUILD", "consumer": "USE",
                          "kind": "missing_input", "artifact": "generated/api.json"}])
        self.assertEqual(report["ready_count"], 0)
        self.assertEqual(report["task_count"], 2)
        self.assertEqual(report["blocked_tasks"]["USE"][0]["dependency_ids"], ["BUILD"])

    def test_existing_input_with_writer_is_review_hint_instead_of_invented_hard_edge(self):
        self.write("generated/api.json", '{}\n')
        tasks = [task("BUILD", ownership=["generated/api.json"], produces=["generated/api.json"]),
                 task("USE", reads=["generated/api.json"])]
        report = self.analyze(tasks, workers=2)
        self.assertEqual(report["edges"], [])
        self.assertEqual(report["ready_count"], 2)
        self.assertEqual(report["safe_frontier_count"], 1)

    def test_static_scan_uses_exact_git_base_and_ignores_dirty_changes(self):
        self.write("src/a.ts", 'export const value = 1;\n')
        self.write("src/b.ts", 'export const b = 2;\n')
        base = self.commit()
        self.write("src/a.ts", 'import { b } from "./b";\n')
        report = analyze_dependencies([task("A1", ownership=["src/a.ts"]),
                                       task("B1", ownership=["src/b.ts"])],
                                      root=self.root, base_sha=base, workers=2)
        self.assertEqual(report["safe_frontier_count"], 2)
        self.assertEqual(report["base_sha"], base)
        self.assertFalse(any(item["code"] == "static_import_dependency_review"
                             for item in report["review_hints"]))

    def test_scan_bounds_and_unavailable_base_do_not_claim_verified_frontier(self):
        self.write("src/a.py", "value = 1\n")
        self.write("src/b.py", "value = 2\n")
        base = self.commit()
        tasks = [task("A1", ownership=["src/a.py"]), task("B1", ownership=["src/b.py"])]
        with patch("swarm_control.dependencies.MAX_SCAN_FILES", 1):
            report = analyze_dependencies(tasks, root=self.root, base_sha=base, workers=2)
        self.assertTrue(report["static_scan"]["truncated"])
        self.assertTrue(report["static_scan"]["incomplete"])
        self.assertFalse(report["requested_parallelism_possible_now"])
        self.assertEqual(report["suggested_dispatch_ids"], [])
        absent = analyze_dependencies(tasks, root=self.root, base_sha="f" * 40, workers=2)
        self.assertTrue(absent["static_scan"]["unavailable"])
        self.assertFalse(absent["requested_parallelism_possible_now"])

    def test_secret_named_files_and_symlinks_are_never_read_as_source(self):
        self.write("src/credentials.py", 'TOKEN="synthetic-credential-marker"\n')
        self.write("outside.py", 'raise RuntimeError("outside-source-marker")\n')
        (self.root / "src/link.py").symlink_to("../outside.py")
        report = self.analyze([task("A1", ownership=["src"])], workers=1)
        self.assertEqual(report["static_scan"]["files_scanned"], 0)
        self.assertNotIn("synthetic-credential-marker", json.dumps(report))
        self.assertNotIn("outside-source-marker", json.dumps(report))

    def test_static_import_of_missing_planned_module_is_explainable_review_hint(self):
        self.write("src/use.ts", 'import { future } from "./future";\n')
        report = self.analyze([task("A1", ownership=["src/use.ts"]),
                               task("B1", ownership=["src/future.ts"])], workers=2)
        self.assertEqual(report["edges"], [])
        self.assertIn("missing_import_with_provider", [item["code"] for item in report["review_hints"]])
        self.assertEqual(report["safe_frontier_count"], 1)
        self.assertEqual(report["review_required_task_ids"], ["A1"])

    def test_missing_import_can_identify_unselected_exact_file_owner_without_inventing_order(self):
        self.write("src/use.ts", 'import { future } from "./future";\n')
        report = self.analyze([task("A1", ownership=["src/use.ts"]),
                               task("B1", ownership=["src/future.ts"])],
                              selected_ids=["A1"], workers=1)
        self.assertEqual(report["task_count"], 1)
        self.assertEqual(report["edges"], [])
        hint = next(item for item in report["review_hints"] if item["code"] == "missing_import_with_provider")
        self.assertEqual(hint["task_ids"], ["A1", "B1"])
        self.assertEqual(report["review_required_task_ids"], ["A1"])
        self.assertFalse(report["requested_parallelism_possible_now"])

    def test_git_source_output_is_capped_before_allocating_unbounded_body(self):
        self.write("src/large.py", "value = 1\n" * 100)
        base = self.commit()
        with patch("swarm_control.dependencies.MAX_TREE_BYTES", 20):
            report = analyze_dependencies([task("A1")], root=self.root, base_sha=base, workers=1)
        self.assertTrue(report["static_scan"]["incomplete"])
        self.assertTrue(report["static_scan"]["unavailable"])
        self.assertFalse(report["requested_parallelism_possible_now"])

    def test_unparsable_existing_python_source_makes_strict_frontier_incomplete(self):
        self.write("src/a.py", "from .broken import (\n")
        self.write("src/b.py", "value = 1\n")
        report = self.analyze([task("A1", ownership=["src/a.py"]),
                               task("B1", ownership=["src/b.py"])], workers=2)
        self.assertEqual(report["safe_frontier_count"], 2)
        self.assertEqual(report["static_scan"]["parse_failures"], 1)
        self.assertTrue(report["static_scan"]["incomplete"])
        self.assertFalse(report["requested_parallelism_possible_now"])
        self.assertEqual(report["suggested_dispatch_ids"], [])
        self.assertIn("source_parse_unavailable", [item["code"] for item in report["review_hints"]])
