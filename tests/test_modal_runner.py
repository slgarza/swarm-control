"""Offline regression tests for worker boundaries and recoverable Git output."""

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from swarm_control import modal_runner as runner
from swarm_control.modal_worker import collect, valid_path
from tests.support import make_project


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "packages/ui").mkdir(parents=True)
        (self.repo / "packages/ui/old.ts").write_text("export const old = 1;\n")
        (self.repo / "outside.txt").write_text("baseline\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True)

    def capture(self):
        return collect(self.repo, self.base, ["packages/ui"], self.root / "artifacts")

    def test_patch_recovers_new_modified_deleted_and_binary_files(self):
        (self.repo / "packages/ui/old.ts").unlink()
        (self.repo / "packages/ui/new.ts").write_text("export const token = '#fff';\n")
        (self.repo / "packages/ui/data.bin").write_bytes(b"\x00\xff\x01\x02")
        result = self.capture()
        self.assertEqual(len(result["changed_files"]), 3)
        self.assertEqual(result["ownership_violations"], [])
        patch_path = self.root / "artifacts/changes.patch"
        restored = self.root / "restored"
        subprocess.run(
            ["git", "clone", "-q", str(self.repo), str(restored)], check=True
        )
        subprocess.run(["git", "apply", str(patch_path)], cwd=restored, check=True)
        self.assertFalse((restored / "packages/ui/old.ts").exists())
        self.assertEqual(
            (restored / "packages/ui/data.bin").read_bytes(), b"\x00\xff\x01\x02"
        )
        self.assertIn("token", (restored / "packages/ui/new.ts").read_text())

    def test_out_of_scope_changes_are_captured_but_rejected(self):
        (self.repo / "outside.txt").write_text("changed\n")
        result = self.capture()
        self.assertEqual(result["ownership_violations"], ["outside.txt"])
        self.assertFalse(runner.eligible_result(0, 0, result))
        self.assertIn("changed", (self.root / "artifacts/changes.patch").read_text())

    def test_agent_commits_are_recoverable_but_rejected(self):
        (self.repo / "packages/ui/old.ts").write_text("changed\n")
        self.git("add", ".")
        self.git("commit", "-qm", "unauthorized")
        result = self.capture()
        self.assertFalse(result["head_unchanged"])
        self.assertGreater(result["patch_bytes"], 0)
        self.assertFalse(runner.eligible_result(0, 0, result))

    def test_symlinks_and_env_files_are_rejected(self):
        (self.repo / "packages/ui/link").symlink_to("/tmp")
        (self.repo / "packages/ui/.env.local").write_text("EXAMPLE=placeholder\n")
        self.assertEqual(
            self.capture()["prohibited_files"],
            ["packages/ui/.env.local", "packages/ui/link"],
        )

    def test_empty_or_failed_runs_never_count_as_validated(self):
        self.assertFalse(runner.eligible_result(0, 0, self.capture()))
        (self.repo / "packages/ui/old.ts").write_text("changed\n")
        result = self.capture()
        self.assertTrue(runner.eligible_result(0, 0, result))
        self.assertFalse(runner.eligible_result(1, 0, result))
        self.assertFalse(runner.eligible_result(0, 1, result))
        self.assertFalse(runner.eligible_result(None, 0, result))


class PlanningTests(unittest.TestCase):
    def test_duplicate_and_parent_child_ownership_are_blocked(self):
        for second in ["packages/ui", "packages/ui/src", "packages"]:
            with self.assertRaises(ValueError):
                runner.validate_batch(
                    [
                        {"id": "A1", "ownership": ["packages/ui"]},
                        {"id": "A2", "ownership": [second]},
                    ],
                    2,
                )
        runner.validate_batch(
            [
                {"id": "A1", "ownership": ["packages/ui"]},
                {"id": "A2", "ownership": ["packages/ui-other"]},
            ],
            2,
        )

    def test_path_traversal_and_ambiguous_paths_are_rejected(self):
        for path in [
            "",
            ".",
            "../engine",
            "/tmp",
            "a//b",
            "a/./b",
            "a\\b",
            ".git/hooks",
        ]:
            self.assertFalse(valid_path(path), path)
        self.assertTrue(valid_path("packages/ui/src"))

    def test_missing_task_dependencies_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "swarm/tasks").mkdir(parents=True)
            (root / "swarm/tasks/A1.json").write_text(
                json.dumps(
                    {
                        "id": "A1",
                        "title": "test",
                        "prompt": "test",
                        "ownership": ["packages/ui"],
                        "acceptance": ["test"],
                        "dependsOn": ["P03"],
                    }
                )
            )
            with patch.object(runner, "ROOT", root), patch.object(runner, "PROJECT", None), self.assertRaises(ValueError):
                runner.load_task("A1")

    def test_plan_does_not_claim_100_agents_for_one_task(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            project = make_project(Path(temporary))
            with (
                patch.multiple(runner, ROOT=project.root, PROJECT=None, REPOSITORY=None, APP=None),
                patch("sys.argv", ["swarm-control", "plan", "A1", "--project", str(project.path),
                                   "--workers", "100", "--base", "HEAD"]),
                redirect_stdout(output),
            ):
                self.assertEqual(runner.main(), 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["maximum_active_agents"], 1)
        self.assertFalse(plan["enough_tasks_for_100"])
        self.assertFalse(plan["live_execution_enabled"])

    def test_second_local_dispatcher_cannot_claim_work(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            runner.controller_lock(Path(directory)),
            self.assertRaises(ValueError),
            runner.controller_lock(Path(directory)),
        ):
            self.fail("duplicate dispatcher acquired lock")

    def test_credentials_are_not_in_argv_and_logs_are_redacted(self):
        command = runner.codex_command("example-model")
        self.assertIn("--ignore-user-config", command)
        self.assertIn(
            'shell_environment_policy.exclude=["CODEX_API_KEY","OPENAI_API_KEY"]',
            command,
        )
        fake_key = "sk-" + "x" * 30
        self.assertNotIn(fake_key, runner.redact("example=" + fake_key))


class LifecycleTests(unittest.TestCase):
    def test_chunked_events_are_individually_timestamped_and_split_keys_redacted(self):
        process = SimpleNamespace(
            stdout=iter(
                [
                    '{"type":"thread.started"}\n{"type":"item.',
                    (
                        'completed","item":{"type":"command_execution"}}\n'
                        '{"type":"turn.completed"}\n{"type":"final'
                    ),
                    '_event"}',
                ]
            ),
            stderr=iter(
                [
                    "warning credential=sk-" + "x" * 8,
                    "x" * 22 + " failure\ntrailing sk-" + "y" * 7,
                    "y" * 23,
                ]
            ),
            returncode=7,
            wait=lambda: None,
        )
        timestamp = "2026-11-10T20:00:00+00:00"
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(runner, "now", return_value=timestamp),
        ):
            directory = Path(temporary)
            self.assertEqual(runner.consume(process, directory, "agent"), 7)
            events = [
                json.loads(line)
                for line in (directory / "agent.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [event["type"] for event in events],
                [
                    "thread.started",
                    "item.completed",
                    "turn.completed",
                    "final_event",
                ],
            )
            self.assertTrue(
                all(event["swarm_received_at"] == timestamp for event in events)
            )
            self.assertEqual(
                (directory / "agent.stderr.log").read_text(),
                "warning credential=[REDACTED_OPENAI_KEY] failure\n"
                "trailing [REDACTED_OPENAI_KEY]",
            )

    def test_plain_text_and_partial_eof_output_are_preserved(self):
        process = SimpleNamespace(
            stdout=iter(["not JSON\n[1,", "2]\ntrai", "ling text"]),
            stderr=iter(["first\r", "\nlast partial"]),
            returncode=0,
            wait=lambda: None,
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(runner.consume(process, directory, "prepare"), 0)
            self.assertEqual(
                (directory / "prepare.jsonl").read_text(),
                "not JSON\n[1,2]\ntrailing text",
            )
            self.assertEqual(
                (directory / "prepare.stderr.log").read_bytes(),
                b"first\r\nlast partial",
            )

    def test_failed_setup_still_recovers_artifacts_and_terminates(self):
        class Process:
            stdout = ()
            stderr = ()

            def __init__(self, exit_code):
                self.returncode = exit_code

            def wait(self):
                return self.returncode

        class Filesystem:
            def write_text(self, *_):
                pass

            def read_bytes(self, name):
                if name.endswith("changes.patch"):
                    return b"partial work\n"
                if name.endswith("install.log"):
                    return b"install failed\n"
                raise FileNotFoundError(name)

        class Sandbox:
            object_id = "sb-test"
            filesystem = Filesystem()
            terminated = False

            def exec(self, *args, **_):
                return Process(1 if "prepare" in args else 0)

            def terminate(self):
                self.terminated = True

        sandbox = Sandbox()
        fake_modal = SimpleNamespace(
            Sandbox=SimpleNamespace(create=lambda **_: sandbox),
            exception=SimpleNamespace(SandboxFilesystemNotFoundError=FileNotFoundError),
        )
        args = SimpleNamespace(
            command="smoke", model=None, timeout=30, environment="main"
        )
        task = {"id": "A1", "ownership": ["packages/ui"]}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("sys.modules", {"modal": fake_modal}),
        ):
            with redirect_stdout(io.StringIO()):
                report = runner.execute_task(
                    task, args, "a" * 40, Path(directory), None, None, None
                )
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["sandbox_terminated"])
            self.assertFalse(report["merged"])
            self.assertEqual(
                (Path(directory) / "A1/changes.patch").read_text(), "partial work\n"
            )
            self.assertTrue(sandbox.terminated)


if __name__ == "__main__":
    unittest.main()
