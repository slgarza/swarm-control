"""Real local HTTP, reviewed commands and ledger preservation for first use."""

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_control.dashboard import make_server
from swarm_control.gui_budget import configure_budget
from swarm_control.local_commands import execute, validation_environment
from swarm_control.onboarding import Onboarding, SetupError, private_json
from swarm_control.pilot_state import Ledger
from swarm_control.project import load_project
from swarm_control.repository_setup import RepositorySetup
from swarm_control.workspace import Workspace
from tests.support import make_project


class FakeConnections:
    def status(self):
        return {name: {"configured": True, "verified": False} for name in ("github", "modal", "openai")}

    def environment(self):
        return {"PATH": os.environ["PATH"], "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "GH_TOKEN": "synthetic-controller-token", "MODAL_TOKEN_SECRET": "synthetic-modal-token"}


class SetupHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(self.root / "private/workspace.json")
        self.server = make_server(workspace_path=self.workspace.path)
        self.server.onboarding.connections = FakeConnections()
        self.server.onboarding.workspace.connections = self.server.onboarding.connections
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.csrf = self.request("GET", "/api/session")[1]["csrf_token"]
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, value=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        actual = {"Origin": self.origin, "Content-Type": "application/json"}
        if hasattr(self, "csrf"):
            actual["X-Swarm-CSRF"] = self.csrf
        actual.update(headers or {})
        try:
            connection.request(method, path, json.dumps(value) if value is not None else None, actual)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw and response.getheader("Content-Type", "").startswith("application/json") else raw
        finally:
            connection.close()

    def body(self, **changes):
        return {"action": "environment.configure", "options": {"dotenv_text": "GH_TOKEN=synthetic-private-token"},
                "confirmed": True, "request_id": "a" * 32, **changes}

    def test_setup_is_available_before_a_repository_and_inherits_http_origin_csrf_guards(self):
        code, result = self.request("GET", "/api/setup")
        self.assertEqual(code, 200)
        self.assertIn("environment.configure", result["capabilities"]["actions"])
        self.assertEqual(result["capabilities"]["worker_capacity"], 100)
        with patch.object(self.server.onboarding, "perform") as perform:
            self.assertEqual(self.request("POST", "/api/setup/action", self.body(), {"X-Swarm-CSRF": "invalid"})[0], 403)
            self.assertEqual(self.request("POST", "/api/setup/action", self.body(), {"Origin": "https://foreign.invalid"})[0], 403)
            self.assertEqual(self.request("POST", "/api/setup/action", self.body(confirmed=False))[0], 400)
            perform.assert_not_called()
        for asset in ("/assets/setup.js", "/assets/setup.css"):
            self.assertEqual(self.request("GET", asset)[0], 200)

    def test_secret_requests_are_not_logged_returned_or_replayed(self):
        secret = "synthetic-private-token"
        with patch.object(self.server.onboarding, "perform", return_value={"saved": True}) as perform:
            code, result = self.request("POST", "/api/setup/action", self.body())
            self.assertEqual(code, 202)
            self.server.onboarding.wait()
            self.assertNotIn(secret, json.dumps(result))
            self.assertEqual(self.request("POST", "/api/setup/action", self.body())[0], 202)
            self.assertEqual(perform.call_count, 1)
        code, result = self.request("GET", "/api/setup/job?id=" + "a" * 32)
        self.assertEqual(code, 200)
        self.assertEqual(result["job"]["status"], "finished")
        for path in self.server.onboarding.root.rglob("*.json"):
            self.assertNotIn(secret, path.read_text())
        restarted = Onboarding(self.workspace, connections=FakeConnections())
        self.assertEqual(restarted.job("a" * 32)["status"], "finished")
        self.assertEqual(restarted.status()["operations"][0]["id"], "a" * 32)

    def test_background_failure_removes_external_exception_text_and_audit_failure_stops_dispatch(self):
        secret = "synthetic-private-token"
        with patch.object(self.server.onboarding, "perform", side_effect=RuntimeError(secret)):
            self.request("POST", "/api/setup/action", self.body())
            self.server.onboarding.wait()
        result = self.request("GET", "/api/setup/job?id=" + "a" * 32)[1]
        self.assertEqual(result["job"]["status"], "failed")
        self.assertNotIn(secret, json.dumps(result))
        with (patch("swarm_control.onboarding.private_json", side_effect=OSError("disk unavailable")),
              patch.object(self.server.onboarding, "perform") as perform):
            self.assertEqual(self.request("POST", "/api/setup/action", self.body(request_id="b" * 32))[0], 503)
            perform.assert_not_called()

    def test_single_background_operation_keeps_http_alive_and_blocks_agent_launch(self):
        entered, release = threading.Event(), threading.Event()
        def perform(*args):
            entered.set()
            release.wait(timeout=5)
            return {"done": True}
        with patch.object(self.server.onboarding, "perform", side_effect=perform):
            try:
                self.assertEqual(self.request("POST", "/api/setup/action", self.body())[0], 202)
                self.assertTrue(entered.wait(timeout=3))
                self.assertEqual(self.request("GET", "/api/health")[0], 200)
                self.assertEqual(self.request("POST", "/api/setup/action", self.body(request_id="b" * 32))[0], 409)
                self.assertEqual(self.request("POST", "/api/runs/launch", {
                    "project_id": "unknown", "run_id": "unknown", "gateway_url": "https://fixture.modal.run", "confirmed": True})[0], 409)
            finally:
                release.set()
                self.server.onboarding.wait()


class CommandAndBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = make_project(self.root / "target with spaces")
        self.workspace = Workspace(self.root / "private/workspace.json")
        self.setup = Onboarding(self.workspace, connections=FakeConnections())

    def directory(self, name="operation"):
        path = self.root / name
        path.mkdir(mode=0o700)
        private_json(path / "request.json", {"confirmed": True, "model_limit_cents": 5000,
                                             "infra_limit_cents": 5000, "max_live_agents": 100})
        return path

    def test_reviewed_command_is_real_argv_and_cannot_be_replayed_or_change_after_preview(self):
        command = f"swarm-control analyze --project '{self.project.path}' --workers 1"
        review = self.setup.preview_cli(command)
        self.assertIn(str(self.project.path), review["argv"])
        def command_boundary(argv, **kwargs):
            if argv[0] == "git":
                return execute(argv, **kwargs)
            return {"exit_code": 0, "output": "{}"}
        with patch("swarm_control.onboarding.execute", side_effect=command_boundary) as run:
            self.setup.execute_cli(command, review["review_binding"], directory=self.directory())
            self.assertEqual(run.call_args.args[0][:3], [sys.executable, "-m", "swarm_control"])
            with self.assertRaises(SetupError):
                self.setup.execute_cli(command, review["review_binding"], directory=self.root)
            self.assertEqual(sum(call.args[0][0] != "git" for call in run.call_args_list), 1)
        review = self.setup.preview_cli(command)
        self.project.path.write_text(self.project.path.read_text() + "\n# reviewed profile changed\n")
        with self.assertRaises(SetupError):
            self.setup.execute_cli(command, review["review_binding"], directory=self.root)
        for invalid in ("python -c pass", "analyze; touch /tmp/forbidden", "run A1 --project x", "start", "budget apply --project x"):
            with self.subTest(invalid=invalid), self.assertRaises(SetupError):
                self.setup.preview_cli(invalid)

    def test_bounded_local_commands_strip_credentials_and_stop_excess_output_or_deadline(self):
        env = validation_environment(FakeConnections().environment())
        result = execute([sys.executable, "-c", "import os;print(os.getenv('GH_TOKEN'));print(os.getenv('MODAL_TOKEN_SECRET'))"],
                         cwd=self.root, environment=env)
        self.assertEqual(result["output"].splitlines(), ["None", "None"])
        result = execute([sys.executable, "-c", "print('synthetic-controller-token')"],
                         cwd=self.root, environment=FakeConnections().environment())
        self.assertNotIn("synthetic-controller-token", result["output"])
        noisy = execute([sys.executable, "-c", "print('x'*100000)"], cwd=self.root, environment=env, maximum=1000)
        self.assertTrue(noisy["output_truncated"])
        self.assertLessEqual(len(noisy["output"]), 1000)
        started = time.monotonic()
        slow = execute([sys.executable, "-c", "import subprocess,time;subprocess.Popen(['sleep','30']);time.sleep(30)"],
                       cwd=self.root, environment=env, timeout=0.1)
        self.assertEqual(slow["stop_reason"], "deadline_reached")
        self.assertLess(time.monotonic() - started, 3)

    def test_queue_preview_binds_relative_manifest_and_its_candidate_inputs(self):
        artifact = self.root / "candidate"
        artifact.mkdir()
        for filename in ("manifest.json", "report.json"):
            (artifact / filename).write_text("{}")
        (artifact / "changes.patch").write_text("original candidate")
        manifest = self.root / "queue.json"
        manifest.write_text(json.dumps({"schema_version": 1, "items": [{"artifacts": "candidate"}]}))
        with patch("swarm_control.onboarding.CONTROLLER", self.root):
            command = f"integrate-queue queue.json --project '{self.project.path}'"
            review = self.setup.preview_cli(command)
            self.assertEqual(review["argv"][2], str(manifest))
            (artifact / "manifest.json").write_text('{"task":{"acceptance_checks":[["changed"]]}}')
            with self.assertRaises(SetupError):
                self.setup.execute_cli(command, review["review_binding"], directory=self.root)

    def test_fresh_policy_configures_hundred_capacity_without_creating_a_ledger(self):
        result = configure_budget(RepositorySetup(self.workspace, environment=FakeConnections().environment()),
            str(self.project.root), 5000, 5000, 100, False, directory=self.directory())
        current = load_project(self.project.path)
        self.assertEqual(current.max_live_agents, 100)
        self.assertEqual(current.gateway_concurrency, 100)
        self.assertFalse(current.live_enabled)
        self.assertFalse(result["ledger_migrated"])
        self.assertFalse((self.project.root / ".swarm/control.sqlite3").exists())

    def test_gui_migration_preserves_all_existing_allocations_tasks_events(self):
        ledger_path = self.project.root / ".swarm/control.sqlite3"
        ledger = Ledger(ledger_path)
        ledger.allocate("original_failed_attempt", 1800, infra_cents=670)
        ledger.claim([{"id": "T01"}], "original_batch", "a" * 40)
        ledger.finish("T01", "original_batch", "merged", "original-report.json")
        def history():
            with sqlite3.connect(ledger_path) as db:
                return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                        for table in ("tasks", "events", "pilot_allocations", "pilot_infra_allocations")}
        before = history()
        result = configure_budget(RepositorySetup(self.workspace, environment=FakeConnections().environment()),
            str(self.project.root), 5000, 5000, 100, False, directory=self.directory())
        self.assertTrue(result["ledger_migrated"])
        self.assertEqual(history(), before)
        self.assertEqual(load_project(self.project.path).model_limit_cents, 5000)
        self.assertTrue((self.root / "operation/budget-authorization.json").exists())
        with self.assertRaises(SetupError):
            configure_budget(RepositorySetup(self.workspace), str(self.project.root), 0, 0, 1, False,
                             directory=self.directory("attempted_reset"))
        self.assertEqual(history(), before)

    def test_blocked_drafts_are_visible_but_cannot_be_dispatched(self):
        draft = {"id": "DRAFT1", "title": "Imported issue", "prompt": "Review its scope first", "status": "blocked",
                 "ownership": [], "acceptance": [], "dependsOn": []}
        (self.project.tasks_dir / "DRAFT1.json").write_text(json.dumps(draft))
        connected = self.workspace.connect_project(self.project.root)
        self.assertEqual(next(task for task in connected["tasks"] if task["id"] == "DRAFT1")["status"], "blocked")
        with self.assertRaises(ValueError):
            self.workspace.prepare_run(connected["project_id"], {"task_ids": ["DRAFT1"]})
