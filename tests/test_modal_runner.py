"""Offline regression tests for worker boundaries and recoverable Git output."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

from swarm_control import modal_runner as runner
from swarm_control import modal_worker as worker
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
        (self.repo / ".gitignore").write_text(".swarm/\n")
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

    def test_target_validation_cannot_read_administrative_billing_keys(self):
        secret_names = ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY",
                        "OPENAI_ADMIN_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")
        environment = {name: "synthetic-secret" for name in secret_names}
        environment["SWARM_TEST_SETTING"] = "preserved"
        probe = "import json,os; print(json.dumps(dict(os.environ)))"
        log = self.root / "validation.log"
        with patch.dict(os.environ, environment, clear=True):
            exit_code = worker.run_commands([[sys.executable, "-c", probe]], self.repo, log)
            # Filtering applies only to child commands; trusted billing code in
            # this process retains access to the operator's explicit credential.
            self.assertEqual(os.environ["OPENAI_ADMIN_KEY"], "synthetic-secret")
        self.assertEqual(exit_code, 0)
        received = json.loads(log.read_text())
        self.assertEqual(received["SWARM_TEST_SETTING"], "preserved")
        self.assertTrue(set(secret_names).isdisjoint(received))
        self.assertNotIn("synthetic-secret", log.read_text())

    def test_benchmark_receipts_preserve_exact_utf8_without_copying_budget_or_logs(self):
        receipts = self.repo / ".swarm/benchmarks"
        (receipts / "nested").mkdir(parents=True)
        raw = '{"measurement_kind":"local_validation","note":"señal","cost_usd":null}\n'.encode()
        (receipts / "nested/attempt.json").write_bytes(raw)
        (receipts / "checks.log").write_text("private raw log")
        (self.repo / ".swarm/control.sqlite3").write_bytes(b"private ledger")
        (self.repo / ".swarm/budget.json").write_text('{"private":"budget"}')
        result = self.capture()
        bundle = json.loads((self.root / "artifacts/benchmarks.json").read_text())
        self.assertEqual(bundle["receipt_count"], 1)
        receipt = bundle["receipts"][0]
        self.assertEqual(receipt["text"].encode(), raw)
        self.assertEqual(receipt["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(receipt["byte_count"], len(raw))
        self.assertEqual(receipt["name"], "attempt.json")
        self.assertEqual(receipt["relative_path"], "nested/attempt.json")
        self.assertNotIn("private raw log", json.dumps(bundle))
        self.assertNotIn("private ledger", json.dumps(bundle))
        self.assertNotIn("private budget", json.dumps(bundle))
        self.assertEqual(result["benchmark_receipts"]["source"], "worker-reported")
        self.assertNotIn("text", json.dumps(result["benchmark_receipts"]))
        self.assertEqual(result["ownership_violations"], [])

    def test_missing_benchmarks_are_an_explicit_empty_collection(self):
        self.assertEqual(self.capture()["benchmark_receipts"]["receipt_count"], 0)
        self.assertEqual(json.loads((self.root / "artifacts/benchmarks.json").read_text())["receipts"], [])

    def test_invalid_receipt_fails_collection_but_preserves_patch_and_error_artifact(self):
        receipts = self.repo / ".swarm/benchmarks"
        receipts.mkdir(parents=True)
        receipt = receipts / "attempt.json"
        (self.repo / "packages/ui/old.ts").write_text("useful change\n")
        for raw in (b"[]", b"broken", b'{"cost":NaN}', b"\xff"):
            with self.subTest(raw=raw):
                receipt.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "benchmark_receipt_json_invalid"):
                    self.capture()
                bundle = json.loads((self.root / "artifacts/benchmarks.json").read_text())
                self.assertEqual(bundle["status"], "failed")
                self.assertIn("useful change", (self.root / "artifacts/changes.patch").read_text())

    def test_benchmark_symlinks_are_rejected_without_following_them(self):
        receipts = self.repo / ".swarm/benchmarks"
        receipts.mkdir(parents=True)
        outside = self.root / "outside.json"
        outside.write_text('{"secret":"outside"}')
        (receipts / "attempt.json").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "benchmark_symlink_prohibited"):
            self.capture()
        (receipts / "attempt.json").unlink()
        receipts.rmdir()
        receipts.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "benchmark_symlink_prohibited"):
            self.capture()
        self.assertNotIn("outside", (self.root / "artifacts/benchmarks.json").read_text())

    def test_benchmark_count_individual_and_total_bytes_have_hard_limits(self):
        receipts = self.repo / ".swarm/benchmarks"
        receipts.mkdir(parents=True)
        for name in ("one.json", "two.json"):
            (receipts / name).write_text('{"ok":true}')
        for setting, maximum, reason in (
            ("MAX_BENCHMARK_RECEIPTS", 1, "benchmark_receipt_count_limit"),
            ("MAX_BENCHMARK_RECEIPT_BYTES", 5, "benchmark_receipt_size_limit"),
            ("MAX_BENCHMARK_TOTAL_BYTES", 15, "benchmark_total_size_limit"),
        ):
            with self.subTest(setting=setting), patch.object(worker, setting, maximum), self.assertRaisesRegex(ValueError, reason):
                self.capture()


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
        admission = plan["local_admission"]
        self.assertEqual(admission["ready_task_count"], 100)
        self.assertFalse(admission["project_capacity_sufficient"])
        self.assertEqual(admission["minimum_infrastructure_reserve_cents"], 3100)
        self.assertFalse(admission["infrastructure_policy_covers_minimum_reserve"])
        self.assertIsNone(admission["remaining_infrastructure_allowance_cents"])
        self.assertFalse(admission["budget_ledger_checked"])
        self.assertEqual(admission["budget_accounting_kind"], "local_admission_reservations_not_provider_spend")
        self.assertFalse(admission["provider_billing_queried"])
        self.assertIsNone(admission["provider_reported_spend_usd"])
        self.assertIsNone(admission["provider_credit_balance_usd"])
        self.assertIsNone(admission["provider_funds_exhausted"])
        self.assertFalse(admission["launch_readiness_verified"])

    def test_local_admission_does_not_count_blocked_or_duplicate_issue_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = make_project(Path(temporary))
            dependency = project.tasks_dir / "D1.json"
            dependency.write_text(json.dumps({"id": "D1", "status": "ready"}))
            tasks = [
                {"id": "T1", "issue": 1, "status": "blocked"},
                {"id": "T2", "issue": 2, "blockedBy": ["external_decision"]},
                {"id": "T3", "issue": 3, "dependsOn": ["D1"]},
                {"id": "T4", "issue": 4},
                {"id": "T5", "issue": 4},
                {"id": "T6", "issue": True},
                {"id": "T7", "issue": 7},
            ]
            with patch.object(project.__class__, "ledger", side_effect=AssertionError("ledger touched")):
                admission = runner.local_admission_summary(tasks, 7, project)
            self.assertFalse((project.root / ".swarm").exists())
            self.assertEqual(admission["ready_task_ids"], ["T7"])
            self.assertEqual(admission["maximum_metadata_ready_agents"], 1)
            self.assertIn("dependency_not_locally_merged:D1", admission["blocked_tasks"]["T3"])
            self.assertEqual(admission["blocked_tasks"]["T4"], ["duplicate_issue"])
            self.assertEqual(admission["blocked_tasks"]["T5"], ["duplicate_issue"])
            self.assertEqual(admission["blocked_tasks"]["T6"], ["missing_issue"])
            dependency.write_text(json.dumps({"id": "D1", "status": "merged", "pullRequest": 11}))
            admission = runner.local_admission_summary([tasks[2]], 1, project)
            self.assertEqual(admission["ready_task_count"], 1)
            self.assertFalse(admission["github_readiness_checked"])
            self.assertFalse(admission["provider_capacity_verified"])
            self.assertFalse(admission["launch_readiness_verified"])

    def test_local_admission_rejects_invalid_or_escaped_dependency_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = make_project(Path(temporary))
            task = {"id": "T1", "issue": 1, "dependsOn": ["D1"]}
            dependency = project.tasks_dir / "D1.json"
            for contents in ("invalid-json", "[]", '{"status":"merged","pullRequest":true}'):
                dependency.write_text(contents)
                admission = runner.local_admission_summary([task], 1, project)
                self.assertEqual(admission["ready_task_count"], 0)
            dependency.unlink()
            dependency.symlink_to(project.root / "README.md")
            admission = runner.local_admission_summary([task], 1, project)
            self.assertEqual(admission["blocked_tasks"]["T1"], ["dependency_metadata_invalid:D1"])

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
            'shell_environment_policy.exclude=["CODEX_API_KEY","OPENAI_API_KEY",'
            '"OPENAI_ADMIN_KEY","OPENAI_ADMIN_API_KEY"]',
            command,
        )
        fake_key = "sk-" + "x" * 30
        self.assertNotIn(fake_key, runner.redact("example=" + fake_key))


class LifecycleTests(unittest.TestCase):
    def test_final_check_benchmarks_are_recovered_before_termination_and_validated(self):
        for invalid_bundle in (False, True):
            with self.subTest(invalid_bundle=invalid_bundle):
                self.check_final_benchmark_recovery(invalid_bundle)

    def check_final_benchmark_recovery(self, invalid_bundle):
        class Process:
            stdout = stderr = ()
            returncode = 0

            def wait(self):
                return 0

        receipt_text = '{"measurement_kind":"local_validation","private_note":"exact check evidence"}\n'
        receipt_bytes = receipt_text.encode()
        received = []

        class Filesystem:
            def __init__(self):
                self.files = {
                    "changes.patch": b"", "changes.json": json.dumps({
                        "ownership_violations": [], "prohibited_files": [],
                        "head_unchanged": True, "patch_bytes": 0,
                    }).encode(), "install.log": b"installed", "checks.log": b"passed", "versions.json": b"{}",
                }

            def write_text(self, *_):
                pass

            def read_bytes(self, name):
                key = Path(name).name
                if key not in self.files:
                    raise FileNotFoundError(name)
                received.append(key)
                return self.files[key]

            def read_text(self, name):
                return self.read_bytes(name).decode()

        class Sandbox:
            object_id = "sb-benchmark"
            filesystem = Filesystem()
            checked = False
            terminated = False

            def exec(self, *args, **_):
                if "check" in args:
                    self.checked = True
                if "collect" in args:
                    records = [{"name": "check.json", "relative_path": "check.json",
                                "text": receipt_text, "byte_count": len(receipt_bytes),
                                "sha256": "bad" if invalid_bundle else hashlib.sha256(receipt_bytes).hexdigest()}] if self.checked else []
                    self.filesystem.files["benchmarks.json"] = json.dumps({
                        "format_version": 1, "source": "worker-reported", "status": "complete",
                        "receipt_count": len(records), "total_bytes": len(receipt_bytes) if records else 0,
                        "receipts": records,
                    }).encode()
                return Process()

            def terminate(self):
                assert "benchmarks.json" in received
                self.terminated = True

        sandbox = Sandbox()
        fake_modal = SimpleNamespace(Sandbox=SimpleNamespace(create=lambda **_: sandbox),
            exception=SimpleNamespace(SandboxFilesystemNotFoundError=FileNotFoundError))
        args = SimpleNamespace(command="smoke", model=None, timeout=30, environment="main")
        with tempfile.TemporaryDirectory() as temporary, patch.dict("sys.modules", {"modal": fake_modal}), redirect_stdout(io.StringIO()):
            batch = Path(temporary)
            report = runner.execute_task({"id": "A1", "ownership": ["src"]}, args,
                                         "a" * 40, batch, None, None, None)
            recovered = json.loads((batch / "A1/benchmarks.json").read_text())
            self.assertEqual(recovered["receipts"][0]["text"], receipt_text)
            self.assertTrue(sandbox.terminated)
            if invalid_bundle:
                self.assertEqual(report["status"], "needs_recovery")
                self.assertIn("benchmarks.json: ValueError", report["recovery_errors"])
            else:
                self.assertEqual(report["status"], "smoke_passed")
                self.assertEqual(report["benchmark_receipts"]["receipt_count"], 1)
                self.assertEqual(report["benchmark_receipts"]["source"], "worker-reported")
                self.assertNotIn("private_note", json.dumps(report))

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
