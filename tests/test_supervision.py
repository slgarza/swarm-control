"""Real local subprocess closure and deterministic supervision evidence."""

import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from swarm_control.modal_worker import (
    process_group_state,
    run_agent,
    source_fingerprint,
)
from swarm_control.progress import WorkerProgress
from tests.support import make_project


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = make_project(self.root / "target")
        self.artifacts = self.root / "artifacts"

    def execute(self, code, *, timeout=2, idle=None):
        result = run_agent([sys.executable, "-c", code], self.project.root, self.artifacts,
            ["src"], timeout=timeout, no_progress_timeout=idle, poll_seconds=0.01, grace_seconds=0.15,
            task_id="A1", base_sha=self.project.root.name)
        record = json.loads((self.artifacts / "supervision.json").read_text())
        self.assertIsNone(record["remote_provider_cause"])
        self.assertFalse(record["useful_progress_verified"])
        self.assertEqual(record["automatic_retries"], 0)
        self.assertEqual((self.artifacts / "supervision.json").stat().st_mode & 0o077, 0)
        return result, record

    def test_owned_shell_edits_have_progress_even_without_native_file_change_event(self):
        code = "from pathlib import Path; import time; Path('src/new.txt').write_text('useful'); time.sleep(.08)"
        result, record = self.execute(code)
        self.assertEqual(result, 0)
        self.assertEqual(record["terminal_reason"], "child_exited")
        self.assertGreaterEqual(record["source_change_count"], 1)
        self.assertIsNotNone(record["first_source_change_seconds"])

    def test_deadline_has_explicit_reason_and_graceful_process_group_stop(self):
        result, record = self.execute("import time; time.sleep(10)", timeout=0.15)
        self.assertEqual(result, 128 + signal.SIGTERM)
        self.assertEqual(record["terminal_reason"], "coding_deadline_reached")
        self.assertIn("SIGTERM", record["signals"])
        self.assertLess(record["elapsed_seconds"], 2)

    def test_sigterm_ignoring_child_is_killed_with_a_recorded_policy_cause(self):
        code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(10)"
        result, record = self.execute(code, timeout=0.3)
        self.assertEqual(result, 137)
        self.assertEqual(record["signals"], ["SIGTERM", "SIGKILL"])
        self.assertEqual(record["terminal_reason"], "coding_deadline_reached")
        self.assertIsNone(record["remote_provider_cause"])

    def test_configured_no_progress_stop_is_distinct_and_default_silence_is_allowed(self):
        result, record = self.execute("import time; time.sleep(10)", timeout=1, idle=0.15)
        self.assertEqual(result, 143)
        self.assertEqual(record["terminal_reason"], "configured_no_source_progress")
        result, record = self.execute("import time; time.sleep(.3)", timeout=0.5)
        self.assertEqual(result, 0)
        self.assertEqual(record["terminal_reason"], "child_exited")
        self.assertIn("no_source_change_observed", record["warnings"])

    def test_process_group_cleanup_stops_a_grandchild_after_the_parent_exits(self):
        descendant = ("import signal,time;from pathlib import Path;"
                      "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                      "\nwhile True: Path('src/heartbeat.txt').write_text(str(time.monotonic()));time.sleep(.02)")
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(descendant) + "]);time.sleep(10)"
        _, record = self.execute(code, timeout=0.25)
        self.assertIn("SIGKILL", record["signals"])
        heartbeat = self.project.root / "src/heartbeat.txt"
        before = heartbeat.read_bytes()
        time.sleep(0.08)
        self.assertEqual(heartbeat.read_bytes(), before)

    def test_actual_changes_reset_the_opted_in_idle_budget(self):
        code = ("import time;from pathlib import Path; time.sleep(.06);"
                "Path('src/new.txt').write_text('one');time.sleep(.08);"
                "Path('src/new.txt').write_text('two');time.sleep(.06)")
        result, record = self.execute(code, idle=0.14)
        self.assertEqual(result, 0)
        self.assertGreaterEqual(record["source_change_count"], 2)

    def test_unavailable_source_observation_does_not_kill_on_unproven_idle(self):
        with patch("swarm_control.modal_worker.source_fingerprint", side_effect=OSError):
            result, record = self.execute("import time; time.sleep(.2)", timeout=1, idle=0.05)
        self.assertEqual(result, 0)
        self.assertGreater(record["source_observation_failures"], 0)
        self.assertEqual(record["terminal_reason"], "child_exited")

    def test_initial_receipt_failure_prevents_process_spawn(self):
        with (patch("swarm_control.modal_worker.private_json", side_effect=OSError),
              patch("swarm_control.modal_worker.source_fingerprint", return_value="frozen"),
              patch("swarm_control.modal_worker.subprocess.Popen") as spawn,
              self.assertRaises(OSError)):
            self.execute("raise AssertionError('must not start')")
        spawn.assert_not_called()

    def test_no_progress_policy_validation_and_spawn_failure_preserve_evidence(self):
        for idle in (-1, 3, True):
            with self.subTest(idle=idle), self.assertRaises(ValueError):
                self.execute("pass", idle=idle)
        with self.assertRaises(FileNotFoundError):
            run_agent(["/definitely/not/an/executable"], self.project.root, self.artifacts,
                      ["src"], timeout=1)
        data = json.loads((self.artifacts / "supervision.json").read_text())
        self.assertEqual(data["terminal_reason"], "spawn_failed")
        self.assertEqual(data["error_type"], "FileNotFoundError")

    def test_source_fingerprint_includes_untracked_content_updates(self):
        first = source_fingerprint(self.project.root, ["src"])
        target = self.project.root / "src/new.txt"
        target.write_text("one")
        second = source_fingerprint(self.project.root, ["src"])
        target.write_text("longer")
        third = source_fingerprint(self.project.root, ["src"])
        self.assertEqual(len({first, second, third}), 3)

    def test_agent_wrapper_preserves_stdin_for_the_coding_process(self):
        script = self.root / "invoke.py"
        command = [sys.executable, "-c",
                   "import sys;from pathlib import Path;Path('src/prompt.txt').write_text(sys.stdin.read())"]
        script.write_text("from pathlib import Path\nfrom swarm_control.modal_worker import run_agent\n"
                          + "raise SystemExit(run_agent(" + repr(command) + ",Path(" + repr(str(self.project.root))
                          + "),Path(" + repr(str(self.artifacts)) + "),['src'],timeout=2,poll_seconds=.01))\n")
        subprocess.run([sys.executable, str(script)], input="frozen task prompt\n", text=True,
                       check=True, capture_output=True)
        self.assertEqual((self.project.root / "src/prompt.txt").read_text(), "frozen task prompt\n")

    def test_natural_parent_exit_cleans_descendants_and_outer_output_reaches_eof(self):
        descendant = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
        parent = ("import os,subprocess,sys,time;from pathlib import Path;"
                  "Path('src/group.txt').write_text(str(os.getpid()));"
                  "subprocess.Popen([sys.executable,'-c'," + repr(descendant) + "]);time.sleep(.1)")
        command = [sys.executable, "-c", parent]
        script = self.root / "natural-parent.py"
        script.write_text("from pathlib import Path\nfrom swarm_control.modal_worker import run_agent\n"
                          + "raise SystemExit(run_agent(" + repr(command) + ",Path(" + repr(str(self.project.root))
                          + "),Path(" + repr(str(self.artifacts))
                          + "),['src'],timeout=1,poll_seconds=.01,grace_seconds=.1))\n")
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=3, check=True)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result.returncode, 0)
        record = json.loads((self.artifacts / "supervision.json").read_text())
        self.assertEqual(record["terminal_reason"], "child_exited")
        self.assertEqual(record["child_exit_code"], 0)
        self.assertEqual(record["wrapper_exit_code"], 0)
        self.assertEqual(record["process_group_cleanup"], "verified")
        self.assertIn("SIGTERM", record["signals"])
        group_id = int((self.project.root / "src/group.txt").read_text())
        self.assertEqual(process_group_state(group_id), "stopped")

    def test_hundred_workers_keep_distinct_progress_and_receipts(self):
        def worker(index):
            writes = []
            clock = [float(index)]
            progress = WorkerProgress(writes.append, monotonic=lambda: clock[0])
            with progress.phase("agent"):
                clock[0] += 1
                progress.event({"type": "item.completed", "item": {"type": "file_change", "status": "completed"}}, "2026-11-12T16:00:00Z")
                progress.agent_exit(0)
            path = self.root / f"worker-{index}.json"
            path.write_text(json.dumps(progress.snapshot()))
            return path, writes
        with ThreadPoolExecutor(max_workers=100) as pool:
            observations = list(pool.map(worker, range(100)))
        self.assertEqual(len({str(path) for path, _ in observations}), 100)
        for path, writes in observations:
            data = json.loads(path.read_text())
            self.assertEqual(data["file_change_signals"], 1)
            self.assertEqual(data["first_file_change_seconds"], 1)
            self.assertFalse(data["useful_progress_verified"])
            self.assertTrue(writes)
