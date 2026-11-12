"""Independent acceptance checks for inferred admission gates and HTTP privacy."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_control.preflight import build_preflight
from swarm_control.project import load_project
from swarm_control.workload import public_analysis
from swarm_control.workspace import Workspace, _options
from tests.support import make_project


class WorkloadGlueReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = make_project(self.root / "target")
        self.write_task("D1", {
            "id": "D1", "title": "Merged producer", "prompt": "Original scoped producer",
            "ownership": ["src/producer.py"], "acceptance": ["Contract"], "status": "merged",
            "pullRequest": 10, "produces": ["contract:api-v1"],
        })
        consumer_path = self.project.tasks_dir / "A1.json"
        consumer = json.loads(consumer_path.read_text())
        consumer["consumes"] = ["contract:api-v1"]
        self.write_task("A1", consumer)
        self.git("add", ".")
        self.git("commit", "-qm", "freeze inferred prerequisite")
        self.base = self.git("rev-parse", "HEAD")
        self.requests = []

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.project.root, text=True).strip()

    def write_task(self, task_id, value):
        (self.project.tasks_dir / f"{task_id}.json").write_text(json.dumps(value))

    def query(self, path):
        self.requests.append(path)
        if "/pulls?" in path:
            return []
        if "/issues/" in path:
            return {"state": "open"}
        if path.endswith("/pulls/10"):
            return {"merged": True, "merge_commit_sha": "f" * 40}
        if "/compare/" in path:
            return {"status": "identical", "merge_base_commit": {"sha": self.base}}
        if "/check-runs?" in path:
            return {"total_count": 1, "check_runs": [{"id": 1, "name": "checks",
                    "head_sha": self.base, "status": "completed", "conclusion": "success",
                    "app": {"slug": "github-actions"}}]}
        raise AssertionError("Unexpected synthetic GitHub query")

    def test_inferred_artifact_producer_must_really_be_in_selected_base(self):
        with patch("swarm_control.pilot_state.Ledger", side_effect=AssertionError("ledger opened")):
            report = build_preflight(self.project, ["A1"], base=self.base,
                                     online=True, query=self.query)
        self.assertTrue(report["local_workload_ready"])
        self.assertFalse(report["github_source_ready"])
        self.assertFalse(report["preparation_checks_passed"])
        self.assertIn("repos/example/project/pulls/10", self.requests)
        self.assertIn("github_issues_and_dependencies_verified_failed", report["github"]["blockers"])
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_unselected_producer_drift_blocks_saved_launch_before_any_runtime_write(self):
        self.project.path.write_text(self.project.path.read_text().replace(
            "live_enabled = false", "live_enabled = true"))
        self.project = load_project(self.project.path)
        self.git("add", ".")
        self.git("commit", "-qm", "explicit synthetic live fixture")
        workspace = Workspace(self.root / "private/workspace.json")
        project_id = workspace.connect_project(str(self.project.root))["project_id"]
        run = workspace.prepare_run(project_id, {"task_ids": ["A1"], "model_budget": 0.50}, save=True)
        producer_path = self.project.tasks_dir / "D1.json"
        producer = json.loads(producer_path.read_text())
        producer["pullRequest"] = 999
        producer_path.write_text(json.dumps(producer))
        with (patch("swarm_control.operations.OperationsJournal", side_effect=AssertionError("runtime write")),
              self.assertRaisesRegex(ValueError, "Draft source or configuration changed")):
            workspace.launch(project_id, run["id"], gateway_url="https://synthetic-review.modal.run", execute=True)
        self.assertEqual(workspace.run_detail(project_id, run["id"])["status"], "draft")
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_http_projection_drops_credential_looking_ids_even_from_forged_stored_metadata(self):
        credential_like = "SK-" + "A" * 24
        source = {"ready_task_ids": [credential_like, "A1"],
                  "safe_frontier_ids": [credential_like], "critical_path": [credential_like],
                  "suggested_dispatch_ids": [credential_like],
                  "errors": [{"code": "invalid_task", "task_ids": [credential_like]}],
                  "review_hints": [{"code": "shared_ownership", "task_ids": [credential_like]}],
                  "edges": [{"producer": credential_like, "consumer": "A1"}]}
        encoded = json.dumps(public_analysis(source))
        self.assertNotIn(credential_like, encoded)
        self.assertIn("A1", encoded)

    def test_hundred_agent_draft_uses_same_default_independent_frontier_as_browser(self):
        options = _options({"task_ids": [f"T{i:03}" for i in range(100)], "workers": 100})
        self.assertEqual(options["require_parallel"], 100)
