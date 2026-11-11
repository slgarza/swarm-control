"""Real HTTP tests for local draft mutation, CSRF and repository selection."""

import http.client
import json
import threading
from copy import deepcopy
from unittest.mock import MagicMock, patch

from swarm_control.dashboard import make_server
from tests.test_dashboard import SnapshotFixture


class LocalWorkspace:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []
        self.catalog = {"mode": "workspace", "workspace_label": "Local workspace",
                        "projects": [{"id": "first", "label": "First", "repository": "example/project",
                                      "runs": [{"id": "past", "label": "Past run", "status": "historical"}]}],
                        "default_project_id": "first", "default_run_id": "past"}

    def public_catalog(self):
        return deepcopy(self.catalog)

    def project_context(self, project):
        if project != "first":
            raise ValueError("unknown project")
        return {"project_id": project, "configurable": True, "limits": {"max_workers": 10}, "tasks": []}

    def prepare_run(self, project, options, *, save=False):
        if project != "first" or options.get("workers") != 2:
            raise ValueError("bad configuration")
        self.calls.append((project, options, save))
        return {"id": "new" if save else None, "status": "draft" if save else "preview",
                "project_id": project, "workers": 2, "authorizes_spend": False}

    def run_detail(self, project, run):
        if project != "first" or run != "past":
            raise ValueError("unknown run")
        return {"id": run, "status": "historical", "has_snapshot": True}

    def historical_snapshot(self, project, run):
        if project != "first" or run != "past":
            raise ValueError("unknown run")
        return self.snapshot, "example/project", project, run


class WorkspaceHTTPTests(SnapshotFixture):
    def setUp(self):
        super().setUp()
        self.workspace = LocalWorkspace(self.path)
        with patch("swarm_control.workspace.Workspace", return_value=self.workspace):
            self.server = make_server(workspace_path=self.root / "workspace.json")
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        code, session = self.request("GET", "/api/session")
        self.assertEqual(code, 200)
        self.token = session["csrf_token"]

        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
        self.addCleanup(cleanup)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            result = connection.getresponse()
            raw = result.read()
            self.assertNotIn(str(self.root).encode(), raw)
            return result.status, json.loads(raw) if raw else None
        finally:
            connection.close()

    def post(self, *, path="/api/runs/preview", data=None, raw=None, changes=None, method="POST"):
        headers = {"Origin": self.origin, "X-Swarm-CSRF": self.token, "Content-Type": "application/json"}
        headers.update(changes or {})
        body = raw if raw is not None else json.dumps(data or {"project_id": "first", "options": {"workers": 2}})
        return self.request(method, path, body, headers)

    def test_workspace_mode_exposes_only_local_preparation(self):
        code, session = self.request("GET", "/api/session")
        self.assertEqual(code, 200)
        self.assertEqual(session["mode"], "workspace")
        self.assertFalse(session["deployment_enabled"])
        self.assertEqual(self.request("GET", "/api/catalog")[1]["mode"], "workspace")
        self.assertEqual(self.request("GET", "/api/project?project=first")[0], 200)
        self.assertEqual(self.request("GET", "/api/runs?project=first")[1]["runs"][0]["id"], "past")
        self.assertTrue(self.request("GET", "/api/run?project=first&run=past")[1]["has_snapshot"])
        self.assertTrue(self.request("GET", "/api/snapshot?project=first&run=past")[1]["available"])

    def test_preview_and_save_have_distinct_mutation_scope(self):
        self.assertEqual(self.post()[0], 200)
        code, saved = self.post(path="/api/runs")
        self.assertEqual(code, 201)
        self.assertEqual(saved["status"], "draft")
        self.assertFalse(saved["authorizes_spend"])
        self.assertEqual([call[2] for call in self.workspace.calls], [False, True])

    def test_wrong_origin_or_token_and_cross_site_are_rejected_before_preparation(self):
        for changes in ({"Origin": "https://foreign.invalid"}, {"Origin": "null"},
                        {"Origin": ""}, {"X-Swarm-CSRF": "wrong"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(changes=changes)[0], 403)
        self.assertEqual(self.workspace.calls, [])
        self.assertEqual(self.request("GET", "/api/session", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_unknown_fields_duplicate_keys_nonfinite_or_oversized_body_cannot_write(self):
        for body in ('{"project_id":"first","project_id":"other","options":{"workers":2}}',
                     '{"project_id":"first","options":{"workers":NaN}}',
                     '{"project_id":"first","options":{},"command":"run"}',
                     '[]', '{broken', 'x' * 65537):
            with self.subTest(size=len(body)):
                self.assertEqual(self.post(path="/api/runs", raw=body)[0], 400)
        self.assertEqual(self.workspace.calls, [])
        self.assertEqual(self.post(changes={"Content-Type": "text/plain"})[0], 400)

    def test_no_deployment_route_or_alternate_write_verb_exists(self):
        self.assertEqual(self.post(path="/api/launch")[0], 404)
        self.assertEqual(self.post(method="PUT")[0], 405)
        self.assertEqual(self.post(path="/api/runs?execute=true")[0], 404)
        self.assertEqual(self.workspace.calls, [])

    def test_cross_project_and_arbitrary_paths_stay_invalid(self):
        for target in ("/api/project?project=other", "/api/project?path=/private/file",
                       "/api/project?project=first&project=other", "/api/run?project=first&run=../private",
                       "/api/run?project=first", "/api/runs?project=first&run=past"):
            with self.subTest(target=target):
                self.assertEqual(self.request("GET", target)[0], 400)

    def test_catalog_is_reread_without_restart(self):
        self.workspace.catalog["projects"][0]["runs"].append({"id": "new", "label": "New draft", "status": "draft"})
        self.assertEqual(len(self.request("GET", "/api/runs?project=first")[1]["runs"]), 2)

    def test_aborted_fetch_during_headers_does_not_attempt_another_response(self):
        handler = object.__new__(self.server.RequestHandlerClass)
        handler.command, handler.close_connection = "GET", False
        handler.send_response, handler.send_header = MagicMock(), MagicMock()
        handler.end_headers = MagicMock(side_effect=BrokenPipeError)
        handler.wfile = MagicMock()
        handler.respond(200, b'{"ok":true}')
        self.assertTrue(handler.close_connection)
        handler.send_response.assert_called_once_with(200)
        handler.wfile.write.assert_not_called()

    def test_reset_during_body_does_not_escape_the_request_handler(self):
        handler = object.__new__(self.server.RequestHandlerClass)
        handler.command, handler.close_connection = "GET", False
        handler.send_response, handler.send_header, handler.end_headers = MagicMock(), MagicMock(), MagicMock()
        handler.wfile = MagicMock()
        handler.wfile.write.side_effect = ConnectionResetError
        handler.respond(200, b'{"ok":true}')
        self.assertTrue(handler.close_connection)
        handler.send_response.assert_called_once_with(200)
