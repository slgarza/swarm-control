"""Real local Git preparation lanes, durable reservations and unchanged merge gates."""

import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from swarm_control import integration_pipeline as pipeline
from swarm_control import pilot_integrate as integration
from swarm_control.pilot_state import common_state
from swarm_control.project import load_project
from tests.support import make_project


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.project = make_project(self.directory / "target")
        self.root = self.project.root
        for name in ("ROOT", "REPO", "PROJECT", "BASE_BRANCH"):
            context = patch.object(integration, name, getattr(integration, name))
            context.start()
            self.addCleanup(context.stop)
        (self.root / ".gitignore").write_text(".swarm/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Ignore private runtime")
        self.remote = self.directory / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.git("push", str(self.remote), "main")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        original_command = integration.command

        def local_transport(argv, cwd, **kwargs):
            # Only substitute transport; all Git operations and validation
            # processes are real. Public origin metadata remains verifiable.
            if argv[:3] == ["git", "fetch", "origin"]:
                argv = [*argv[:2], str(self.remote), *argv[3:]]
            return original_command(argv, cwd, **kwargs)

        context = patch.object(integration, "command", side_effect=local_transport)
        context.start()
        self.addCleanup(context.stop)
        self.manifest = {"schema_version": 1, "items": []}

    def git(self, *arguments, cwd=None):
        return subprocess.check_output(["git", *arguments], cwd=cwd or self.root,
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def checks(self, script):
        old = self.project.path.read_text()
        lines = old.splitlines()
        lines = ["checks = " + json.dumps([[sys.executable, "-c", script]])
                 if line.startswith("checks = ") else line for line in lines]
        self.project.path.write_text("\n".join(lines) + "\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture checks")
        self.git("push", str(self.remote), "main")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.project = load_project(self.project.path)

    def candidate(self, task_id, *, dependencies=None, recovery=False, task_extra=None):
        name = f"src/{task_id.lower()}.txt"
        target = self.root / name
        target.write_text(f"{task_id} useful change\n")
        self.git("add", name)
        raw = subprocess.check_output(["git", "diff", "--cached", "--binary", "--full-index"], cwd=self.root)
        self.git("reset", "-q", "HEAD", "--", name)
        target.unlink()
        directory = self.directory / "batch" / task_id
        directory.mkdir(parents=True)
        digest = hashlib.sha256(raw).hexdigest()
        task = {"id": task_id, "title": f"Implement {task_id}", "prompt": "Implement the useful change",
                "ownership": [name], "acceptance": ["Expected behavior works"], "issue": len(self.manifest["items"]) + 1,
                "dependsOn": dependencies or [], **(task_extra or {})}
        manifest = {"repository": self.project.clone_url, "base_sha": self.base, "task": task}
        report = {"task_id": task_id, "base_sha": self.base, "status": "validated",
                  "agent_exit": 0, "check_exit": 0, "checked_patch_sha256": digest,
                  "patch_sha256": digest, "changes": {"head_unchanged": True,
                  "ownership_violations": [], "prohibited_files": []}}
        if recovery:
            report.update(status="needs_repair", agent_exit=-1)
            report["recovered_candidate"] = {"status": "candidate", "original_worker_status": "needs_repair",
                "original_agent_exit": -1, "patch_sha256": digest, "base_sha": self.base,
                "head_unchanged": True, "scope_clean": True, "baseline_checks_passed": True,
                "configured_acceptance_passed": None, "requires_independent_validation": True,
                "original_outcome_preserved": True, "authorizes_publish": False}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        (directory / "report.json").write_text(json.dumps(report))
        (directory / "changes.patch").write_bytes(raw)
        item = {"artifacts": str(directory), "reviewed_patch_sha": digest}
        if recovery:
            item["recovery_reviewed"] = True
        self.manifest["items"].append(item)
        return directory, digest

    def run_pipeline(self, **options):
        return pipeline.run_pipeline(self.project, self.manifest, **options)

    def test_preview_reads_verified_artifacts_without_runtime_writes_or_network(self):
        self.candidate("T01")
        before = self.git("status", "--porcelain")
        with patch.object(integration, "github", side_effect=AssertionError("network request")):
            result = self.run_pipeline()
        self.assertFalse(result["execution_requested"])
        self.assertEqual(result["items"][0]["state"], "eligible")
        self.assertFalse(common_state(self.root).exists())
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertFalse(result["automatic_ci_retry"])
        self.assertEqual(result["accepted_deliveries"], 0)

    def test_independent_real_check_processes_overlap_and_global_lock_is_short(self):
        self.checks("import time; time.sleep(0.12)")
        self.candidate("T01")
        self.candidate("T02")
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        active = peak = 0
        original = integration.command

        def observed(argv, cwd, **kwargs):
            nonlocal active, peak
            is_check = "time.sleep(0.12)" in " ".join(argv)
            if not is_check:
                return original(argv, cwd, **kwargs)
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    entered.set()
                    release.set()
            self.assertTrue(release.wait(5), "second lane never entered its checks")
            # During checks, another client can acquire the shared Git lock.
            with integration.integration_lock(common_state(self.root), wait=True):
                pass
            try:
                return original(argv, cwd, **kwargs)
            finally:
                with lock:
                    active -= 1

        with patch.object(integration, "command", side_effect=observed):
            result = self.run_pipeline(execute=True, lanes=2)
        self.assertTrue(entered.is_set())
        self.assertEqual(peak, 2)
        self.assertEqual(result["prepared_count"], 2)
        self.assertTrue(all(row["state"] == "prepared" for row in result["items"]))
        self.assertEqual(result["remote_mutations_performed"], 0)

    def test_waiting_ci_does_not_block_independent_preparation(self):
        waiting, digest = self.candidate("T01")
        self.candidate("T02")
        self.candidate("T03", dependencies=["T01"])
        (waiting / "integration.json").write_text(json.dumps({"task_id": "T01", "patch_sha256": digest,
            "worker_base_sha": self.base, "tested_base_sha": self.base, "status": "published",
            "published_at": "2026-01-01T00:00:00+00:00"}))
        result = self.run_pipeline(execute=True)
        indexed = {row["task_id"]: row for row in result["items"]}
        self.assertEqual(indexed["T02"]["state"], "prepared")
        self.assertEqual(indexed["T01"]["state"], "held")
        self.assertEqual(indexed["T03"]["reason"], "dependency_not_qualified_merged")
        self.assertEqual(result["prepared_count"], 1)

    def test_same_candidate_race_and_restart_do_not_repeat_local_commands(self):
        directory, digest = self.candidate("T01")
        integration.configure(self.project)
        parsed = pipeline._load_candidates(self.manifest, self.directory)[0]
        journal = pipeline.PreparationJournal(self.directory / "journal.sqlite3")
        with ThreadPoolExecutor(max_workers=2) as executor:
            reservations = list(executor.map(lambda _: journal.reserve(
                self.project.repository, parsed, self.base, "synthetic"), range(2)))
        self.assertEqual(sum(identity is not None for identity, _ in reservations), 1)
        result = self.run_pipeline(execute=True)
        self.assertEqual(result["prepared_count"], 1)
        before = (directory / "integration.json").read_bytes()
        with patch.object(integration, "prepare", side_effect=AssertionError("duplicate preparation")):
            repeat = self.run_pipeline(execute=True)
        self.assertEqual(repeat["prepared_count"], 0)
        self.assertEqual((directory / "integration.json").read_bytes(), before)
        self.assertEqual(digest, repeat["items"][0]["patch_sha256"])

    def test_interrupted_reservation_stays_held_after_restart(self):
        self.candidate("T01")
        integration.configure(self.project)
        parsed = pipeline._load_candidates(self.manifest, self.directory)[0]
        journal = pipeline.PreparationJournal(common_state(self.root) / "integration-pipeline.sqlite3")
        journal.reserve(self.project.repository, parsed, self.base, "old-interrupted-run")
        with patch.object(integration, "prepare", side_effect=AssertionError("uncertain attempt retried")):
            result = self.run_pipeline(execute=True, max_pending=4)
        self.assertEqual(result["items"][0]["reason"], "prior_preparation_outcome_unknown")

    def test_failed_checks_preserve_attempt_and_other_lane_finishes(self):
        self.checks("from pathlib import Path; raise SystemExit(1 if Path('src/t01.txt').exists() else 0)")
        first, _ = self.candidate("T01")
        self.candidate("T02")
        result = self.run_pipeline(execute=True)
        self.assertEqual(result["prepared_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(json.loads((first / "integration.json").read_text())["status"], "needs_repair")
        files = list(common_state(self.root).glob("integration-pipelines/*/T01-failure.txt"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_reviewed_recovery_keeps_original_failure_and_runs_current_checks(self):
        directory, digest = self.candidate("T01", recovery=True)
        original = (directory / "report.json").read_bytes()
        result = self.run_pipeline(execute=True)
        self.assertEqual(result["prepared_count"], 1)
        self.assertEqual(result["items"][0]["attribution"], "reviewed_worker_recovery")
        self.assertEqual((directory / "report.json").read_bytes(), original)
        state = json.loads((directory / "integration.json").read_text())
        self.assertEqual(state["original_worker_status"], "needs_repair")
        self.assertEqual(state["original_agent_exit"], -1)
        self.assertEqual(state["patch_sha256"], digest)
        self.assertEqual(result["accepted_deliveries"], 0)

    def test_recovery_needs_explicit_review_and_cannot_hide_scope_or_changed_hash(self):
        directory, digest = self.candidate("T01", recovery=True)
        original = copy.deepcopy(self.manifest)
        self.manifest["items"][0].pop("recovery_reviewed")
        with self.assertRaisesRegex(ValueError, "did not validate"):
            self.run_pipeline()
        self.manifest = copy.deepcopy(original)
        self.manifest["items"][0]["reviewed_patch_sha"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "Reviewed patch hash"):
            self.run_pipeline()
        self.manifest = original
        report = json.loads((directory / "report.json").read_text())
        report["changes"]["ownership_violations"] = ["outside.txt"]
        (directory / "report.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "did not validate"):
            self.run_pipeline()
        self.assertEqual(digest, self.manifest["items"][0]["reviewed_patch_sha"])

    def test_new_main_does_not_rewrite_existing_candidate_or_authorize_publish(self):
        directory, digest = self.candidate("T01")
        self.run_pipeline(execute=True)
        original = (directory / "integration.json").read_bytes()
        (self.root / "README.md").write_text("New main\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Advance main")
        self.git("push", str(self.remote), "main")
        result = self.run_pipeline(execute=True)
        self.assertEqual(result["prepared_count"], 0)
        self.assertEqual((directory / "integration.json").read_bytes(), original)
        state = json.loads(original)
        integration.configure(self.project)
        requests = []

        def github(path, **kwargs):
            requests.append((path, kwargs))
            return {"object": {"sha": self.git("rev-parse", "HEAD")}}

        with patch.object(integration, "github", side_effect=github), self.assertRaisesRegex(ValueError, "main changed"):
            task, report, _ = integration.artifacts(directory)
            integration.publish(directory, task, report, state)
        self.assertFalse(any(kwargs.get("method") in {"POST", "PUT"} for _, kwargs in requests))
        self.assertEqual(digest, state["patch_sha256"])

    def test_unreviewed_merged_claim_cannot_release_dependencies(self):
        directory, digest = self.candidate("T01")
        self.candidate("T02", dependencies=["T01"])
        (directory / "integration.json").write_text(json.dumps({"task_id": "T01", "patch_sha256": digest,
            "worker_base_sha": self.base, "tested_base_sha": self.base, "status": "merged",
            "merge_commit_sha": self.base, "head_sha": "a" * 40, "pr_number": 1}))
        with patch.object(integration, "github", side_effect=AssertionError("offline preview queried GitHub")):
            result = self.run_pipeline()
        self.assertEqual(result["items"][1]["reason"], "dependency_not_qualified_merged")
        with patch.object(integration, "github", return_value={"merged": False}):
            result = self.run_pipeline(online=True)
        self.assertEqual(result["items"][1]["reason"], "dependency_not_qualified_merged")

    def test_pending_bound_survives_restart_and_does_not_reduce_worker_capacity(self):
        for index in range(1, 7):
            self.candidate(f"T{index:02}")
        first = self.run_pipeline(execute=True, lanes=2, max_pending=3)
        self.assertEqual(first["prepared_count"], 3)
        self.assertEqual(first["attempts_admitted"], 3)
        second = self.run_pipeline(execute=True, lanes=2, max_pending=3)
        self.assertEqual(second["prepared_count"], 0)
        self.assertEqual(second["pending_candidates_before"], 3)
        self.assertTrue(all(row["reason"] == "pending_candidate_bound_reached"
                            for row in first["items"][3:]))

    def test_one_hundred_candidate_preview_is_valid_and_bounded(self):
        for index in range(100):
            self.candidate(f"T{index:03}")
        result = self.run_pipeline(lanes=8, max_pending=100)
        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["lanes"], 8)
        self.assertEqual(sum(row["state"] == "eligible" for row in result["items"]), 8)
        self.assertFalse(common_state(self.root).exists())
        executed = self.run_pipeline(execute=True, lanes=8, max_pending=100)
        self.assertEqual(executed["prepared_count"], 100)
        self.assertEqual(executed["attempts_admitted"], 100)
        self.assertEqual(executed["accepted_deliveries"], 0)
        roots = {json.loads((Path(item["artifacts"]) / "integration.json").read_text())["worktree"]
                 for item in self.manifest["items"]}
        self.assertEqual(len(roots), 100)

    def test_baseline_green_does_not_override_frozen_behavior_acceptance(self):
        directory, _ = self.candidate("T01", task_extra={
            "acceptance_checks": [[sys.executable, "-c", "raise SystemExit(1)"]]})
        result = self.run_pipeline(execute=True)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["prepared_count"], 0)
        self.assertEqual(json.loads((directory / "integration.json").read_text())["status"], "needs_repair")
        self.assertTrue((directory / "integration-acceptance-1.log").exists())

    def test_changed_protected_acceptance_is_rejected_before_local_checks(self):
        protected = self.root / "acceptance.py"
        protected.write_text("raise SystemExit(0)\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Frozen independent fixture")
        self.git("push", str(self.remote), "main")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        directory, _ = self.candidate("T01", task_extra={"ownership": ["src/t01.txt", "acceptance.py"],
            "acceptance_checks": [[sys.executable, "acceptance.py"]], "acceptance_files": ["acceptance.py"]})
        protected.write_text("print('weakened fixture')\n")
        self.git("add", "acceptance.py")
        extra = subprocess.check_output(["git", "diff", "--cached", "--binary", "--full-index"], cwd=self.root)
        self.git("restore", "--staged", "--worktree", "acceptance.py")
        path = directory / "changes.patch"
        path.write_bytes(path.read_bytes() + extra)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        report = json.loads((directory / "report.json").read_text())
        report.update(patch_sha256=digest, checked_patch_sha256=digest)
        (directory / "report.json").write_text(json.dumps(report))
        self.manifest["items"][0]["reviewed_patch_sha"] = digest
        result = self.run_pipeline(execute=True)
        self.assertEqual(result["failed_count"], 1)
        self.assertFalse((directory / "integration-install-1.log").exists())
        self.assertEqual(json.loads((directory / "integration.json").read_text())["status"], "needs_repair")

    def test_command_deadline_stops_descendant_processes_and_preserves_failure(self):
        self.checks("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); time.sleep(30)")
        directory, _ = self.candidate("T01")
        started = time.monotonic()
        result = self.run_pipeline(execute=True, check_timeout=1)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(json.loads((directory / "integration.json").read_text())["status"], "needs_repair")

    def test_changed_input_after_durable_reservation_never_executes(self):
        directory, _ = self.candidate("T01")
        original_reserve = pipeline.PreparationJournal.reserve

        def changed(journal, *arguments):
            result = original_reserve(journal, *arguments)
            path = directory / "report.json"
            path.write_bytes(path.read_bytes() + b"\n")
            return result

        with (patch.object(pipeline.PreparationJournal, "reserve", new=changed),
              patch.object(integration, "prepare", side_effect=AssertionError("changed source executed"))):
            result = self.run_pipeline(execute=True)
        self.assertEqual(result["failed_count"], 1)
        self.assertFalse((directory / "integration.json").exists())

    def test_prepared_source_drift_blocks_review_publish_merge_and_queue_reuse(self):
        directory, digest = self.candidate("T01")
        self.run_pipeline(execute=True)
        self.candidate("T02")
        integration.configure(self.project)
        state = json.loads((directory / "integration.json").read_text())
        original = {name: (directory / name).read_bytes() for name in ("manifest.json", "report.json")}
        project_source = self.project.path.read_bytes()
        changes = ("acceptance", "dependency", "report", "project_validation")
        for change in changes:
            with self.subTest(change=change):
                manifest = json.loads(original["manifest.json"])
                if change == "acceptance":
                    manifest["task"]["acceptance_checks"] = [[sys.executable, "-c", "raise SystemExit(99)"]]
                if change == "dependency":
                    manifest["task"]["dependsOn"] = ["UNMERGED"]
                (directory / "manifest.json").write_text(json.dumps(manifest))
                if change == "report":
                    report = json.loads(original["report.json"])
                    report["wall_seconds"] = 1
                    (directory / "report.json").write_text(json.dumps(report))
                if change == "project_validation":
                    self.project.path.write_bytes(project_source + b"\n# changed selected validation profile\n")
                task, report, _ = integration.artifacts(directory)
                with patch.object(integration, "github") as github, patch.object(integration, "candidate") as candidate:
                    with self.assertRaisesRegex(ValueError, "source binding changed"):
                        integration.load_review(directory, digest, digest)
                    with self.assertRaisesRegex(ValueError, "source binding changed"):
                        integration.publish(directory, task, report, copy.deepcopy(state))
                    with self.assertRaisesRegex(ValueError, "source binding changed"):
                        integration.merge(directory, task, {**state, "status": "published"})
                    github.assert_not_called()
                    candidate.assert_not_called()
                result = self.run_pipeline()
                self.assertEqual(result["items"][0]["state"], "held")
                self.assertIn("integration_source_binding_invalid", result["items"][0]["reason"])
                self.assertEqual(result["items"][1]["state"], "eligible")
                for name, content in original.items():
                    (directory / name).write_bytes(content)
                self.project.path.write_bytes(project_source)
        self.assertEqual(integration.load_review(directory, digest, digest)["status"], "prepared")

    def test_legacy_or_missing_accepted_check_receipt_requires_explicit_new_attempt(self):
        directory, digest = self.candidate("T01")
        self.run_pipeline(execute=True)
        integration.configure(self.project)
        original = json.loads((directory / "integration.json").read_text())
        for missing in ("source_bindings", "baseline_checks_exit", "checks_finished_at", "acceptance_configured"):
            state = copy.deepcopy(original)
            state.pop(missing)
            (directory / "integration.json").write_text(json.dumps(state))
            with self.subTest(missing=missing), patch.object(integration, "github") as github:
                with self.assertRaisesRegex(ValueError, "source binding missing|accepted integration checks are missing"):
                    integration.load_review(directory, digest, digest)
                task, report, _ = integration.artifacts(directory)
                with self.assertRaises(ValueError):
                    integration.publish(directory, task, report, state)
                github.assert_not_called()
            result = self.run_pipeline()
            self.assertEqual(result["items"][0]["state"], "held")
            self.assertEqual(json.loads((directory / "integration.json").read_text()), state)

    def test_fresh_verified_merge_and_combined_ci_release_dependent_preparation(self):
        directory, _ = self.candidate("T01", task_extra={"produces": ["contract:session-v1"]})
        self.candidate("T02", task_extra={"consumes": ["contract:session-v1"]})
        first = self.run_pipeline(execute=True)
        self.assertEqual(first["prepared_count"], 1)
        self.assertEqual(first["items"][1]["reason"], "dependency_not_qualified_merged")
        self.assertIn({"producer": "T01", "consumer": "T02", "kind": "artifact", "artifact": "contract:session-v1"},
                      first["dependency_analysis"]["edges"])
        record = json.loads((directory / "integration.json").read_text())
        self.git("commit", "-qm", "Reviewed T01", cwd=Path(record["worktree"]))
        head = self.git("rev-parse", "HEAD", cwd=Path(record["worktree"]))
        self.git("merge", "--squash", record["branch"])
        self.git("commit", "-qm", "Merge reviewed T01")
        merged = self.git("rev-parse", "HEAD")
        self.git("push", str(self.remote), "main")
        record.update(status="merged", head_sha=head, green_head_sha=head, merge_commit_sha=merged, pr_number=1)
        (directory / "integration.json").write_text(json.dumps(record))
        self.git("update-ref", "refs/remotes/origin/main", merged)
        requested = []

        def github(path, **kwargs):
            requested.append(path)
            self.assertFalse(kwargs)
            if path == "pulls/1":
                return {"merged": True, "merge_commit_sha": merged, "head": {"sha": head}, "base": {"ref": "main"}}
            if path.startswith("commits/"):
                observed = path.split("/")[1]
                return {"total_count": 1, "check_runs": [{"id": 1, "name": "checks",
                    "app": {"slug": "github-actions"}, "head_sha": observed,
                    "status": "completed", "conclusion": "success"}]}
            raise AssertionError(path)

        with patch.object(integration, "github", side_effect=github):
            bounded = self.run_pipeline(online=True, github_get_limit=1)
            self.assertEqual(bounded["read_only_github"]["requests"], 1)
            self.assertTrue(bounded["read_only_github"]["limit_reached"])
            self.assertEqual(bounded["items"][1]["reason"], "dependency_not_qualified_merged")
            result = self.run_pipeline(execute=True, online=True)
        self.assertEqual(result["prepared_count"], 1)
        self.assertEqual(result["items"][1]["state"], "prepared")
        self.assertTrue(any(head in path for path in requested))
        self.assertTrue(any(merged in path for path in requested))
        self.assertEqual(result["remote_mutations_performed"], 0)

    def test_dependency_cycle_rejected_before_runtime_state_is_created(self):
        self.candidate("T01", dependencies=["T02"])
        self.candidate("T02", dependencies=["T01"])
        result = self.run_pipeline(execute=True)
        self.assertEqual(result["prepared_count"], 0)
        self.assertTrue(all(row["state"] == "held" and "dependency_analysis:" in row["reason"]
                            for row in result["items"]))
        self.assertFalse(any((Path(item["artifacts"]) / "integration.json").exists()
                             for item in self.manifest["items"]))

    def test_explicit_new_base_revalidation_reuses_pending_candidate_slots(self):
        original_dirs = [self.candidate("T01")[0], self.candidate("T02")[0]]
        self.assertEqual(self.run_pipeline(execute=True, max_pending=2)["prepared_count"], 2)
        original_records = {directory: (directory / "integration.json").read_bytes() for directory in original_dirs}
        (self.root / "README.md").write_text("Independent main update\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Advance integration base")
        self.git("push", str(self.remote), "main")
        for item, original in zip(self.manifest["items"], original_dirs, strict=True):
            copy = self.directory / "explicit-revalidation" / original.name
            copy.mkdir(parents=True)
            for filename in ("manifest.json", "report.json", "changes.patch"):
                shutil.copy2(original / filename, copy / filename)
            item["artifacts"] = str(copy)
        result = self.run_pipeline(execute=True, max_pending=2)
        self.assertEqual(result["pending_candidates_before"], 2)
        self.assertEqual(result["prepared_count"], 2)
        for directory, before in original_records.items():
            self.assertEqual((directory / "integration.json").read_bytes(), before)

    def test_private_journal_records_are_not_budget_allocations(self):
        self.candidate("T01")
        self.run_pipeline(execute=True)
        state = common_state(self.root)
        self.assertFalse((state / "control.sqlite3").exists())
        with sqlite3.connect(state / "integration-pipeline.sqlite3") as db:
            self.assertEqual(db.execute("SELECT state FROM preparations").fetchone()[0], "prepared")
        receipt = next(state.glob("integration-pipelines/*/receipt.json"))
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        value = json.loads(receipt.read_text())
        self.assertIsNone(value["actual_cost_usd"])
        self.assertEqual(value["remote_mutations_performed"], 0)

    def test_invalid_options_and_unknown_manifest_fields_do_not_create_runtime_state(self):
        self.candidate("T01")
        for options in ({"lanes": 0}, {"lanes": 9}, {"lanes": True},
                        {"max_pending": 1}, {"check_timeout": 0}, {"execute": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_pipeline(**options)
        self.manifest["items"][0]["combined_ci_green"] = True
        with self.assertRaisesRegex(ValueError, "Each candidate"):
            self.run_pipeline(execute=True)
        self.assertFalse(common_state(self.root).exists())


if __name__ == "__main__":
    unittest.main()
