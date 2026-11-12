"""Loopback production HTTP + async jobs + real Git, with all providers mocked."""

import hashlib
import http.client
import json
import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import httpx

from swarm_control import onboarding as onboarding_module
from swarm_control.dashboard import make_server
from swarm_control.project import load_project
from tests import test_planning as planning_fixtures

GH, KEY = planning_fixtures.GH, planning_fixtures.KEY


class PlanningHTTPTests(unittest.TestCase):
    def setUp(self):
        git_environment = patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull})
        git_environment.start()
        self.addCleanup(git_environment.stop)
        # Composition avoids rerunning inherited backend test methods here.
        self.fixture = planning_fixtures.PlanningTests("test_production_payload_is_strict_bounded_and_keeps_model_key_out_of_environment")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with patch("swarm_control.connections.Connections", return_value=self.fixture.connections):
            self.server = make_server(workspace_path=self.fixture.workspace.path)
        self.server.onboarding.repositories = self.fixture.repository
        self.server.onboarding.planner_transport = httpx.MockTransport(self.fixture.http)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.csrf = self.request("GET", "/api/session")[1]["csrf_token"]

    def close(self):
        self.server.onboarding.wait()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, method, path, value=None, *, headers=None, extra_headers=()):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        raw = json.dumps(value).encode() if value is not None else b""
        actual = {"Host": f"127.0.0.1:{self.server.server_port}", "Origin": self.origin, "Content-Type": "application/json"}
        if hasattr(self, "csrf"):
            actual["X-Swarm-CSRF"] = self.csrf
        actual.update(headers or {})
        try:
            connection.putrequest(method, path, skip_host=True)
            for key, item in [*actual.items(), *extra_headers]:
                connection.putheader(key, item)
            connection.putheader("Content-Length", str(len(raw)))
            connection.endheaders(raw)
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body and response.getheader("Content-Type", "").startswith("application/json") else body
        finally:
            connection.close()

    def operation(self, action, options, *, request_id=None, confirmed=True):
        body = {"action": action, "options": options, "confirmed": confirmed, "request_id": request_id or uuid.uuid4().hex}
        code, value = self.request("POST", "/api/setup/action", body)
        self.assertEqual(code, 202, value)
        return self.await_job(body["request_id"])

    def await_job(self, request_id):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            code, value = self.request("GET", "/api/setup/job?id=" + request_id)
            self.assertEqual(code, 200, value)
            job = value["job"]
            if job["status"] in {"finished", "failed", "uncertain"}:
                # The terminal result can precede final private receipt fsync.
                # Wait for admission to reopen using GET; never replay a POST.
                code, status = self.request("GET", "/api/setup")
                self.assertEqual(code, 200, status)
                if not status["busy"]:
                    return job
            time.sleep(0.01)
        self.fail("The bounded local setup operation did not finish")

    def inspected(self):
        job = self.operation("planning.inspect", {"path": str(self.fixture.repo)})
        self.assertEqual(job["status"], "finished", job)
        return job["result"]

    def generated(self, *, request_id=None, goal="Implement independent useful behavior"):
        context = self.inspected()
        options = {"path": str(self.fixture.repo), "goal": goal, "context_binding": context["context_binding"],
                   "max_tasks": 2, "workers": 2, "plan_budget_usd": "2.00"}
        job = self.operation("planning.generate", options, request_id=request_id)
        self.assertEqual(job["status"], "finished", job)
        return job["result"], options

    def register(self):
        self.fixture.repository.initialize(str(self.fixture.repo), install=[["true"]], checks=[["true"]])
        return self.fixture.workspace.connect_project(str(self.fixture.repo / "swarm.toml"))["project_id"]

    def snapshot(self):
        paths = [self.fixture.repo / name for name in ("README.md", "main.py", "swarm.toml", "AGENTS.md", ".gitignore")]
        paths.append(self.fixture.workspace.path)
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None for path in paths}

    def test_selected_project_get_resolves_registered_path_without_jobs_providers_or_mutations(self):
        project_id = self.register()
        before = self.snapshot()
        status = self.fixture.git("status", "--porcelain=v1")
        for _ in range(2):
            code, result = self.request("GET", "/api/setup/project?project=" + project_id)
            self.assertEqual(code, 200)
            self.assertEqual(result["project_id"], project_id)
            self.assertEqual(result["path"], str(self.fixture.repo))
            self.assertEqual(result["repository"], "example/project")
        catalog = self.request("GET", "/api/catalog")[1]
        self.assertNotIn(str(self.fixture.repo), json.dumps(catalog))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fixture.git("status", "--porcelain=v1"), status)
        self.assertFalse(self.server.onboarding.root.exists())
        self.assertFalse(self.fixture.requests)
        self.assertFalse(self.fixture.issue_calls)
        self.assertFalse(self.fixture.workspace.path.with_name("workspace.planning").exists())

    def test_selected_project_get_rejects_historical_missing_and_drifted_profiles(self):
        project_id = self.register()
        self.fixture.workspace.add_project("historical", repository="example/archived")
        expected = {"error": "Setup data is unavailable. Check the selected local workspace."}
        for identity in ("historical", "unknown"):
            self.assertEqual(self.request("GET", "/api/setup/project?project=" + identity), (400, expected))
        profile = self.fixture.repo / "swarm.toml"
        original = profile.read_bytes()
        profile.unlink()
        self.assertEqual(self.request("GET", "/api/setup/project?project=" + project_id), (400, expected))
        profile.write_bytes(original)
        profile.write_text(profile.read_text().replace('repository = "example/project"', 'repository = "example/other"'))
        self.assertEqual(self.request("GET", "/api/setup/project?project=" + project_id), (400, expected))
        profile.write_bytes(original)
        self.fixture.git("remote", "set-url", "origin", "https://github.com/example/other.git")
        self.assertEqual(self.request("GET", "/api/setup/project?project=" + project_id), (400, expected))
        self.assertFalse(self.server.onboarding.root.exists())
        self.assertFalse(self.fixture.requests)
        self.assertFalse(self.fixture.issue_calls)

    def test_selected_project_query_host_and_origin_guards_reject_without_mutation(self):
        project_id = self.register()
        before = self.snapshot()
        path = "/api/setup/project?project=" + project_id
        with patch.object(self.server.onboarding, "selected_repository", wraps=self.server.onboarding.selected_repository) as selected:
            for target in ("/api/setup/project", "/api/setup/project?project=", path + "&project=" + project_id,
                           path + "&other=1", "/api/setup/project?other=" + project_id):
                self.assertEqual(self.request("GET", target)[0], 400)
            for headers in ({"Host": "foreign.invalid"}, {"Origin": "https://foreign.invalid"}):
                self.assertEqual(self.request("GET", path, headers=headers)[0], 403)
            for extra in ((("Host", "localhost:" + str(self.server.server_port)),), (("Origin", self.origin),)):
                self.assertEqual(self.request("GET", path, extra_headers=extra)[0], 403)
            selected.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.server.onboarding.root.exists())
        self.assertFalse(self.fixture.requests)

    def test_complete_async_http_inspect_generate_review_apply_registers_actual_tasks(self):
        request_id = uuid.uuid4().hex
        generated, options = self.generated(request_id=request_id)
        self.assertEqual(generated["state"], "review_required")
        self.assertTrue(generated["can_apply"])
        self.assertFalse((self.fixture.repo / "swarm.toml").exists())
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertFalse(self.fixture.issue_calls)
        apply_options = {"plan_id": generated["plan_id"], "review_binding": generated["review_binding"]}
        unconfirmed = {"action": "planning.apply", "options": apply_options, "confirmed": False, "request_id": uuid.uuid4().hex}
        self.assertEqual(self.request("POST", "/api/setup/action", unconfirmed)[0], 400)
        wrong = self.operation("planning.apply", {**apply_options, "review_binding": "0" * 64})
        self.assertEqual(wrong["status"], "failed")
        self.assertEqual(wrong["error_code"], "planning_review_changed")
        self.assertFalse(self.fixture.issue_calls)
        applied_job = self.operation("planning.apply", apply_options)
        self.assertEqual(applied_job["status"], "finished", applied_job)
        applied = applied_job["result"]
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(len(applied["task_ids"]), 2)
        self.assertEqual(len([call for call in self.fixture.issue_calls if "POST" in call]), 2)
        profile = load_project(self.fixture.repo / "swarm.toml")
        self.assertFalse(profile.live_enabled)
        self.assertEqual(profile.model_limit_cents, 0)
        self.assertEqual(profile.infra_limit_cents, 0)
        self.assertEqual(profile.max_live_agents, 2)
        code, context = self.request("GET", "/api/project?project=" + applied["project_id"])
        self.assertEqual(code, 200)
        self.assertEqual({task["id"] for task in context["tasks"]}, set(applied["task_ids"]))
        self.assertEqual(self.request("GET", "/api/setup/project?project=" + applied["project_id"])[1]["path"], str(self.fixture.repo))
        duplicate = self.operation("planning.generate", {**options, "goal": "Ignored retry fields"}, request_id=request_id)
        self.assertEqual(duplicate["result"]["plan_id"], generated["plan_id"])
        self.assertEqual(len(self.fixture.requests), 1)
        again = self.operation("planning.apply", apply_options)
        self.assertEqual(again["result"], applied)
        self.assertEqual(len([call for call in self.fixture.issue_calls if "POST" in call]), 2)
        self.assertFalse(applied["published"])
        self.assertFalse(applied["authorizes_worker_launch"])
        self.assertFalse((self.fixture.repo / ".swarm/control.sqlite3").exists())
        self.assertNotIn(KEY, json.dumps([generated, applied, context]))

    def test_generate_confirmation_and_csrf_are_enforced_before_any_call(self):
        inspected = self.inspected()
        body = {"action": "planning.generate", "options": {"path": str(self.fixture.repo), "goal": "Implement useful behavior",
                "context_binding": inspected["context_binding"], "max_tasks": 2, "workers": 2, "plan_budget_usd": "2.00"},
                "confirmed": False, "request_id": uuid.uuid4().hex}
        self.assertEqual(self.request("POST", "/api/setup/action", body)[0], 400)
        body["confirmed"] = True
        self.assertEqual(self.request("POST", "/api/setup/action", body, headers={"X-Swarm-CSRF": "bad"})[0], 403)
        self.assertFalse(self.fixture.requests)
        self.assertFalse(self.fixture.issue_calls)
        self.assertFalse(self.fixture.workspace.path.with_name("workspace.planning").exists())

    def test_known_and_credential_looking_filenames_never_reach_http_jobs_or_model_payload(self):
        values = ("as-syntheticfile-qwerty01234567890", "AKIA" + "B" * 16)
        self.fixture.connections.configure_modal("ak-synthetic-qwerty01234567890", values[0], persist=False)
        for value in values:
            path = self.fixture.repo / (value + ".py")
            path.write_text("ordinary_source = True\n")
            self.fixture.git("add", path.name)
        self.fixture.git("commit", "-m", "Synthetic filename privacy fixtures")
        inspected = self.inspected()
        generated, _ = self.generated()
        raw = json.dumps([inspected, generated])
        for value in values:
            self.assertNotIn(value, raw)
            self.assertNotIn(value.encode(), self.fixture.requests[0].content)
            for path in self.server.onboarding.root.rglob("*.json"):
                self.assertNotIn(value, path.read_text())
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertFalse(self.fixture.issue_calls)

    def test_unsafe_repository_identity_returns_fixed_safe_http_error_without_a_model_call(self):
        value = "as-syntheticrepo-qwerty01234567890"
        self.fixture.connections.configure_modal("ak-synthetic-qwerty01234567890", value, persist=False)
        self.fixture.git("remote", "set-url", "origin", "https://github.com/example/" + value + ".git")
        job = self.operation("planning.inspect", {"path": str(self.fixture.repo)})
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "planning_unsafe_source")
        self.assertNotIn(value, json.dumps(job))
        self.assertIsNone(job["result"])
        self.assertFalse(self.fixture.requests)
        self.assertFalse(self.fixture.workspace.path.with_name("workspace.planning").exists())

    def test_source_or_account_drift_blocks_http_apply_before_remote_issue_writes(self):
        generated, _ = self.generated()
        options = {"plan_id": generated["plan_id"], "review_binding": generated["review_binding"]}
        (self.fixture.repo / "main.py").write_text("concurrent source change\n")
        job = self.operation("planning.apply", options)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "planning_source_changed")
        self.fixture.git("checkout", "--", "main.py")
        configured = self.operation("environment.configure", {"dotenv_text": "GH_TOKEN=ghp_rotated_fixture_0123456789", "persist": False})
        self.assertEqual(configured["status"], "finished")
        verified = self.operation("connections.verify", {"provider": "github"})
        self.assertEqual(verified["status"], "finished")
        job = self.operation("planning.apply", options)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "planning_accounts_changed")
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertFalse(self.fixture.issue_calls)
        self.assertFalse((self.fixture.repo / "swarm.toml").exists())

    def test_failed_http_generation_exposes_safe_retained_id_and_never_retries(self):
        def failure(request):
            self.fixture.requests.append(request)
            raise httpx.ReadTimeout("private provider failure " + KEY)
        self.server.onboarding.planner_transport = httpx.MockTransport(failure)
        inspected = self.inspected()
        options = {"path": str(self.fixture.repo), "goal": "Implement useful behavior", "context_binding": inspected["context_binding"],
                   "max_tasks": 2, "workers": 2, "plan_budget_usd": "2.00"}
        request_id = uuid.uuid4().hex
        failed = self.operation("planning.generate", options, request_id=request_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "planning_call_unknown")
        plan_id = failed["result"]["plan_id"]
        self.assertRegex(plan_id, r"^[a-f0-9]{32}$")
        self.assertEqual(failed["result"]["planning_reservation"]["state"], "retained")
        self.assertEqual(self.operation("planning.generate", options, request_id=request_id), failed)
        another = self.operation("planning.generate", options)
        self.assertEqual(another["error_code"], "planning_attempt_held")
        self.assertEqual(another["result"]["plan_id"], plan_id)
        inspected_attempt = self.operation("planning.get", {"plan_id": plan_id})
        self.assertEqual(inspected_attempt["status"], "finished")
        self.assertEqual(inspected_attempt["result"]["state"], "uncertain")
        self.assertEqual(len(self.fixture.requests), 1)
        history = self.request("GET", "/api/setup")[1]
        raw = json.dumps([failed, another, inspected_attempt, history])
        for value in (KEY, GH, "private provider failure", "source_excerpts", "account_binding"):
            self.assertNotIn(value, raw)
        for path in self.server.onboarding.root.rglob("*.json"):
            self.assertNotIn(KEY, path.read_text())
        self.assertFalse(self.fixture.issue_calls)

    def test_terminal_http_result_stays_running_until_private_outcome_is_durable(self):
        entered, release = threading.Event(), threading.Event()
        original = onboarding_module.private_json
        operation_id = uuid.uuid4().hex
        def delayed(path, value):
            if path.name == "outcome.json":
                entered.set()
                self.assertTrue(release.wait(5))
            return original(path, value)
        with patch("swarm_control.onboarding.private_json", side_effect=delayed):
            body = {"action": "planning.inspect", "options": {"path": str(self.fixture.repo)}, "confirmed": True, "request_id": operation_id}
            try:
                self.assertEqual(self.request("POST", "/api/setup/action", body)[0], 202)
                self.assertTrue(entered.wait(3))
                for _ in range(2):
                    job = self.request("GET", "/api/setup/job?id=" + operation_id)[1]["job"]
                    self.assertEqual(job["status"], "running")
                    self.assertIsNone(job["result"])
                    self.assertNotIn("finished_at_utc", job)
                self.assertTrue(self.request("GET", "/api/setup")[1]["busy"])
                self.assertFalse((self.server.onboarding.root / operation_id / "outcome.json").exists())
            finally:
                release.set()
            job = self.await_job(operation_id)
        self.assertEqual(job["status"], "finished")
        saved = json.loads((self.server.onboarding.root / operation_id / "outcome.json").read_text())
        self.assertEqual(saved["status"], "finished")
        self.assertEqual(saved["result"], job["result"])
        self.assertFalse(self.fixture.requests)

    def test_failed_outcome_write_never_publishes_success_and_keeps_plan_inspectable(self):
        project_id = self.register()
        inspected_job = self.operation("planning.inspect", {"project_id": project_id})
        self.assertEqual(inspected_job["status"], "finished")
        inspected = inspected_job["result"]
        options = {"project_id": project_id, "goal": "Implement useful behavior", "context_binding": inspected["context_binding"],
                   "max_tasks": 2, "workers": 2, "plan_budget_usd": "2.00"}
        original = onboarding_module.private_json
        operation_id = uuid.uuid4().hex
        seen = []
        def failed_outcome(path, value):
            if path.name == "outcome.json":
                seen.append(self.server.onboarding.job(operation_id)["status"])
                raise OSError("private storage failure " + KEY)
            return original(path, value)
        with patch("swarm_control.onboarding.private_json", side_effect=failed_outcome):
            job = self.operation("planning.generate", options, request_id=operation_id)
        self.assertEqual(seen, ["running"])
        self.assertEqual(job["status"], "uncertain")
        self.assertEqual(job["error_code"], "setup_evidence_unavailable")
        self.assertEqual(job["result"]["state"], "uncertain")
        self.assertFalse(job["result"]["can_apply"])
        self.assertRegex(job["result"]["plan_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(job["result"]["path"], str(self.fixture.repo))
        self.assertEqual(job["result"]["project_id"], project_id)
        self.assertEqual(job["result"]["repository"], "example/project")
        self.assertEqual(set(job["result"]), {"plan_id", "state", "can_apply", "path", "project_id", "repository"})
        # Planner history matches explicit selected project identity. Its later
        # readJob remains sufficient to offer an Inspect retained plan button.
        recorded = self.request("GET", "/api/setup/job?id=" + operation_id)[1]["job"]
        self.assertEqual(recorded["result"]["project_id"], project_id)
        self.assertEqual(recorded["result"]["plan_id"], job["result"]["plan_id"])
        self.assertFalse((self.server.onboarding.root / operation_id / "outcome.json").exists())
        self.assertNotIn(KEY, json.dumps(job))
        self.assertNotIn("private storage failure", json.dumps(job))
        self.assertEqual(len(self.fixture.requests), 1)
        retained = self.operation("planning.get", {"plan_id": job["result"]["plan_id"]})
        self.assertEqual(retained["result"]["state"], "review_required")
        self.assertEqual(len(self.fixture.requests), 1)
        self.assertFalse(self.fixture.issue_calls)


if __name__ == "__main__":
    unittest.main()
