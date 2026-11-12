"""Whole-backlog analysis is read-only and gates real dependency admission."""

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from swarm_control import cli, modal_runner, preflight
from swarm_control.project import load_project
from swarm_control.workload import (
    analyze_project,
    effective_tasks,
    load_inventory,
    preparation_blockers,
    public_analysis,
    public_context,
    require_cohort,
)
from swarm_control.workspace import Workspace
from tests.support import make_project


class WorkloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = make_project(self.root / "target")
        text = self.project.path.read_text().replace("[budget]", "[runtime]\ngateway_concurrency = 100\n[budget]\nmax_live_agents = 100")
        self.project.path.write_text(text)
        self.project = load_project(self.project.path)

    def tasks(self, count, chain=False):
        (self.project.tasks_dir / "A1.json").unlink()
        ids = [f"T{i:04}" for i in range(count)]
        for index, task_id in enumerate(ids):
            (self.project.tasks_dir / f"{task_id}.json").write_text(json.dumps({
                "id": task_id, "title": "Bounded task", "prompt": "Implement the frozen contract",
                "ownership": [f"src/{task_id}.py"], "acceptance": ["Contract passes"],
                "issue": index + 1, "status": "ready",
                "dependsOn": [ids[index - 1]] if chain and index else [],
            }))
        subprocess.run(["git", "add", "."], cwd=self.project.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "freeze workload"], cwd=self.project.root,
                       check=True, capture_output=True)
        return ids

    def test_hundred_independent_tasks_keep_hundred_worker_capacity(self):
        ids = self.tasks(100)
        result = analyze_project(self.project, ids, workers=100)
        self.assertEqual(result["safe_frontier_count"], 100)
        self.assertTrue(result["supports_100_now"])
        self.assertEqual(preparation_blockers(result, 100), [])
        self.assertFalse(result["launch_ready"])
        self.assertFalse(result["semantic_independence_verified"])
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_hundred_serial_tasks_fail_explicit_parallelism_validator(self):
        ids = self.tasks(100, chain=True)
        result = analyze_project(self.project, ids, workers=100)
        self.assertEqual(result["safe_frontier_count"], 1)
        self.assertEqual(result["critical_path_length"], 100)
        self.assertIn("independent_frontier_below_required_parallelism", preparation_blockers(result, 100))
        with self.assertRaisesRegex(ValueError, "Dependency preparation"):
            require_cohort(result, 100)
        output = io.StringIO()
        with (patch("sys.argv", ["swarm-control", "analyze", *ids, "--project", str(self.project.path),
                                 "--workers", "100", "--require-parallel", "100"]),
              redirect_stdout(output)):
            self.assertEqual(cli.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["safe_frontier_count"], 1)
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_direct_run_rejects_serial_hundred_before_runtime_or_provider_admission(self):
        ids = self.tasks(100, chain=True)
        self.project.path.write_text(self.project.path.read_text().replace(
            "live_enabled = false", "live_enabled = true"))
        with (
            patch.multiple(modal_runner, ROOT=self.project.root, PROJECT=None, REPOSITORY=None, APP=None),
            patch("swarm_control.operations.OperationsJournal", side_effect=AssertionError("runtime opened")),
            patch.object(modal_runner, "verify_github_tasks", side_effect=AssertionError("remote queried")),
            patch("sys.argv", ["swarm-control", "run", *ids, "--project", str(self.project.path),
                               "--workers", "100", "--base", "HEAD", "--model", "gpt-6.1-sol",
                               "--model-budget", "1", "--gateway-url", "https://synthetic.modal.run"]),
            self.assertRaisesRegex(ValueError, "Dependency preparation"),
        ):
            modal_runner.main()
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_thousand_task_backlog_is_distinct_from_hundred_execution_slots(self):
        ids = self.tasks(1000)
        result = analyze_project(self.project, workers=100)
        self.assertEqual(result["selected_task_count"], 1000)
        self.assertEqual(result["safe_frontier_count"], 1000)
        self.assertEqual(len(result["suggested_dispatch_ids"]), 100)
        self.assertEqual(result["inventory_task_count"], 1000)
        self.assertEqual(len(ids), 1000)

    def test_shared_writes_cannot_fill_default_hundred_independent_slots(self):
        ids = self.tasks(100)
        for task_id in ids:
            path = self.project.tasks_dir / f"{task_id}.json"
            task = json.loads(path.read_text())
            task["ownership"] = ["src/shared.py"]
            path.write_text(json.dumps(task))
        result = analyze_project(self.project, ids, workers=100, allow_overlap=True)
        self.assertEqual(result["ready_count"], 100)
        self.assertEqual(result["safe_frontier_count"], 1)
        self.assertIn("independent_frontier_below_required_parallelism", preparation_blockers(result, 100))
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_frozen_manifest_selects_only_explicit_workload(self):
        ids = self.tasks(101)
        manifest = self.root / "selection.json"
        manifest.write_text(json.dumps({"taskIds": ids[:100]}))
        output = io.StringIO()
        with (patch("sys.argv", ["swarm-control", "analyze", "--project", str(self.project.path),
                                 "--manifest", str(manifest), "--require-parallel", "100"]),
              redirect_stdout(output)):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["selected_task_count"], 100)

    def test_preflight_and_saved_draft_keep_graph_observations_and_do_not_spend(self):
        ids = self.tasks(2, chain=True)
        result = preflight.build_preflight(self.project, ids, workers=2, base="HEAD", require_parallel=2)
        self.assertFalse(result["preparation_checks_passed"])
        self.assertIn("task_dependencies_not_ready", result["local_workload_blockers"])
        self.assertIn("independent_frontier_below_required_parallelism", result["local_workload_blockers"])
        registry = Workspace(self.root / "private/workspace.json")
        project_id = registry.connect_project(str(self.project.root))["project_id"]
        saved = registry.prepare_run(project_id, {"task_ids": ids, "workers": 2, "require_parallel": 2}, save=True)
        observed = saved["preflight"]["dependency_analysis"]
        self.assertEqual(observed["safe_frontier_count"], 1)
        self.assertFalse(saved["preflight"]["local_workload_ready"])
        self.assertNotIn(str(self.root), json.dumps(saved))
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_producer_metadata_drift_changes_binding_for_unselected_dependency(self):
        ids = self.tasks(2)
        first = self.project.tasks_dir / f"{ids[0]}.json"
        second = self.project.tasks_dir / f"{ids[1]}.json"
        data = json.loads(first.read_text())
        data.update(produces=["public-contract"], status="merged", pullRequest=1)
        first.write_text(json.dumps(data))
        data = json.loads(second.read_text())
        data.update(consumes=["public-contract"])
        second.write_text(json.dumps(data))
        registry = Workspace(self.root / "private/workspace.json")
        project_id = registry.connect_project(str(self.project.root))["project_id"]
        before = registry.prepare_run(project_id, {"task_ids": [ids[1]]}, save=True)
        recorded = registry._run(registry._project(registry._read(), project_id), before["id"])
        data = json.loads(first.read_text())
        data["pullRequest"] = 999
        first.write_text(json.dumps(data))
        _, after = registry._preflight(self.project, recorded["options"])
        self.assertNotEqual(recorded["bindings"]["dependency_inventory_sha256"], after["dependency_inventory_sha256"])

    def test_inferred_dependencies_use_the_existing_github_ancestor_gate(self):
        tasks = [{"id": "T02", "dependsOn": ["T00"]}]
        derived = effective_tasks(tasks, {"edges": [{"producer": "T01", "consumer": "T02", "kind": "artifact"}]})
        self.assertEqual(derived[0]["dependsOn"], ["T00", "T01"])
        self.assertEqual(tasks[0]["dependsOn"], ["T00"])

    def test_safe_browser_projection_excludes_source_paths_commands_and_raw_text(self):
        source = {"task_count": 100, "ready_count": 100, "safe_frontier_count": 100,
                  "errors": [{"code": "missing_dependency", "task_ids": ["T01"], "raw": "private"}],
                  "review_hints": [{"code": "read_write_conflict", "task_ids": ["T01", "T02"],
                                    "paths": [str(self.root)], "source": "customer code"}],
                  "edges": [{"producer": "T01", "consumer": "T02", "source": "private"}],
                  "raw": "credential"}
        received = json.dumps(public_analysis(source))
        for forbidden in (str(self.root), "customer code", "private", "credential"):
            self.assertNotIn(forbidden, received)
        self.assertIsNone(public_context({"token_count": 100})["token_count"])

    def test_duplicate_keys_and_symlinks_fail_without_creating_state(self):
        path = self.project.tasks_dir / "A1.json"
        path.write_text('{"id":"A1","id":"A1"}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_inventory(self.project)
        path.unlink()
        path.symlink_to(self.project.root / "README.md")
        with self.assertRaises(ValueError):
            load_inventory(self.project)
        self.assertFalse((self.project.root / ".swarm").exists())
