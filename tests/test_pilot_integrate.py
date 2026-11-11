import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from swarm_control import pilot_integrate as integration
from swarm_control.pilot_state import Ledger


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        repo_patch = patch.object(integration, "REPO", "example/project")
        repo_patch.start()
        self.addCleanup(repo_patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = b"diff --git a/src/item.ts b/src/item.ts\n"
        self.digest = hashlib.sha256(self.patch).hexdigest()
        self.task = {"id": "T01", "title": "Useful validation", "ownership": ["src"],
                     "acceptance": ["Reject invalid inputs"], "issue": 12}
        self.manifest = {"repository": f"https://github.com/{integration.REPO}.git",
                         "base_sha": "a" * 40, "task": self.task}
        self.report = {"task_id": "T01", "base_sha": "a" * 40, "status": "validated",
                       "agent_exit": 0, "check_exit": 0, "checked_patch_sha256": self.digest,
                       "patch_sha256": self.digest, "changes": {"head_unchanged": True,
                       "ownership_violations": [], "prohibited_files": []}}
        self.write()

    def write(self):
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))
        (self.root / "report.json").write_text(json.dumps(self.report))
        (self.root / "changes.patch").write_bytes(self.patch)

    def test_digest_requires_both_checked_and_downloaded_versions(self):
        self.assertEqual(integration.artifacts(self.root)[2], self.digest)
        for field in ("checked_patch_sha256", "patch_sha256"):
            with self.subTest(field=field):
                self.report[field] = "b" * 64
                self.write()
                with self.assertRaisesRegex(ValueError, "digest mismatch"):
                    integration.artifacts(self.root)
                self.report[field] = self.digest
        self.write()
        (self.root / "changes.patch").write_bytes(self.patch + b"tampered")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            integration.artifacts(self.root)

    def test_failed_checks_commits_and_bad_ownership_block_admission(self):
        original = copy.deepcopy(self.report)
        for key, value in (("status", "needs_repair"), ("agent_exit", 1),
                           ("check_exit", False), ("recovery_errors", ["missing"]),
                           ("changes", {"head_unchanged": False, "ownership_violations": [], "prohibited_files": []}),
                           ("changes", {"head_unchanged": True, "ownership_violations": ["outside"], "prohibited_files": []})):
            with self.subTest(key=key, value=value):
                self.report = {**original, key: value}
                self.write()
                with self.assertRaisesRegex(ValueError, "did not validate"):
                    integration.artifacts(self.root)

    def test_manifest_cannot_escape_ownership_or_hide_credentials(self):
        for ownership in (["../src"], [".git"], []):
            self.task["ownership"] = ownership
            self.write()
            with self.assertRaisesRegex(ValueError, "invalid ownership"):
                integration.artifacts(self.root)
        self.task["ownership"] = ["src"]
        self.task["title"] = "sk-" + "a" * 32
        self.write()
        with self.assertRaisesRegex(ValueError, "credential content"):
            integration.artifacts(self.root)

    def test_review_acknowledgement_is_required_and_bound_to_integration(self):
        integration.save(self.root / "integration.json", {"patch_sha256": self.digest})
        for acknowledged in (None, "b" * 64):
            with self.assertRaisesRegex(ValueError, "explicit"):
                integration.load_review(self.root, self.digest, acknowledged)
        self.assertEqual(integration.load_review(self.root, self.digest, self.digest)["patch_sha256"], self.digest)
        integration.save(self.root / "integration.json", {"patch_sha256": "c" * 64})
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            integration.load_review(self.root, self.digest, self.digest)


class LocalCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Integration test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "src").mkdir()
        (self.root / "src/item.ts").write_text("export const value = 1;\n")
        (self.root / "outside.txt").write_text("outside\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=self.root, stderr=subprocess.DEVNULL).decode().strip()

    def test_deletions_are_checked_and_untracked_outside_files_rejected(self):
        (self.root / "src/item.ts").unlink()
        self.assertEqual(integration.audit(self.root, self.base, ["src"]), ["src/item.ts"])
        (self.root / "outside-new.txt").write_text("untracked")
        with self.assertRaisesRegex(ValueError, "ownership violation"):
            integration.audit(self.root, self.base, ["src"])

    def test_symlinks_and_credential_files_or_content_are_rejected(self):
        target = self.root / "src/link"
        target.symlink_to("../../outside")
        with self.assertRaisesRegex(ValueError, "symlink prohibited"):
            integration.audit(self.root, self.base, ["src"])
        target.unlink()
        for name, content in (("auth.json", "{}"), ("service-account.json", "{}"),
                              (".env.local", "NAME=value"), ("key.txt", "sk-" + "x" * 32)):
            with self.subTest(name=name):
                target = self.root / "src" / name
                target.write_text(content)
                with self.assertRaisesRegex(ValueError, "credential"):
                    integration.audit(self.root, self.base, ["src"])
                target.unlink()

    def test_modifications_after_validation_cannot_be_published(self):
        target = self.root / "src/item.ts"
        target.write_text("export const value = 2;\n")
        self.git("add", ".")
        state = {"worktree": str(self.root), "branch": "main", "tested_base_sha": self.base,
                 "tested_tree": self.git("write-tree")}
        task = {"ownership": ["src"]}
        self.assertEqual(integration.candidate(state, task), self.root)
        target.write_text("export const value = 3;\n")
        with self.assertRaisesRegex(ValueError, "unstaged"):
            integration.candidate(state, task)
        self.git("add", ".")
        with self.assertRaisesRegex(ValueError, "tree changed"):
            integration.candidate(state, task)


class MergeGateTests(unittest.TestCase):
    def setUp(self):
        self.head = "b" * 40
        self.base = "a" * 40
        self.checks = {"total_count": 1, "check_runs": [{"name": "checks", "app": {"slug": "github-actions"},
                       "head_sha": self.head, "status": "completed", "conclusion": "success"}]}
        self.state = {"status": "published", "task_id": "T01", "batch_id": "batch", "branch": "swarm/t01",
                      "head_sha": self.head, "tested_base_sha": self.base, "pr_number": 55}
        self.pr = {"state": "open", "draft": False, "base": {"ref": "main"},
                   "head": {"sha": self.head, "ref": "swarm/t01"}}

    def test_missing_stale_failed_pending_and_incomplete_checks_are_rejected(self):
        integration.green_checks(self.checks, self.head)
        for mutation in ({"head_sha": self.base}, {"conclusion": "failure"},
                         {"status": "in_progress"}, {"name": "unrelated"}, {"conclusion": "skipped"}):
            payload = copy.deepcopy(self.checks)
            payload["check_runs"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                integration.green_checks(payload, self.head)
        for payload in ({"check_runs": []}, {**self.checks, "total_count": 101}):
            with self.assertRaises(ValueError):
                integration.green_checks(payload, self.head)

    def test_changed_main_or_pr_head_prevents_remote_merge(self):
        for changed in ("main", "head"):
            calls = []
            def github(path, calls=calls, changed=changed, **kwargs):
                calls.append((path, kwargs))
                if path.startswith("pulls/"):
                    pr = copy.deepcopy(self.pr)
                    if changed == "head":
                        pr["head"]["sha"] = "c" * 40
                    return pr
                if "check-runs" in path:
                    return self.checks
                return {"object": {"sha": "c" * 40}}
            with patch.object(integration, "candidate"), patch.object(integration, "github", side_effect=github), self.assertRaises(ValueError):
                integration.merge(Path("unused"), {"id": "T01"}, copy.deepcopy(self.state))
            self.assertFalse(any(kwargs.get("method") == "PUT" for _, kwargs in calls))

    def test_merge_is_sha_guarded_and_reconciles_ledger_after_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "batch/T01"
            directory.mkdir(parents=True)
            ledger = Ledger(Path(temporary) / "control.sqlite3")
            ledger.claim([{"id": "T01"}], "batch", self.base)
            requests = []
            merged = False
            def github(path, **kwargs):
                nonlocal merged
                requests.append((path, kwargs))
                if path.endswith("/merge"):
                    self.assertEqual(kwargs["body"]["sha"], self.head)
                    merged = True
                    return {"merged": True, "sha": "c" * 40}
                if path.startswith("pulls/"):
                    return {**self.pr, "merged": merged, "merge_commit_sha": "c" * 40,
                            "merged_at": "2026-11-10T20:00:00Z"}
                if "check-runs" in path:
                    return self.checks
                return {"object": {"sha": self.base}}
            with patch.object(integration, "candidate"), patch.object(integration, "github", side_effect=github), patch.object(integration, "common_state", return_value=Path(temporary)), patch.object(integration, "PROJECT", SimpleNamespace(ledger=Ledger, required_checks=("checks",), model_limit_cents=1800, infra_limit_cents=700)):
                result = integration.merge(directory, {"id": "T01", "title": "Useful work"}, self.state)
                self.assertEqual(result["status"], "merged")
                self.assertEqual(ledger.rows()[0]["state"], "merged")
                integration.merge(directory, {"id": "T01"}, result)
            self.assertEqual(sum(kwargs.get("method") == "PUT" for _, kwargs in requests), 1)

    def test_build_commands_receive_no_model_or_github_keys(self):
        environment = {"PATH": os.environ["PATH"], "OPENAI_API_KEY": "model", "CODEX_API_KEY": "model",
                       "OPENAI_ADMIN_KEY": "billing-admin", "OPENAI_ADMIN_API_KEY": "billing-admin-alias",
                       "OTHER_API_KEY": "other", "GH_TOKEN": "github", "GITHUB_TOKEN": "github",
                       "MODAL_TOKEN_SECRET": "modal"}
        with patch.dict(os.environ, environment, clear=True), patch.object(integration.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"{}", b"")) as run:
            integration.command(["npm", "run", "check"], Path("."))
            self.assertEqual(run.call_args.kwargs["env"], {"PATH": environment["PATH"]})
            integration.command(["gh", "api"], Path("."))
            self.assertEqual(run.call_args.kwargs["env"]["GH_TOKEN"], "github")
            self.assertNotIn("OPENAI_API_KEY", run.call_args.kwargs["env"])
            self.assertNotIn("OPENAI_ADMIN_KEY", run.call_args.kwargs["env"])
            self.assertNotIn("OPENAI_ADMIN_API_KEY", run.call_args.kwargs["env"])

    def test_check_pagination_includes_page_two_failures_and_rejects_inconsistent_pages(self):
        first = [{**self.checks["check_runs"][0], "id": index} for index in range(100)]
        last = {**self.checks["check_runs"][0], "id": 100, "conclusion": "failure"}
        with patch.object(integration, "github", side_effect=[
            {"total_count": 101, "check_runs": first}, {"total_count": 101, "check_runs": [last]}
        ]) as query:
            result = integration.all_checks(self.head)
        self.assertIn("page=2", query.call_args.args[0])
        with self.assertRaisesRegex(ValueError, "not all green"):
            integration.green_checks(result, self.head)
        for second in ({"total_count": 102, "check_runs": [last]},
                       {"total_count": 101, "check_runs": [first[0]]},
                       {"total_count": 101, "check_runs": []}):
            with self.subTest(second=second), patch.object(integration, "github", side_effect=[
                {"total_count": 101, "check_runs": first}, second
            ]), self.assertRaises(ValueError):
                integration.all_checks(self.head)


class CollisionIntegrationTests(unittest.TestCase):
    """Exercise actual Git patch behavior rather than mocking conflict resolution."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "target"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Integration test")
        self.git("config", "user.email", "test@example.invalid")
        remote = self.directory / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
        self.git("remote", "add", "origin", str(remote))
        self.original = [f"line {index}\n" for index in range(40)]
        self.source = self.root / "src.py"
        self.source.write_text("".join(self.original))
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        worker = self.original.copy()
        worker[10] = "worker change\n"
        self.source.write_text("".join(worker))
        self.patch = subprocess.check_output(["git", "diff", "--binary", "--full-index"], cwd=self.root)
        self.digest = hashlib.sha256(self.patch).hexdigest()
        self.git("restore", "src.py")
        self.artifacts = self.directory / "batch" / "T01"
        self.artifacts.mkdir(parents=True)
        (self.artifacts / "changes.patch").write_bytes(self.patch)
        (self.artifacts / "manifest.json").write_text(json.dumps({"allow_overlap": True}))
        self.task = {"id": "T01", "ownership": ["src.py"]}
        self.project = SimpleNamespace(install=((sys.executable, "-c", "pass"),),
                                       checks=((sys.executable, "-c", "pass"),))
        for name, value in (("ROOT", self.root), ("PROJECT", self.project)):
            context = patch.object(integration, name, value)
            context.start()
            self.addCleanup(context.stop)

    def git(self, *arguments, cwd=None):
        return subprocess.check_output(["git", *arguments], cwd=cwd or self.root,
                                       stderr=subprocess.DEVNULL).decode().strip()

    def upstream(self, index):
        lines = self.original.copy()
        lines[index] = "upstream change\n"
        self.source.write_text("".join(lines))
        self.git("add", ".")
        self.git("commit", "-m", "upstream change")
        self.git("push", "origin", "main")

    def prepare(self):
        return integration.prepare(self.artifacts, self.task, {"base_sha": self.base}, self.digest)

    def test_independent_same_file_edits_use_three_way_and_validate_combined_tree(self):
        self.upstream(8)  # Changes patch context, so strict apply fails; edits still merge cleanly.
        check = "from pathlib import Path; s=Path('src.py').read_text(); assert 'worker change' in s and 'upstream change' in s"
        self.project.checks = ((sys.executable, "-c", check),)
        state = self.prepare()
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(state["application_method"], "three_way")
        self.assertNotEqual(state["worker_base_sha"], state["tested_base_sha"])
        self.assertEqual(state["patch_sha256"], self.digest)
        applied = (self.artifacts / "integration.patch").read_bytes()
        self.assertEqual(state["applied_patch_sha256"], hashlib.sha256(applied).hexdigest())
        self.assertTrue((self.artifacts / "integration-checks-1.log").exists())
        self.assertEqual(integration.candidate(state, self.task), Path(state["worktree"]))
        with self.assertRaisesRegex(ValueError, "reviewed-applied"):
            integration.load_review(self.artifacts, self.digest, self.digest)
        integration.load_review(self.artifacts, self.digest, self.digest, state["applied_patch_sha256"])

    def test_true_conflict_is_durable_and_partial_application_is_cleaned(self):
        self.upstream(10)
        with self.assertRaisesRegex(ValueError, "needs repair"):
            self.prepare()
        state = json.loads((self.artifacts / "integration.json").read_text())
        self.assertEqual(state["status"], "needs_repair")
        self.assertEqual(state["conflicting_files"], ["src.py"])
        self.assertTrue(state["worktree_cleaned"])
        root = Path(state["worktree"])
        self.assertEqual(self.git("status", "--porcelain", cwd=root), "")
        self.assertNotIn("<<<<<<<", (root / "src.py").read_text())
        self.assertEqual((self.artifacts / "changes.patch").read_bytes(), self.patch)
        self.assertTrue((self.artifacts / "integration-apply.log").exists())

    def test_already_applied_worker_patch_leaves_durable_repair_receipt(self):
        lines = self.original.copy()
        lines[10] = "worker change\n"
        self.source.write_text("".join(lines))
        self.git("add", ".")
        self.git("commit", "-m", "already integrated")
        self.git("push", "origin", "main")
        with self.assertRaisesRegex(ValueError, "empty candidate"):
            self.prepare()
        state = json.loads((self.artifacts / "integration.json").read_text())
        self.assertEqual(state["status"], "needs_repair")
        self.assertEqual(state["repair_reason"], "candidate_validation_failed")

    def test_overlap_opt_in_is_required_and_failed_combined_checks_block_publish(self):
        self.upstream(8)
        (self.artifacts / "manifest.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.prepare()
        state_path = self.artifacts / "integration.json"
        self.assertEqual(json.loads(state_path.read_text())["repair_reason"], "patch_does_not_apply")
        # A separate attempt preserves the first receipt and original worker patch.
        self.artifacts = self.directory / "retry" / "T01"
        self.artifacts.mkdir(parents=True)
        (self.artifacts / "changes.patch").write_bytes(self.patch)
        (self.artifacts / "manifest.json").write_text(json.dumps({"allow_overlap": True}))
        self.project.checks = ((sys.executable, "-c", "raise SystemExit(1)"),)
        with self.assertRaises(ValueError):
            self.prepare()
        state = json.loads((self.artifacts / "integration.json").read_text())
        self.assertEqual(state["status"], "needs_repair")
        self.assertEqual(state["repair_reason"], "candidate_validation_failed")

    def test_integration_lock_excludes_second_client_and_releases_after_error(self):
        state = self.directory / "state"
        with self.assertRaisesRegex(RuntimeError, "failure"), integration.integration_lock(state):
            with self.assertRaisesRegex(ValueError, "another integrator"), integration.integration_lock(state):
                self.fail("second integrator acquired the lock")
            raise RuntimeError("failure")
        with integration.integration_lock(state):
            pass


if __name__ == "__main__":
    unittest.main()
