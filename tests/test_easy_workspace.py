"""Beginner repository connection and explicit browser execution boundaries."""

import http.client
import json
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_control.dashboard import BrowserLaunch, BrowserLaunchBusy, make_server
from swarm_control.workspace import ProjectConnectionError, Workspace
from tests.support import make_project


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = make_project(self.root / "target with spaces")
        self.workspace = Workspace(self.root / "private/workspace.json")

    def test_folder_and_config_reconnect_preserve_id_and_immutable_history(self):
        connected = self.workspace.connect_project(self.target.root, label_text="My project")
        project_id = connected["project_id"]
        saved = self.workspace.prepare_run(project_id, {"task_ids": ["A1"]}, save=True)
        before = self.workspace.path.read_bytes()
        again = self.workspace.connect_project(str(self.target.path))
        self.assertEqual(again["project_id"], project_id)
        self.assertEqual(again["label"], "My project")
        self.assertEqual(self.workspace.path.read_bytes(), before)
        self.assertEqual(self.workspace.run_detail(project_id, saved["id"])["status"], "draft")
        self.assertNotIn(str(self.root), json.dumps(again))
        self.assertFalse((self.target.root / ".swarm").exists())

    def test_invalid_connections_never_write_registry_and_return_public_guidance(self):
        cases = [("https://github.com/example/project", "local_path_required"),
                 (self.root / "missing", "project_config_missing")]
        for selected, code in cases:
            with self.subTest(code=code), self.assertRaises(ProjectConnectionError) as caught:
                self.workspace.connect_project(selected)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(str(self.root), str(caught.exception))
            self.assertFalse(self.workspace.path.exists())
        (self.target.tasks_dir / "A1.json").write_text("not JSON")
        with self.assertRaises(ProjectConnectionError) as caught:
            self.workspace.connect_project(self.target.path)
        self.assertEqual(caught.exception.code, "project_tasks_invalid")
        self.assertFalse(self.workspace.path.exists())

    def test_origin_mismatch_and_bad_configuration_are_actionable(self):
        subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/other/repo.git"],
                       cwd=self.target.root, check=True)
        with self.assertRaises(ProjectConnectionError) as caught:
            self.workspace.connect_project(self.target.root)
        self.assertEqual(caught.exception.code, "project_origin_mismatch")
        self.target.path.write_text("not TOML {{{")
        with self.assertRaises(ProjectConnectionError) as caught:
            self.workspace.connect_project(self.target.root)
        self.assertEqual(caught.exception.code, "project_config_invalid")
        self.assertFalse(self.workspace.path.exists())


class BrowserLaunchHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.target = make_project(self.root / "target")
        self.workspace = Workspace(self.root / "private/workspace.json")
        self.project_id = self.workspace.connect_project(self.target.root)["project_id"]
        self.saved = self.workspace.prepare_run(self.project_id,
            {"task_ids": ["A1"], "model_budget": 0.50}, save=True)
        self.server = make_server(workspace_path=self.workspace.path)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.csrf = self.request("GET", "/api/session")[1]["csrf_token"]

        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
        self.addCleanup(cleanup)

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            actual_headers = {"Origin": self.origin, "Content-Type": "application/json"}
            if hasattr(self, "csrf"):
                actual_headers["X-Swarm-CSRF"] = self.csrf
            actual_headers.update(headers or {})
            connection.request(method, path, body=json.dumps(data) if data is not None else None,
                               headers=actual_headers)
            response = connection.getresponse()
            raw = response.read()
            self.assertNotIn(str(self.root).encode(), raw)
            return response.status, json.loads(raw) if raw else None
        finally:
            connection.close()

    def launch_body(self, **changes):
        return {"project_id": self.project_id, "run_id": self.saved["id"],
                "gateway_url": "https://verified-gateway.modal.run", "confirmed": True, **changes}

    def launch_path(self):
        return f"/api/launch?project={self.project_id}&run={self.saved['id']}"

    def test_connect_route_requires_csrf_and_returns_no_private_paths(self):
        data = {"path": str(self.target.root)}
        self.assertEqual(self.request("POST", "/api/projects", data,
                         {"X-Swarm-CSRF": "wrong"})[0], 403)
        code, context = self.request("POST", "/api/projects", data)
        self.assertEqual(code, 200)
        self.assertEqual(context["project_id"], self.project_id)
        code, error = self.request("POST", "/api/projects", {"path": "https://github.com/a/b"})
        self.assertEqual(code, 400)
        self.assertEqual(error["code"], "local_path_required")
        self.assertEqual(self.request("POST", "/api/projects", {**data, "command": "evil"})[0], 400)

    def test_confirmation_gateway_and_csrf_are_required_before_launch(self):
        with patch.object(self.server.browser_launch.workspace, "launch") as launch:
            for changes in ({"confirmed": False}, {"confirmed": 1},
                            {"gateway_url": "http://private.invalid"},
                            {"gateway_url": "https://gateway.modal.run/path"}):
                with self.subTest(changes=changes):
                    self.assertEqual(self.request("POST", "/api/runs/launch",
                                     self.launch_body(**changes))[0], 400)
            self.assertEqual(self.request("POST", "/api/runs/launch", self.launch_body(),
                             {"Origin": "https://foreign.invalid"})[0], 403)
            self.assertEqual(self.request("POST", "/api/runs/launch", self.launch_body(),
                             {"X-Swarm-CSRF": "wrong"})[0], 403)
            launch.assert_not_called()
        for path in ("/api/deploy", "/api/budget", "/api/launch"):
            self.assertEqual(self.request("POST", path, {})[0], 404)
        self.assertEqual(self.request("PUT", "/api/runs/launch", self.launch_body())[0], 405)

    def test_disabled_policy_is_reported_without_starting_runner_or_spending(self):
        with patch.object(self.server.browser_launch.workspace, "_preflight",
                          side_effect=AssertionError("admission reached before live authorization")):
            code, observation = self.request("POST", "/api/runs/launch", self.launch_body())
            self.assertEqual(code, 202)
            self.assertIn(observation["status"], {"starting", "running", "failed"})
            self.server.browser_launch.thread.join(timeout=3)
        code, outcome = self.request("GET", self.launch_path())
        self.assertEqual(code, 200)
        self.assertEqual(outcome["status"], "failed")
        self.assertIn("disabled", outcome["error"])
        self.assertEqual(self.workspace.run_detail(self.project_id, self.saved["id"])["status"], "draft")
        self.assertFalse((self.target.root / ".swarm").exists())

    def test_async_launch_is_bounded_and_keeps_http_responsive(self):
        entered, release = threading.Event(), threading.Event()

        def execute(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            self.assertEqual(args, (self.project_id, self.saved["id"]))
            self.assertTrue(kwargs["execute"])
            return {"runner_exit_code": 0}

        with patch.object(self.server.browser_launch.workspace, "launch", side_effect=execute) as launch:
            try:
                self.assertEqual(self.request("POST", "/api/runs/launch", self.launch_body())[0], 202)
                self.assertTrue(entered.wait(timeout=3))
                self.assertEqual(self.request("GET", "/api/health")[0], 200)
                self.assertEqual(self.request("POST", "/api/runs/launch", self.launch_body())[0], 409)
                self.assertEqual(launch.call_count, 1)
                self.assertEqual(self.request("GET", self.launch_path())[1]["status"], "running")
            finally:
                release.set()
                self.server.browser_launch.thread.join(timeout=3)
        self.assertEqual(self.request("GET", self.launch_path())[1]["status"], "finished")
        self.assertFalse(self.server.browser_launch.thread.daemon)

    def test_raw_launch_failure_is_never_returned_to_browser(self):
        with patch.object(self.server.browser_launch.workspace, "launch",
                          side_effect=ValueError(str(self.root) + " ghp_private_secret")):
            self.assertEqual(self.request("POST", "/api/runs/launch", self.launch_body())[0], 202)
            self.server.browser_launch.thread.join(timeout=3)
        error = self.request("GET", self.launch_path())[1]
        self.assertEqual(error["status"], "failed")
        self.assertNotIn("private_secret", json.dumps(error))
        self.assertEqual(self.request("GET", "/api/launch?project=other&run=unknown")[0], 400)
        audit_root = self.workspace.path.with_name("workspace.launch-attempts")
        directories = list(audit_root.iterdir())
        self.assertEqual(len(directories), 1)
        directory = directories[0]
        self.assertEqual(stat.S_IMODE(audit_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for name in ("request.json", "outcome.json", "failure.txt"):
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
        self.assertIn("private_secret", (directory / "failure.txt").read_text())
        self.assertEqual(json.loads((directory / "outcome.json").read_text())["status"], "failed")
        # A fresh HTTP server forgets its live status but preserves private originals.
        fresh = BrowserLaunch(self.workspace)
        self.assertEqual(fresh.observation(self.project_id, self.saved["id"])["status"], "idle")
        self.assertTrue((directory / "failure.txt").exists())

    def test_audit_write_failure_prevents_dispatch(self):
        with patch.object(self.server.browser_launch, "_write_private", side_effect=OSError("full disk")), \
                patch.object(self.server.browser_launch.workspace, "launch") as launch:
            code, error = self.request("POST", "/api/runs/launch", self.launch_body())
            self.assertEqual(code, 503)
            self.assertEqual(error["code"], "launch_evidence_unavailable")
            launch.assert_not_called()
        self.assertEqual(self.workspace.run_detail(self.project_id, self.saved["id"])["status"], "draft")

    def test_final_audit_write_failure_remains_visible_without_retry(self):
        original = self.server.browser_launch._write_private

        def fail_outcome(path, value):
            if path.name == "outcome.json":
                raise OSError("full disk")
            original(path, value)

        with patch.object(self.server.browser_launch, "_write_private", side_effect=fail_outcome), \
                patch.object(self.server.browser_launch.workspace, "launch", return_value={"runner_exit_code": 0}) as launch:
            self.assertEqual(self.request("POST", "/api/runs/launch", self.launch_body())[0], 202)
            self.server.browser_launch.thread.join(timeout=3)
            self.assertEqual(launch.call_count, 1)
        outcome = self.request("GET", self.launch_path())[1]
        self.assertEqual(outcome["evidence_status"], "unavailable")
        self.assertIsNotNone(outcome["error"])
        self.assertEqual(outcome["runner_exit_code"], 0)


class BrowserLaunchUnitTests(unittest.TestCase):
    def test_close_waits_for_active_launch_and_restart_keeps_status_unknown(self):
        from unittest.mock import Mock
        workspace = Mock()
        controller = BrowserLaunch(workspace)
        self.assertEqual(controller.observation("project", "run")["status"], "idle")
        controller.thread = Mock()
        controller.thread.is_alive.return_value = True
        controller.wait()
        controller.thread.join.assert_called_once()
        with self.assertRaises(BrowserLaunchBusy):
            workspace.run_detail.return_value = {"status": "draft"}
            controller.start("project", "run", "https://verified.modal.run", True)


if __name__ == "__main__":
    unittest.main()
