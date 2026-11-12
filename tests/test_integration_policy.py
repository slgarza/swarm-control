import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from swarm_control import pilot_integrate as integration
from swarm_control.pilot_state import Ledger


class IntegrationPolicyTests(unittest.TestCase):
    def setUp(self):
        # Source binding has real prepared-artifact coverage in the pipeline
        # suite; isolate the existing financial policy gates here.
        binding_patch = patch.object(integration, "check_source_bindings")
        binding_patch.start()
        self.addCleanup(binding_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "state with spaces?#"
        self.root.mkdir()
        self.path = self.root / "control.sqlite3"
        self.ledger = Ledger(self.path, model_limit_cents=2400, infra_limit_cents=900)
        self.ledger.allocate("prior-run", 500, 100)
        self.ledger.claim([{"id": "T01"}], "batch", "a" * 40)
        self.directory = self.root / "batch" / "T01"
        self.directory.mkdir(parents=True)
        self.task = {"id": "T01", "title": "Useful work", "issue": 12,
                     "ownership": ["src"], "acceptance": ["Implements the reviewed contract"]}
        self.state = {"status": "prepared", "task_id": "T01", "batch_id": "batch",
                      "branch": "swarm/t01", "patch_sha256": "c" * 64,
                      "tested_base_sha": "a" * 40, "tested_tree": "d" * 40,
                      "checks_started_at": "2026-11-11T16:00:00Z",
                      "checks_finished_at": "2026-11-11T16:01:00Z"}
        self.project = SimpleNamespace(
            model_limit_cents=2400, infra_limit_cents=900, required_checks=("checks",),
            ledger=Mock(side_effect=lambda path: Ledger(path, model_limit_cents=2400, infra_limit_cents=900)))
        for name, value in (("PROJECT", self.project), ("REPO", "example/project")):
            context = patch.object(integration, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(integration, "common_state", return_value=self.root)
        context.start()
        self.addCleanup(context.stop)
        self.github_requests = []
        self.git_calls = []
        self.merged = False

    def snapshot(self):
        with sqlite3.connect(self.path) as db:
            history = list(db.iterdump())
        return self.path.read_bytes(), history

    def change_policy(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE budget_policy SET model_limit_cents=2500 WHERE singleton=1")

    def github(self, path, *, method="GET", body=None):
        self.github_requests.append((path, method))
        if path == "git/ref/heads/main":
            return {"object": {"sha": "a" * 40}}
        if path.startswith("issues/"):
            return {"state": "open"}
        if path.startswith("pulls?"):
            return []
        if path.endswith("/merge"):
            self.merged = True
            return {"merged": True, "sha": "e" * 40}
        if "check-runs" in path:
            return {"total_count": 1, "check_runs": [{"name": "checks", "app": {"slug": "github-actions"},
                    "head_sha": "b" * 40, "status": "completed", "conclusion": "success"}]}
        return {"number": 55, "html_url": "https://example.invalid/pr/55", "state": "open",
                "draft": False, "base": {"ref": "main"}, "head": {"sha": "b" * 40, "ref": "swarm/t01"},
                "merged": self.merged, "merge_commit_sha": "e" * 40 if self.merged else None,
                "merged_at": "2026-11-11T16:02:00Z" if self.merged else None}

    def git(self, root, *arguments):
        self.git_calls.append(arguments)
        return "b" * 40 if arguments == ("rev-parse", "HEAD") else ""

    def test_mismatched_model_or_infra_blocks_publish_and_merge_before_any_effect(self):
        before = self.snapshot()
        for field in ("model_limit_cents", "infra_limit_cents"):
            matching = getattr(self.project, field)
            setattr(self.project, field, matching + 1)
            for phase in ("publish", "merge"):
                with self.subTest(field=field, phase=phase), patch.object(integration, "github") as github, patch.object(integration, "git") as git, patch.object(integration, "candidate") as candidate:
                    with self.assertRaisesRegex(ValueError, "selected profile differs"):
                        if phase == "publish":
                            integration.publish(self.directory, self.task, {}, copy.deepcopy(self.state))
                        else:
                            integration.merge(self.directory, self.task, {**self.state, "status": "published"})
                    github.assert_not_called()
                    git.assert_not_called()
                    candidate.assert_not_called()
                    self.project.ledger.assert_not_called()
                    self.assertEqual(before, self.snapshot())
            setattr(self.project, field, matching)

    def test_matching_preflight_is_read_only_and_does_not_construct_ledger(self):
        before = self.snapshot()
        integration.require_recorded_budget_policy()
        self.assertEqual(before, self.snapshot())
        self.project.ledger.assert_not_called()

    def test_missing_ledger_preserves_integration_and_does_not_create_state(self):
        missing = self.root / "no-accounting"
        with patch.object(integration, "common_state", return_value=missing):
            integration.require_recorded_budget_policy()
        self.assertFalse(missing.exists())
        self.project.ledger.assert_not_called()

    def test_legacy_empty_or_invalid_policy_fails_closed_without_initialization(self):
        for state in ("legacy", "empty", "invalid"):
            with self.subTest(state=state):
                path = self.root / state
                path.mkdir()
                ledger_path = path / "control.sqlite3"
                if state == "invalid":
                    ledger_path.write_bytes(b"Invalid original SQLite bytes; preserve them.")
                else:
                    with sqlite3.connect(ledger_path) as db:
                        db.execute("CREATE TABLE events(id INTEGER, state TEXT)")
                        db.execute("INSERT INTO events VALUES(1, 'original')")
                        if state == "empty":
                            db.execute("CREATE TABLE budget_policy(singleton INTEGER,model_limit_cents INTEGER,infra_limit_cents INTEGER)")
                before = ledger_path.read_bytes()
                with patch.object(integration, "common_state", return_value=path), self.assertRaisesRegex(ValueError, "reconcile"):
                    integration.require_recorded_budget_policy()
                self.assertEqual(before, ledger_path.read_bytes())
        self.project.ledger.assert_not_called()

    def test_policy_is_rechecked_immediately_before_branch_push(self):
        def commit_then_change(root, *arguments):
            if arguments[0] == "commit":
                self.change_policy()
            return self.git(root, *arguments)
        with patch.object(integration, "candidate", return_value=self.root), patch.object(integration, "git", side_effect=commit_then_change), patch.object(integration, "github", side_effect=self.github), self.assertRaisesRegex(ValueError, "selected profile differs"):
            integration.publish(self.directory, self.task, {}, self.state)
        self.assertFalse(any(arguments[0] == "push" for arguments in self.git_calls))
        self.assertFalse(any(method != "GET" for _, method in self.github_requests))
        self.project.ledger.assert_not_called()

    def test_policy_is_rechecked_after_push_before_pr_creation(self):
        def query_then_change(path, **kwargs):
            if path.startswith("pulls?"):
                self.change_policy()
            return self.github(path, **kwargs)
        with patch.object(integration, "candidate", return_value=self.root), patch.object(integration, "git", side_effect=self.git), patch.object(integration, "github", side_effect=query_then_change), self.assertRaisesRegex(ValueError, "selected profile differs"):
            integration.publish(self.directory, self.task, {}, self.state)
        self.assertTrue(any(arguments[0] == "push" for arguments in self.git_calls))
        self.assertFalse(any(method == "POST" for _, method in self.github_requests))
        self.project.ledger.assert_not_called()

    def test_policy_is_rechecked_after_github_reads_before_merge_put(self):
        def query_then_change(path, **kwargs):
            if path == "git/ref/heads/main":
                self.change_policy()
            return self.github(path, **kwargs)
        state = {**self.state, "status": "published", "pr_number": 55, "head_sha": "b" * 40}
        with patch.object(integration, "candidate"), patch.object(integration, "github", side_effect=query_then_change), self.assertRaisesRegex(ValueError, "selected profile differs"):
            integration.merge(self.directory, self.task, state)
        self.assertFalse(any(method == "PUT" for _, method in self.github_requests))
        self.project.ledger.assert_not_called()

    def test_matching_and_missing_ledger_allow_normal_publish_and_merge(self):
        for existing in (True, False):
            with self.subTest(existing=existing):
                state = copy.deepcopy(self.state)
                directory = self.directory if existing else self.root / "no-ledger-attempt"
                directory.mkdir(exist_ok=True)
                accounting = self.root if existing else self.root / "absent-accounting"
                self.merged = False
                with patch.object(integration, "common_state", return_value=accounting), patch.object(integration, "candidate", return_value=self.root), patch.object(integration, "git", side_effect=self.git), patch.object(integration, "github", side_effect=self.github):
                    published = integration.publish(directory, self.task, {}, state)
                    self.assertEqual(published["status"], "published")
                    result = integration.merge(directory, self.task, published)
                self.assertEqual(result["status"], "merged")
                self.assertTrue(json.loads((directory / "integration.json").read_text())["merged_at"])
                if existing:
                    self.assertEqual(self.ledger.rows()[0]["state"], "merged")
                else:
                    # Operational action receipts are separate from financial
                    # accounting; a merge must still never initialize a budget.
                    self.assertFalse((accounting / "control.sqlite3").exists())
                    self.assertTrue((accounting / "operations.sqlite3").is_file())
        self.assertEqual(self.project.ledger.call_count, 1)


if __name__ == "__main__":
    unittest.main()
