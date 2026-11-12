"""Real local Git source transport; all Sandbox/provider operations are mocked."""

import asyncio
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from swarm_control import modal_runner as runner
from swarm_control import modal_worker as worker
from swarm_control.source_bundle import (
    WORKER_BUNDLE_PATH,
    SourceBundleError,
    _environment,
    _git,
    build_source_bundle,
)
from tests.support import make_project


class SourceBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = make_project(self.root / "project", "fixture/private-project")
        (self.project.root / ".gitignore").write_text(".swarm/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "private state ignore")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.state = self.project.root / ".swarm"

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.project.root)

    def build(self, **kwargs):
        return build_source_bundle(self.project.root, self.base, self.state, **kwargs)

    def test_real_bundle_contains_only_exact_base_and_preserves_source_refs_index_and_worktree(self):
        before = {"refs": self.git("show-ref"), "index": (self.project.root / ".git/index").read_bytes(),
                  "head": self.git("rev-parse", "HEAD"), "status": self.git("status", "--porcelain")}
        (self.project.root / "src/item.txt").write_text("uncommitted source must not be included\n")
        (self.project.root / "private-untracked.txt").write_text("not in selected base\n")
        current = self.git("status", "--porcelain")
        bundle = self.build()
        self.assertEqual(bundle.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(bundle.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(bundle.metadata()["base_sha"], self.base)
        self.assertEqual(bundle.metadata()["bytes"], bundle.path.stat().st_size)
        self.assertNotIn(str(self.root), json.dumps(bundle.metadata()))
        self.assertEqual(hashlib.sha256(bundle.path.read_bytes()).hexdigest(), bundle.sha256)
        self.assertEqual(self.git("show-ref"), before["refs"])
        self.assertEqual((self.project.root / ".git/index").read_bytes(), before["index"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before["head"])
        self.assertEqual(self.git("status", "--porcelain"), current)
        receipt = json.loads((bundle.path.parent / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "ready")
        self.assertIsNone(receipt["cost_usd"])
        self.assertFalse((bundle.path.parent / "objects.git").exists())

    def test_worker_clones_offline_with_unreachable_private_origin_then_checks_exact_head(self):
        bundle = self.build()
        target = self.root / "worker"
        manifest = {"repository": "https://github.com/fixture/private-project.git", "base_sha": self.base,
                    "branch": "swarm/a1-offline", "source_bundle": bundle.metadata()}
        worker.prepare_repository(manifest, target, bundle_path=bundle.path)
        self.assertEqual(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target).decode().strip(), self.base)
        self.assertEqual((target / "src/item.txt").read_text(), "baseline\n")
        self.assertEqual(subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=target).decode().strip(),
                         manifest["repository"])
        log = self.root / "offline-check.log"
        self.assertEqual(worker.run_commands([[os.sys.executable, "-c", "print('offline baseline')"]], target, log), 0)
        self.assertIn("offline baseline", log.read_text())

    def test_selected_older_base_is_independent_from_mutable_head_and_current_files(self):
        (self.project.root / "src/item.txt").write_text("newer commit\n")
        self.git("add", ".")
        self.git("commit", "-qm", "later unselected work")
        newer_head = self.git("rev-parse", "HEAD")
        bundle = self.build()
        target = self.root / "older-worker"
        worker.prepare_repository({"repository": self.project.clone_url, "base_sha": self.base,
                                   "branch": "swarm/older", "source_bundle": bundle.metadata()},
                                  target, bundle_path=bundle.path)
        self.assertEqual((target / "src/item.txt").read_text(), "baseline\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), newer_head)

    def test_tampered_bundle_or_manifest_is_rejected_before_clone(self):
        bundle = self.build()
        manifest = {"repository": self.project.clone_url, "base_sha": self.base,
                    "branch": "swarm/a1", "source_bundle": bundle.metadata()}
        for key, value in (("sha256", "0" * 64), ("bytes", bundle.byte_count + 1),
                           ("worker_path", "/tmp/other.bundle"), ("base_sha", "0" * 40)):
            with self.subTest(field=key), self.assertRaises(ValueError):
                worker.prepare_repository({**manifest, "source_bundle": {**bundle.metadata(), key: value}},
                                          self.root / "never-created", bundle_path=bundle.path)
        self.assertFalse((self.root / "never-created").exists())
        with self.assertRaises(ValueError):
            worker.prepare_repository({**manifest, "repository": "https://token@github.com/fixture/private.git"},
                                      self.root / "never-created", bundle_path=bundle.path)

    def test_missing_base_unignored_state_oversize_and_deadline_fail_locally(self):
        with self.assertRaises(SourceBundleError):
            build_source_bundle(self.project.root, "0" * 40, self.state)
        with self.assertRaises(SourceBundleError):
            self.build(timeout=.000001)
        with self.assertRaises(SourceBundleError):
            self.build(maximum=32)
        self.assertEqual(list(self.state.rglob("*.bundle")), [])
        receipts = list(self.state.rglob("receipt.json"))
        self.assertTrue(receipts)
        self.assertEqual(json.loads(receipts[-1].read_text())["status"], "failed")
        (self.project.root / ".gitignore").unlink()
        with self.assertRaises(SourceBundleError):
            self.build()

    def test_shallow_and_symlink_caches_fail_closed(self):
        with patch("swarm_control.source_bundle._git") as git:
            git.side_effect = [str(self.project.root / ".git").encode(), b"", b"", b"true\n"]
            with self.assertRaisesRegex(SourceBundleError, "shallow"):
                self.build()
        self.state.mkdir()
        (self.state / "source-bundles").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SourceBundleError):
            self.build()

    def test_local_git_cannot_receive_account_credentials_or_execute_source_hooks(self):
        marker = self.root / "hook-executed"
        hook = self.root / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        self.git("config", "uploadpack.packObjectsHook", str(hook))
        env = {key: "fixture-secret" for key in ("GH_TOKEN", "GITHUB_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET",
                                                  "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY")}
        with patch.dict(os.environ, env):
            received = _environment()
            self.assertFalse(set(env) & received.keys())
            self.build()
        self.assertFalse(marker.exists())

    def test_hundred_distinct_mock_workers_upload_one_cached_bundle_with_same_digest(self):
        bundle = self.build()
        observed = []
        async def copy(local, remote):
            observed.append((local, remote, hashlib.sha256(Path(local).read_bytes()).hexdigest()))
        sandbox = SimpleNamespace(filesystem=SimpleNamespace(copy_from_local=SimpleNamespace(aio=copy)))
        with ThreadPoolExecutor(max_workers=100) as pool:
            list(pool.map(lambda _: bundle.upload(sandbox), range(100)))
        self.assertEqual(len(observed), 100)
        self.assertEqual(set(observed), {(str(bundle.path), WORKER_BUNDLE_PATH, bundle.sha256)})
        self.assertEqual(len(list(self.state.rglob("*.bundle"))), 1)

    def test_mock_sdk_upload_is_bounded_and_errors_propagate_to_existing_cleanup(self):
        bundle = self.build()
        async def hang(*_):
            await asyncio.sleep(30)
        sandbox = SimpleNamespace(filesystem=SimpleNamespace(copy_from_local=SimpleNamespace(aio=hang)))
        with self.assertRaises(TimeoutError):
            bundle.upload(sandbox, timeout=.01)
        bundle.path.chmod(0o644)
        with self.assertRaises(SourceBundleError):
            bundle.upload(sandbox)

    def test_bundle_failure_happens_before_operations_ledger_or_provider_admission(self):
        self.project.path.write_text(self.project.path.read_text().replace("live_enabled = false", "live_enabled = true"))
        (self.project.root / ".gitignore").unlink()
        provider = SimpleNamespace(App=SimpleNamespace(lookup=Mock()))
        with (patch.dict("sys.modules", {"modal": provider}),
              patch("swarm_control.operations.OperationsJournal") as operations,
              patch.object(runner, "verify_github_tasks") as github,
              patch("sys.argv", ["swarm-control", "run", "A1", "--project", str(self.project.path),
                                  "--base", "HEAD", "--source-transport", "bundle", "--model", "gpt-6.1-sol",
                                  "--model-budget", "1", "--gateway-url", "https://fixture.modal.run"]),
              patch.multiple(runner, ROOT=self.project.root, PROJECT=None, REPOSITORY=None, APP=None),
              redirect_stdout(io.StringIO()), self.assertRaises(SourceBundleError)):
            runner.main()
        provider.App.lookup.assert_not_called()
        operations.assert_not_called()
        github.assert_not_called()
        self.assertFalse((self.state / "control.sqlite3").exists())

    def test_foreign_fork_issue_url_is_rejected_even_when_issue_number_coincides(self):
        path = self.project.tasks_dir / "A1.json"
        task = json.loads(path.read_text())
        task["issueUrl"] = "https://github.com/upstream/project/issues/1"
        path.write_text(json.dumps(task))
        with patch.multiple(runner, PROJECT=self.project, ROOT=self.project.root), self.assertRaisesRegex(ValueError, "rebind"):
            runner.load_task("A1")
        task["issueUrl"] = "https://github.com/fixture/private-project/issues/1"
        path.write_text(json.dumps(task))
        with patch.multiple(runner, PROJECT=self.project, ROOT=self.project.root):
            self.assertEqual(runner.load_task("A1")["issue"], 1)

    def test_local_git_timeout_also_bounds_descendant_held_stdout(self):
        command = [os.sys.executable, "-c", "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"]
        started = time.monotonic()
        with patch("swarm_control.source_bundle._GIT", command), self.assertRaises(SourceBundleError):
            _git(None, [], time.monotonic() + .15)
        self.assertLess(time.monotonic() - started, 2)

    def test_upload_failure_keeps_sandbox_cleanup_and_upload_shares_preparation_allowance(self):
        bundle = self.build()
        for failed in (False, True):
            clock, timeouts = [0.0], []
            class Filesystem:
                def write_text(self, *_):
                    pass

                def read_bytes(self, *_):
                    raise FileNotFoundError

                def read_text(self, *_):
                    raise FileNotFoundError

            class Sandbox:
                object_id = "fixture-sandbox"
                filesystem = Filesystem()
                terminated = False

                def exec(self, *args, _timeouts=timeouts, **kwargs):
                    _timeouts.append((args[2], kwargs["timeout"]))
                    return SimpleNamespace(stdout=iter(()), stderr=iter(()), wait=lambda: 0, returncode=0)

                def terminate(self):
                    self.terminated = True

            sandbox = Sandbox()
            def upload(_, clock=clock, failed=failed):
                clock[0] += 60
                if failed:
                    raise TimeoutError("fixture upload timeout")
            prepared = SimpleNamespace(metadata=bundle.metadata, upload=upload)
            provider = SimpleNamespace(Sandbox=SimpleNamespace(create=Mock(return_value=sandbox)),
                                       exception=SimpleNamespace(SandboxFilesystemNotFoundError=FileNotFoundError))
            args = SimpleNamespace(command="smoke", timeout=30, source_bundle=prepared,
                context_packets={"A1": {"metadata": {}, "prompt": "fixture task"}}, run_id="fixture-run")
            directory = self.root / ("failed-upload" if failed else "shared-time")
            directory.mkdir()
            with (patch.dict("sys.modules", {"modal": provider}),
                  patch.multiple(runner, PROJECT=None, REPOSITORY=self.project.clone_url),
                  patch.object(runner.time, "monotonic", side_effect=lambda clock=clock: clock[0]), redirect_stdout(io.StringIO())):
                report = runner.execute_task({"id": "A1", "ownership": ["src"]}, args, self.base,
                                             directory, None, None, None)
            self.assertTrue(sandbox.terminated)
            self.assertTrue(report["sandbox_terminated"])
            self.assertNotIn("env", provider.Sandbox.create.call_args.kwargs)
            self.assertNotIn("secrets", provider.Sandbox.create.call_args.kwargs)
            self.assertEqual([timeout for action, timeout in timeouts if action == "prepare"], [] if failed else [180])


if __name__ == "__main__":
    unittest.main()
