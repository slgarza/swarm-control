"""Offline regression tests for worker boundaries and recoverable Git output."""

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

from swarm_control import modal_runner as runner
from swarm_control.modal_worker import collect, valid_path
from swarm_control.pilot_state import Ledger
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
    def test_actual_gateway_capacity_must_attest_larger_cohorts(self):
        runner.verify_gateway_capacity({}, 20)
        runner.verify_gateway_capacity({"gateway_concurrency": 100}, 100)
        for admission in ({}, {"gateway_concurrency": 20}, {"gateway_concurrency": None},
                          {"gateway_concurrency": True}, {"gateway_concurrency": "100"},
                          {"gateway_concurrency": 101}):
            with self.subTest(admission=admission), self.assertRaisesRegex(ValueError, "deployed gateway"):
                runner.verify_gateway_capacity(admission, 100)

    def test_shared_paths_require_explicit_opt_in_but_duplicates_never_pass(self):
        tasks = [
            {"id": "A1", "ownership": ["src/shared.py"]},
            {"id": "A2", "ownership": ["src"]},
        ]
        runner.validate_batch(tasks, 2, allow_overlap=True)
        self.assertEqual(runner.overlap_pairs(tasks), [["A1", "A2"]])
        with self.assertRaisesRegex(ValueError, "overlapping"):
            runner.validate_batch(tasks, 2)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            runner.validate_batch([tasks[0], tasks[0]], 2, allow_overlap=True)
        with self.assertRaisesRegex(ValueError, "at least one"):
            runner.validate_batch([], 1)

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

    def test_100_shared_file_tasks_have_explicit_collision_plan_without_execution(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            project = make_project(Path(temporary))
            template = json.loads((project.tasks_dir / "A1.json").read_text())
            task_ids = [f"T{index:03}" for index in range(100)]
            for index, task_id in enumerate(task_ids):
                (project.tasks_dir / f"{task_id}.json").write_text(json.dumps({
                    **template, "id": task_id, "issue": index + 1,
                }))
            with (
                patch.multiple(runner, ROOT=project.root, PROJECT=None, REPOSITORY=None, APP=None),
                patch("sys.argv", ["swarm-control", "plan", *task_ids,
                                   "--project", str(project.path), "--workers", "100",
                                   "--synchronized", "--allow-overlap", "--base", "HEAD"]),
                redirect_stdout(output),
            ):
                self.assertEqual(runner.main(), 0)
            self.assertFalse((project.root / ".swarm").exists())
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["maximum_active_agents"], 100)
        self.assertEqual(plan["gateway_concurrency"], 20)
        self.assertFalse(plan["gateway_capacity_sufficient"])
        self.assertTrue(plan["allow_overlap"])
        self.assertEqual(len(plan["overlap_pairs"]), 4950)
        self.assertFalse(plan["live_execution_enabled"])

    def test_synchronized_run_requires_model_capacity_before_claiming_or_cloud_import(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            project = make_project(Path(temporary))
            project.path.write_text(project.path.read_text().replace(
                "live_enabled = false", "live_enabled = true"
            ).replace("[budget]", "[runtime]\ngateway_concurrency = 1\n[budget]"))
            template = json.loads((project.tasks_dir / "A1.json").read_text())
            (project.tasks_dir / "A2.json").write_text(json.dumps({**template, "id": "A2", "issue": 2}))
            with (
                patch.multiple(runner, ROOT=project.root, PROJECT=None, REPOSITORY=None, APP=None),
                patch.dict("sys.modules", {"modal": None}),
                patch("sys.argv", ["swarm-control", "run", "A1", "A2", "--project", str(project.path),
                                   "--workers", "2", "--synchronized", "--allow-overlap", "--base", "HEAD",
                                   "--model", "gpt-6.1-sol", "--model-budget", "1",
                                   "--gateway-url", "https://fake.modal.run"]),
                redirect_stderr(output), self.assertRaises(SystemExit) as stopped,
            ):
                runner.main()
            self.assertEqual(stopped.exception.code, 2)
            self.assertIn("runtime.gateway_concurrency", output.getvalue())
            self.assertFalse((project.root / ".swarm").exists())

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
    def test_mocked_100_agent_run_keeps_one_budget_and_incremental_task_state(self):
        self.run_mocked_100_agent_cohort(100)

    def test_stale_deployed_capacity_stops_100_agent_run_without_spawning_or_refunding(self):
        self.run_mocked_100_agent_cohort(20)

    def test_missing_deployed_capacity_stops_100_agent_run_without_spawning_or_refunding(self):
        self.run_mocked_100_agent_cohort(None)

    def run_mocked_100_agent_cohort(self, actual_capacity):
        class Image:
            @classmethod
            def from_registry(cls, *_args, **_kwargs):
                return cls()

            def apt_install(self, *_):
                return self

            def run_commands(self, *_):
                return self

            def add_local_file(self, *_):
                return self

        gateway_calls = []

        def remote(name, *args):
            gateway_calls.append((name, args))
            if name == "create_run":
                return {"tokens": {task: "fake-task-token" for task in args[1]},
                        "budget_usd": args[2], **(
                            {"gateway_concurrency": actual_capacity} if actual_capacity is not None else {}
                        )}
            return {"state": "stopped"}

        fake_modal = SimpleNamespace(
            App=SimpleNamespace(lookup=lambda *_args, **_kwargs: object()), Image=Image,
            Function=SimpleNamespace(from_name=lambda _app, name, **_: SimpleNamespace(
                remote=lambda *args: remote(name, *args))),
            Secret=SimpleNamespace(from_dict=lambda _: object()),
        )
        rendezvous = Barrier(100)

        def execute(task, args, base, batch, *_):
            rendezvous.wait(timeout=10)
            return {"task_id": task["id"], "status": "validated", "sandbox_terminated": True}

        with tempfile.TemporaryDirectory() as temporary:
            project = make_project(Path(temporary))
            project.path.write_text(project.path.read_text().replace(
                "live_enabled = false", "live_enabled = true\nmax_live_agents = 100"
            ).replace("model_limit_cents = 1800", "model_limit_cents = 18000").replace(
                "infra_limit_cents = 700", "infra_limit_cents = 4000"
            ).replace("[budget]", "[runtime]\ngateway_concurrency = 100\n[budget]"))
            template = json.loads((project.tasks_dir / "A1.json").read_text())
            task_ids = [f"T{index:03}" for index in range(100)]
            for index, task_id in enumerate(task_ids):
                (project.tasks_dir / f"{task_id}.json").write_text(json.dumps({
                    **template, "id": task_id, "issue": index + 1,
                }))
            with (
                patch.multiple(runner, ROOT=project.root, PROJECT=None, REPOSITORY=None, APP=None),
                patch.dict("sys.modules", {"modal": fake_modal}),
                patch.object(runner, "verify_github_tasks"),
                patch.object(runner, "execute_task", side_effect=execute) as execute_worker,
                patch("sys.argv", ["swarm-control", "run", *task_ids,
                                   "--project", str(project.path), "--workers", "100",
                                   "--synchronized", "--allow-overlap", "--base", "HEAD",
                                   "--model", "gpt-6.1-sol", "--model-budget", "180",
                                   "--gateway-url", "https://fake.modal.run"]),
                redirect_stdout(io.StringIO()),
            ):
                if actual_capacity == 100:
                    self.assertEqual(runner.main(), 0)
                    self.assertEqual(execute_worker.call_count, 100)
                else:
                    with self.assertRaisesRegex(ValueError, "deployed gateway capacity"):
                        runner.main()
                    execute_worker.assert_not_called()
            ledger = Ledger(project.root / ".swarm/control.sqlite3", model_limit_cents=18000, infra_limit_cents=4000)
            self.assertEqual(len(ledger.rows()), 100)
            expected_state = "awaiting_integration" if actual_capacity == 100 else "launch_failed"
            self.assertTrue(all(row["state"] == expected_state for row in ledger.rows()))
            with ledger.connect() as db:
                self.assertEqual(db.execute("SELECT cents FROM pilot_allocations").fetchone()[0], 18000)
                self.assertEqual(db.execute("SELECT cents FROM pilot_infra_allocations").fetchone()[0], 3100)
            for path in (project.root / ".swarm").rglob("*.json"):
                self.assertNotIn("fake-task-token", path.read_text())
        self.assertEqual([name for name, _ in gateway_calls], ["create_run", "stop_run"])

    def test_dispatch_checkpoints_fast_task_before_slow_one_and_bounds_submissions(self):
        slow_released = Event()
        checkpointed = Event()
        tasks = [{"id": name} for name in ("A1", "A2", "A3")]
        checkpoints = []

        def execute(task):
            if task["id"] == "A1":
                self.assertTrue(slow_released.wait(timeout=2))
            if task["id"] == "A3":
                self.assertTrue(checkpointed.is_set())
                slow_released.set()
            return {"task_id": task["id"], "status": "validated"}

        def checkpoint(task, report):
            checkpoints.append(task["id"])
            if task["id"] == "A2":
                checkpointed.set()

        reports = runner.dispatch_tasks(tasks, 2, execute, checkpoint)
        self.assertEqual(checkpoints[0], "A2")
        self.assertEqual(len(reports), 3)
        self.assertEqual(runner.dispatch_tasks([], 2, execute, checkpoint), [])

    def test_100_concurrent_fake_workers_checkpoint_failures_without_replacements(self):
        tasks = [{"id": f"T{index:03}"} for index in range(100)]
        barrier = Barrier(100)
        counter_lock = Lock()
        active = peak = calls = 0

        def execute(task):
            nonlocal active, peak, calls
            with counter_lock:
                calls += 1
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=10)
            with counter_lock:
                active -= 1
            if task["id"] == "T001":
                raise RuntimeError("simulated launch failure")
            return {"task_id": task["id"], "status": "validated"}

        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary) / "batch-test"
            batch.mkdir()
            path = Path(temporary) / "state.db"
            ledger = Ledger(path)
            ledger.claim(tasks, batch.name, "a" * 40)
            reports = runner.dispatch_tasks(
                tasks, 100, execute,
                lambda task, report: runner.record_task_result(ledger, batch, task, report),
            )
            rows = Ledger(path).rows()
        self.assertEqual(calls, 100)
        self.assertEqual(peak, 100)
        self.assertEqual(len(reports), 100)
        self.assertEqual(sum(row["state"] == "awaiting_integration" for row in rows), 99)
        self.assertEqual(sum(row["state"] == "launch_failed" for row in rows), 1)

    def test_checkpoint_failure_stops_admitting_new_work(self):
        calls = []

        def execute(task):
            calls.append(task["id"])
            return {"task_id": task["id"], "status": "validated"}

        def checkpoint(task, report):
            raise OSError("simulated durable state failure")

        with self.assertRaises(OSError):
            runner.dispatch_tasks([{"id": "A1"}, {"id": "A2"}], 1, execute, checkpoint)
        self.assertEqual(calls, ["A1"])

    def test_reconciliation_records_missing_and_interrupted_workers_durably(self):
        tasks = [{"id": "A1"}, {"id": "A2"}]
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary) / "batch-test"
            batch.mkdir()
            ledger = Ledger(Path(temporary) / "state.db")
            ledger.claim(tasks, batch.name, "a" * 40)
            runner.record_task_result(ledger, batch, tasks[0])
            runner.record_task_result(ledger, batch, tasks[1], {"status": "working"})
            self.assertEqual([row["state"] for row in ledger.rows()], ["launch_failed", "interrupted"])
            self.assertEqual(json.loads((batch / "A2/report.json").read_text())["status"], "interrupted")

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
            command="smoke", model=None, timeout=30, environment="main",
            allow_overlap=True, overlap_pairs=[["A1", "A2"], ["A2", "A3"]],
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
            manifest = json.loads((Path(directory) / "A1/manifest.json").read_text())
            self.assertIs(manifest["allow_overlap"], True)
            self.assertEqual(manifest["overlap_pairs"], [["A1", "A2"]])


if __name__ == "__main__":
    unittest.main()
