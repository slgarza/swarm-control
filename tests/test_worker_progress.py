"""Offline incident checks for measured worker closure and frozen task packets."""

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
from swarm_control.context_packet import task_packet
from swarm_control.progress import WorkerProgress
from tests.support import make_project


class ProgressTests(unittest.TestCase):
    def test_monotonic_measurements_and_fixed_event_allowlist(self):
        tick = [10.0]
        snapshots = []
        progress = WorkerProgress(snapshots.append, monotonic=lambda: tick[0],
                                  utc_now=lambda: "2026-11-11T22:00:00+00:00")
        with progress.phase("agent"):
            progress.event({"type": "thread.started", "thread_id": "private-thread"}, "2026-11-11T22:00:00+00:00")
            progress.event({"type": "item.completed", "item": {"type": "command_execution", "command": "private command"}}, "2026-11-11T22:00:00+00:00")
            progress.event({"type": ["untrusted"], "message": "private output"}, "2026-11-11T22:00:00+00:00")
            tick[0] = 15.25
            progress.event({"type": "turn.completed", "usage": {"arbitrary": "private usage"}}, "2026-11-11T22:00:05+00:00")
            progress.agent_exit(0)
        result = progress.snapshot()
        self.assertEqual(result["phases"][0]["wall_seconds"], 5.25)
        self.assertEqual(result["agent_process_outcome"], "successful_exit")
        self.assertEqual(result["event_counts"]["turn.completed"], 1)
        self.assertEqual(result["item_counts"]["completed:command_execution"], 1)
        self.assertEqual(result["event_counts"]["other"], 1)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("untrusted", json.dumps(result))
        self.assertIsNone(result["model_service_seconds"])
        self.assertIsNone(result["request_queue_seconds"])
        self.assertIsNone(result["remote_termination_cause"])
        self.assertLessEqual(len(snapshots), 5)

    def test_negative_exit_and_137_never_invent_timeout_or_oom(self):
        for code, expected in ((-1, "unknown"), (137, "nonzero_exit"), (None, "unknown")):
            with self.subTest(code=code):
                progress = WorkerProgress()
                progress.agent_exit(code)
                data = progress.snapshot()
                self.assertEqual(data["agent_process_outcome"], expected)
                self.assertIsNone(data["remote_termination_cause"])
                self.assertNotIn("oom", json.dumps(data))

    def test_observed_wait_timeout_is_distinct_from_remote_termination(self):
        progress = WorkerProgress()
        with self.assertRaises(TimeoutError), progress.phase("agent"):
            raise TimeoutError("private provider details")
        data = progress.snapshot()
        self.assertEqual(data["agent_process_outcome"], "wait_timeout_observed")
        self.assertEqual(data["phases"][0]["outcome"], "raised")
        self.assertIsNone(data["remote_termination_cause"])
        self.assertNotIn("private", json.dumps(data))

    def test_event_checkpoints_are_throttled_but_phase_boundaries_persist(self):
        writes = []
        progress = WorkerProgress(writes.append, monotonic=lambda: 1.0)
        with progress.phase("agent"):
            for _ in range(1000):
                progress.event({"type": "item.started", "item": {"type": "file_change"}}, "2026-11-11T22:00:00+00:00")
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1]["event_counts"]["item.started"], 1000)

    def test_failed_observer_is_recorded_without_interrupting_cleanup_phases(self):
        def failed(_):
            raise OSError("private filesystem details")

        progress = WorkerProgress(failed)
        with progress.phase("terminate"):
            pass
        result = progress.snapshot()
        self.assertEqual(result["checkpoint_failures"], 2)
        self.assertEqual(result["phases"][0]["outcome"], "returned")
        self.assertNotIn("private", json.dumps(result))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = make_project(Path(self.temporary.name) / "project")
        docs = self.project.root / "docs"
        docs.mkdir()
        (docs / "contract.md").write_text("Heading\nExact accepted rule\nUnrelated section\n")
        subprocess.run(["git", "add", "docs"], cwd=self.project.root, check=True)
        subprocess.run(["git", "commit", "-qm", "contract"], cwd=self.project.root, check=True)
        self.base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.project.root, text=True).strip()
        self.task = json.loads((self.project.tasks_dir / "A1.json").read_text())
        self.task["context_files"] = [{"path": "docs/contract.md", "start_line": 2, "end_line": 2}]

    def test_task_packet_uses_frozen_excerpt_and_preserves_global_authority(self):
        (self.project.root / "docs/contract.md").write_text("Different mutable text\n")
        excerpts, metadata = task_packet(self.task, self.project, self.base, "task")
        self.assertEqual(excerpts[0]["text"], "Exact accepted rule\n")
        self.assertTrue(metadata["required_instructions_preserved"])
        self.assertNotIn("Exact accepted rule", json.dumps(metadata))
        with patch.object(runner, "PROJECT", self.project):
            packets = runner.build_context_packets([self.task], self.base, "task", timeout=300)
        prompt = packets["A1"]["prompt"]
        self.assertIn("Read README.md, AGENTS.md before editing.", prompt)
        self.assertIn("still apply in full", prompt)
        self.assertIn("Exact accepted rule", prompt)
        self.assertNotIn("Different mutable text", prompt)
        self.assertIn("60 seconds remaining", prompt)
        with patch.object(runner, "PROJECT", self.project):
            different_limit = runner.build_context_packets([self.task], self.base, "task", timeout=600)
        self.assertNotEqual(packets["A1"]["metadata"]["prompt_sha256"],
                            different_limit["A1"]["metadata"]["prompt_sha256"])

    def test_missing_traversal_or_unbounded_context_fails(self):
        for refs in (None, [], [{"path": "../secret.md", "start_line": 1, "end_line": 1}],
                     [{"path": "docs/contract.md", "start_line": True, "end_line": 1}],
                     [{"path": "docs/contract.md", "start_line": 1, "end_line": 100}],
                     [{"path": "src/item.txt", "start_line": 1, "end_line": 1}]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                task_packet({**self.task, "context_files": refs}, self.project, self.base, "task")

    def test_symlink_context_is_rejected_without_reading_its_target(self):
        (self.project.root / "docs/link.md").symlink_to("contract.md")
        subprocess.run(["git", "add", "docs/link.md"], cwd=self.project.root, check=True)
        subprocess.run(["git", "commit", "-qm", "link"], cwd=self.project.root, check=True)
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.project.root, text=True).strip()
        with self.assertRaises(ValueError):
            task_packet({**self.task, "context_files": [{"path": "docs/link.md", "start_line": 1, "end_line": 1}]}, self.project, base, "task")

    def test_full_mode_needs_no_optional_contract_selectors(self):
        excerpts, metadata = task_packet({k: v for k, v in self.task.items() if k != "context_files"}, self.project, self.base)
        self.assertEqual(excerpts, [])
        self.assertEqual(metadata["mode"], "full")

    def test_plan_alias_and_binding_create_no_runtime_state_or_public_prompt(self):
        output = io.StringIO()
        originals = {name: getattr(runner, name) for name in ("ROOT", "PROJECT", "REPOSITORY", "APP")}
        with patch.multiple(runner, **originals), patch("sys.argv", [
            "swarm-control", "plan", "A1", "--project", str(self.project.path), "--agents", "1",
            "--base", "HEAD", "--run-id", "draft-one",
        ]), redirect_stdout(output):
            self.assertEqual(runner.main(), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["requested_workers"], 1)
        self.assertEqual(data["run_id"], "draft-one")
        self.assertEqual(data["context_packets"]["A1"]["mode"], "full")
        self.assertNotIn("prompt", data["context_packets"]["A1"])
        self.assertNotIn("Implement acceptance.", json.dumps(data))
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_run_identity_rejects_path_or_credential_shape(self):
        for value in ("../run", "sk-" + "x" * 32, "github_pat_" + "x" * 32, "a" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.validated_run_id(value)

    def test_not_dispatched_report_gets_its_actual_run_identity_without_success(self):
        report = {"task_id": "A1", "status": "not_dispatched", "reason": "operations_hold"}
        runner.bind_report_identity(report, {"id": "A1"}, "draft-one", self.base)
        self.assertEqual(report["run_id"], "draft-one")
        self.assertEqual(report["base_sha"], self.base)
        self.assertEqual(report["status"], "not_dispatched")
        with self.assertRaises(ValueError):
            runner.bind_report_identity(report, {"id": "A2"}, "draft-one", self.base)


class WorkerIncidentTests(unittest.TestCase):
    def execute(self, *, mode="run", patch_bytes=b"candidate\n", agent_exit=0,
                prepare_exit=0, agent_wait_error=None, mutate_check=False, observer_failure=False):
        class Process:
            def __init__(self, code=0, events=(), error=None):
                self.returncode, self.stdout, self.stderr = code, iter(events), iter(())
                self.stdin = SimpleNamespace(write=lambda _: None, write_eof=lambda: None, drain=lambda: None)
                self.error = error

            def wait(self):
                if self.error is not None:
                    raise self.error
                return self.returncode

        bundle = {"format_version": 1, "source": "worker-reported", "status": "complete",
                  "receipt_count": 0, "total_bytes": 0, "receipts": []}
        files = {
            "changes.patch": patch_bytes,
            "changes.json": json.dumps({"ownership_violations": [], "prohibited_files": [],
                "head_unchanged": True, "patch_bytes": len(patch_bytes)}).encode(),
            "install.log": b"installed", "versions.json": b"{}", "benchmarks.json": json.dumps(bundle).encode(),
        }
        actions, saved_reports = [], []

        class Filesystem:
            def write_text(self, *_):
                pass

            def read_bytes(self, name):
                key = Path(name).name
                if key not in files:
                    raise FileNotFoundError(name)
                return files[key]

            def read_text(self, name):
                return self.read_bytes(name).decode()

        class Sandbox:
            object_id = "synthetic-sandbox"
            filesystem = Filesystem()
            terminated = False

            def exec(self, *args, **_):
                if args[0] == "codex":
                    actions.append("agent")
                    return Process(agent_exit, ['{"type":"thread.started","thread_id":"private-id"}\n',
                        '{"type":"item.completed","item":{"type":"command_execution","command":"private command"}}\n',
                        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}\n'], agent_wait_error)
                action = args[2]
                actions.append(action)
                if action == "check":
                    files["checks.log"] = b"checked"
                    if mutate_check:
                        files["changes.patch"] = b"changed by check\n"
                return Process(prepare_exit if action == "prepare" else 0)

            def terminate(self):
                self.terminated = True

        sandbox = Sandbox()
        fake_modal = SimpleNamespace(Sandbox=SimpleNamespace(create=lambda **_: sandbox),
            exception=SimpleNamespace(SandboxFilesystemNotFoundError=FileNotFoundError))
        args = SimpleNamespace(command=mode, model="gpt-6.1-sol", timeout=30,
            environment="main", gateway_url="https://synthetic.modal.run", run_id="draft-one", context_mode="full")
        original_write = runner.write_json

        def capture(path, value):
            if path.name == "report.json":
                if observer_failure and value.get("progress") and not saved_reports[-1].get("progress"):
                    observer_failures.append(True)
                    if len(observer_failures) == 1:
                        raise OSError("synthetic progress write failure")
                saved_reports.append(json.loads(json.dumps(value)))
            original_write(path, value)

        observer_failures = []
        with tempfile.TemporaryDirectory() as directory, patch.dict("sys.modules", {"modal": fake_modal}), \
             patch.object(runner, "PROJECT", None), patch.object(runner, "write_json", side_effect=capture), \
             redirect_stdout(io.StringIO()):
            report = runner.execute_task({"id": "A1", "ownership": ["src"]}, args, "a" * 40,
                                         Path(directory), None, None, None)
            manifest = json.loads((Path(directory) / "A1/manifest.json").read_text())
            self.assertEqual(manifest["run_id"], "draft-one")
            self.assertTrue((Path(directory) / "A1/report.json").stat().st_mode & 0o077 == 0)
        self.assertTrue(sandbox.terminated)
        self.assertNotIn("private command", json.dumps(report["progress"]))
        self.assertTrue(all(row.get("finished_at") or row["status"] not in {"validated", "smoke_passed"}
                            for row in saved_reports))
        return report, actions

    def test_empty_coding_result_skips_full_check_but_still_recovers_and_terminates(self):
        report, actions = self.execute(patch_bytes=b"")
        self.assertEqual(report["status"], "needs_repair")
        self.assertEqual(report["validation_skipped"], "empty_patch")
        self.assertIsNone(report["check_exit"])
        self.assertNotIn("check", actions)
        self.assertEqual(actions.count("collect"), 2)
        self.assertEqual(report["progress"]["event_counts"]["turn.completed"], 1)

    def test_empty_smoke_still_checks_unchanged_project(self):
        report, actions = self.execute(mode="smoke", patch_bytes=b"")
        self.assertEqual(report["status"], "smoke_passed")
        self.assertIn("check", actions)
        self.assertNotIn("validation_skipped", report)

    def test_validated_requires_all_original_gates_despite_completed_turn(self):
        for code, expected in ((0, "validated"), (-1, "needs_repair"), (137, "needs_repair"), (False, "needs_repair")):
            with self.subTest(code=code):
                report, _ = self.execute(agent_exit=code)
                self.assertEqual(report["status"], expected)
                self.assertIsNone(report["progress"]["remote_termination_cause"])
        report, _ = self.execute(mutate_check=True)
        self.assertEqual(report["status"], "needs_repair")
        self.assertIn("checks modified", report["error"])

    def test_failed_prepare_and_wait_timeout_keep_recovery_receipts(self):
        report, actions = self.execute(prepare_exit=1)
        self.assertEqual(report["status"], "failed")
        self.assertNotIn("agent", actions)
        prepare = next(row for row in report["progress"]["phases"] if row["phase"] == "prepare")
        self.assertEqual(prepare["process_exit"], 1)
        self.assertEqual(prepare["outcome"], "nonzero_exit")
        report, actions = self.execute(agent_wait_error=TimeoutError("synthetic wait timeout"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["progress"]["agent_process_outcome"], "wait_timeout_observed")
        self.assertIsNone(report["progress"]["remote_termination_cause"])
        self.assertIn("collect", actions)

    def test_failed_progress_checkpoint_still_terminates_and_never_validates(self):
        report, actions = self.execute(observer_failure=True)
        self.assertEqual(report["status"], "needs_recovery")
        self.assertEqual(report["progress"]["checkpoint_failures"], 1)
        self.assertIn("progress_checkpoint_failed", report["recovery_errors"])
        self.assertIn("check", actions)


if __name__ == "__main__":
    unittest.main()
