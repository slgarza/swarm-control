"""Real temporary Git + mocked fixed-endpoint HTTP; no paid/provider requests."""

import copy
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from swarm_control.connections import Connections
from swarm_control.onboarding import Onboarding
from swarm_control.planning import ENDPOINT, MAX_OUTPUT_TOKENS, Planner, PlanStore
from swarm_control.project import load_project
from swarm_control.repository_setup import RepositorySetup
from swarm_control.setup_common import SetupError
from swarm_control.workspace import Workspace
from tests.test_connections import FakeRunner

KEY = "sk-planner-fixture-0123456789"
GH = "ghp_planner_fixture_0123456789"


def proposal(count=2, *, chain=False, overlap=False):
    return {"summary": "Implement the concrete requested behavior with reviewed tests.",
            "install": [["python3", "-m", "pip", "install", "-e", "."]],
            "checks": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
            "instructions": ["README.md", "AGENTS.md"],
            "tasks": [{"id": f"T{index:03}", "title": f"Implement behavior {index}",
                       "prompt": f"Implement behavior {index} and its regression test within the stated ownership.",
                       "ownership": ["shared.py" if overlap else f"src/feature_{index}.py"],
                       "acceptance": [f"Behavior {index} passes its independently reviewed regression check."],
                       "dependsOn": [f"T{index-1:03}"] if chain and index > 1 else []}
                      for index in range(1, count + 1)]}


def response(plan=None, **changes):
    return {"id": "resp_fixture", "status": "completed", "error": None, "incomplete_details": None,
            "output": [{"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(plan or proposal())}]}],
            "usage": {"input_tokens": 1200, "output_tokens": 700}, **changes}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.repo = self.folder / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Offline planner")
        self.git("config", "user.email", "planner@example.invalid")
        self.git("remote", "add", "origin", "https://github.com/example/project.git")
        (self.repo / "README.md").write_text("Original project. Implement behavior without invoking scripts.\n")
        (self.repo / "main.py").write_text("original = True\n")
        self.git("add", ".")
        self.git("commit", "-m", "Original source")
        self.workspace = Workspace(self.folder / "private/workspace.json")
        self.connections = Connections(self.workspace.path, base_environment={"PATH": os.environ["PATH"]}, runner=FakeRunner())
        self.connections.configure_environment(f"GH_TOKEN={GH}\nCODEX_API_KEY={KEY}\n", persist=False)
        self.connections.verify("github")
        self.repository = RepositorySetup(self.workspace, environment=self.connections.environment())
        self.requests = []
        self.reply = response()
        self.issue_calls = []
        self.issue_number = 100
        self.service = Planner(self.workspace, self.connections, self.repository, transport=httpx.MockTransport(self.http))
        original = self.repository._run

        def command(args, *positional, **kwargs):
            if args[0] != "gh":
                return original(args, *positional, **kwargs)
            self.issue_calls.append(args)
            if "POST" in args:
                self.issue_number += 1
                return json.dumps({"number": self.issue_number, "title": "Created behavior"}).encode()
            return json.dumps({"number": int(args[2].split("/")[-1]), "state": "open", "title": "Created behavior"}).encode()

        self.mock_commands = patch.object(self.repository, "_run", side_effect=command)
        self.mock_commands.start()
        self.addCleanup(self.mock_commands.stop)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.PIPE, text=True).strip()

    def http(self, request):
        self.requests.append(request)
        return httpx.Response(200, json=copy.deepcopy(self.reply))

    def inspect(self):
        return self.service.inspect(str(self.repo))

    def generate(self, **options):
        values = {"path": str(self.repo), "goal": "Implement independent feature behaviors.",
                  "context_binding": self.inspect()["context_binding"], "max_tasks": 2, "workers": 2,
                  "plan_budget_usd": "2.00", **options}
        return self.service.generate(**values)

    def assert_code(self, code, callback):
        with self.assertRaises(SetupError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_inspection_is_read_only_and_context_excludes_secrets_symlinks_binary_and_untracked(self):
        (self.repo / ".env").write_text("CODEX_API_KEY=" + KEY)
        (self.repo / "credentials.json").write_text('{"key":"' + KEY + '"}')
        (self.repo / "binary.dat").write_bytes(b"\x00" + KEY.encode())
        (self.repo / "untracked.py").write_text("untracked private source")
        (self.repo / "linked.py").symlink_to(self.folder / "outside.py")
        (self.folder / "outside.py").write_text("outside private source")
        (self.repo / "main.py").write_text("value = '" + KEY + "'\n")
        self.git("add", ".env", "credentials.json", "binary.dat", "linked.py", "main.py")
        value = self.inspect()
        self.assertFalse((self.repo / "swarm.toml").exists())
        self.assertFalse(self.workspace.path.parent.exists())
        self.assertFalse(self.requests)
        self.assertFalse(self.issue_calls)
        self.assertLessEqual(value["selected_bytes"], 65536)
        self.assertEqual(value["readiness"]["github_identity"], "fixture-user")
        self.generate()
        body = json.loads(self.requests[0].content)
        self.assertEqual(str(self.requests[0].url), ENDPOINT)
        self.assertEqual(self.requests[0].headers["authorization"], "Bearer " + KEY)
        serialized = json.dumps(body)
        for text in (KEY, GH, "untracked private source", "outside private source", ".env", "credentials.json"):
            self.assertNotIn(text, serialized)
        self.assertIn("[redacted]", serialized)
        self.assertNotIn(KEY, json.dumps(self.service.get(self.last_plan_id())))

    def last_plan_id(self):
        store = PlanStore(self.workspace.path)
        with store._connection() as connection:
            return connection.execute("SELECT id FROM plans ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    def test_production_payload_is_strict_bounded_and_keeps_model_key_out_of_environment(self):
        value = self.generate()
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["model"], "gpt-6.1-sol")
        self.assertFalse(body["store"])
        self.assertEqual(body["service_tier"], "default")
        self.assertEqual(body["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["text"]["format"]["schema"]["additionalProperties"])
        self.assertEqual(value["state"], "review_required")
        self.assertTrue(value["can_apply"])
        self.assertEqual(value["analysis"]["parallel_frontier"], 2)
        self.assertIsNone(value["usage"]["invoice_cost_usd"])
        self.assertLess(value["usage"]["estimated_cost_usd"], value["planning_reservation"]["reserved_usd"])
        self.assertEqual(value["planning_reservation"]["state"], "retained")
        self.assertNotIn("CODEX_API_KEY", self.connections.environment())
        self.assertFalse(self.issue_calls)
        self.assertFalse((self.repo / "swarm.toml").exists())

    def test_credential_bearing_filenames_are_bound_locally_but_never_uploaded_or_returned(self):
        modal_value = "as-syntheticfile-qwerty01234567890"
        aws_value = "AKIA" + "A" * 16
        self.connections.configure_modal("ak-synthetic-qwerty01234567890", modal_value, persist=False)
        for name in (modal_value + ".py", aws_value + ".py"):
            (self.repo / name).write_text("ordinary_source = True\n")
            self.git("add", name)
        self.git("commit", "-m", "Synthetic credential-bearing path fixtures")
        inspected = self.inspect()
        for value in (modal_value, aws_value):
            self.assertNotIn(value, json.dumps(inspected))
        generated = self.generate()
        for value in (modal_value, aws_value):
            self.assertNotIn(value.encode(), self.requests[0].content)
            self.assertNotIn(value, json.dumps(generated))
        # Exclusion must not permit unnoticed edits while applying the plan.
        (self.repo / (modal_value + ".py")).write_text("changed_source = True\n")
        self.assert_code("planning_source_changed", lambda: self.service.apply(generated["plan_id"], generated["review_binding"]))
        self.assertFalse(self.issue_calls)

    def test_credential_bearing_repository_identity_fails_before_reservation_or_upload(self):
        value = "as-syntheticrepo-qwerty01234567890"
        self.connections.configure_modal("ak-synthetic-qwerty01234567890", value, persist=False)
        self.fixture_identity = "example/" + value
        self.git("remote", "set-url", "origin", "https://github.com/" + self.fixture_identity + ".git")
        self.assert_code("planning_unsafe_source", self.inspect)
        self.assert_code("planning_unsafe_source", lambda: self.service.generate(str(self.repo), "Safe goal", "0" * 64, 2, 2, "2.00"))
        self.assertFalse(self.requests)
        self.assertFalse(self.workspace.path.parent.exists())
        self.assertFalse(self.issue_calls)

    def test_insufficient_allowance_and_changed_context_do_not_reserve_or_send(self):
        self.assert_code("planning_budget_insufficient", lambda: self.generate(plan_budget_usd="0.01"))
        binding = self.inspect()["context_binding"]
        (self.repo / "main.py").write_text("changed = True\n")
        self.assert_code("planning_source_changed", lambda: self.generate(context_binding=binding))
        self.assertFalse(self.requests)
        self.assertFalse(self.workspace.path.parent.exists())

    def test_missing_local_key_and_unverified_github_do_not_spend(self):
        empty = Connections(self.folder / "empty/workspace.json", base_environment={"OPENAI_API_KEY": KEY}, runner=FakeRunner())
        planner = Planner(self.workspace, empty, self.repository, transport=httpx.MockTransport(self.http))
        self.assert_code("planning_key_required", lambda: planner.generate(str(self.repo), "Goal", self.inspect()["context_binding"], 2, 2, "2.00"))
        empty.configure_environment(f"CODEX_API_KEY={KEY}", persist=False)
        self.assert_code("planning_github_required", lambda: planner.generate(str(self.repo), "Goal", self.inspect()["context_binding"], 2, 2, "2.00"))
        self.assertFalse(self.requests)

    def test_tokenless_ambient_github_identity_cannot_authorize_planning(self):
        ambient = Connections(self.folder / "ambient/workspace.json", base_environment={}, runner=FakeRunner())
        ambient.configure_environment("CODEX_API_KEY=" + KEY, persist=False)
        ambient.verify("github")
        self.assertTrue(ambient.status()["github"]["verified"])
        self.assertFalse(ambient.status()["github"]["planning_credential_captured"])
        planner = Planner(self.workspace, ambient, self.repository, transport=httpx.MockTransport(self.http))
        self.assert_code("planning_github_token_required", lambda: planner.generate(str(self.repo), "Goal", self.inspect()["context_binding"], 2, 2, "2.00"))
        self.assertFalse(self.requests)
        self.assertFalse(self.workspace.path.parent.exists())

    def test_completed_identical_generation_reuses_plan_without_another_call(self):
        one = self.generate()
        two = self.generate(plan_budget_usd="3.00")
        self.assertEqual(one, two)
        self.assertEqual(len(self.requests), 1)

    def test_uncertain_transport_is_retained_across_restart_and_never_retried(self):
        def fail(request):
            self.requests.append(request)
            raise httpx.ReadTimeout("private provider failure " + KEY)
        self.service.transport = httpx.MockTransport(fail)
        self.assert_code("planning_call_unknown", self.generate)
        original = self.service.get(self.last_plan_id())
        self.assertEqual(original["state"], "uncertain")
        self.assertEqual(original["planning_reservation"]["state"], "retained")
        self.service = Planner(self.workspace, self.connections, self.repository, transport=httpx.MockTransport(self.http))
        with self.assertRaises(SetupError) as held:
            self.generate()
        self.assertEqual(held.exception.code, "planning_attempt_held")
        self.assertEqual(held.exception.result["plan_id"], original["plan_id"])
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(KEY, json.dumps(original))

    def test_process_loss_after_durable_reservation_is_held_without_another_call(self):
        child = os.fork()
        if child == 0:
            self.service.transport = httpx.MockTransport(lambda _: os._exit(19))
            try:
                self.generate()
            finally:
                os._exit(20)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 19)
        held = self.service.get(self.last_plan_id())
        self.assertEqual(held["state"], "reserved")
        self.assertEqual(held["planning_reservation"]["state"], "retained")
        self.assert_code("planning_attempt_held", self.generate)
        self.assertFalse(self.requests)

    def test_missing_account_binding_key_or_budget_journal_does_not_reset_authorization(self):
        generated = self.generate()
        store = PlanStore(self.workspace.path)
        original_secret = (store.root / "binding-secret").read_bytes()
        (store.root / "binding-secret").unlink()
        self.assert_code("planning_storage_unsafe", self.generate)
        (store.root / "binding-secret").write_bytes(original_secret)
        (store.root / "binding-secret").chmod(0o600)
        self.assertEqual(self.service.get(generated["plan_id"])["state"], "review_required")
        store.database.unlink()
        self.assert_code("planning_storage_unsafe", self.generate)
        self.assertFalse(store.database.exists())
        self.assertEqual(len(self.requests), 1)

    def test_two_threads_cannot_send_the_same_candidate_twice(self):
        entered, release = threading.Event(), threading.Event()
        def slow(request):
            self.requests.append(request)
            entered.set()
            self.assertTrue(release.wait(3))
            return httpx.Response(200, json=self.reply)
        self.service.transport = httpx.MockTransport(slow)
        result = []
        thread = threading.Thread(target=lambda: result.append(self.generate()))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            self.assert_code("planning_attempt_held", self.generate)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result[0]["state"], "review_required")

    def test_refusal_incomplete_malformed_and_oversized_responses_fail_before_mutation(self):
        for name, reply in (("refused", response(output=[{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "No"}]}])),
                            ("incomplete", response(status="incomplete")),
                            ("schema", response({"summary": "missing fields"})),
                            ("badpath", response({**proposal(), "instructions": ["../../outside"]}))):
            with self.subTest(name=name):
                self.reply = reply
                self.assert_code("planning_invalid_response", lambda name=name: self.generate(goal="Goal " + name))
        self.service.transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)))
        self.assert_code("planning_invalid_response", lambda: self.generate(goal="Goal oversized"))
        self.assertFalse((self.repo / "swarm.toml").exists())
        self.assertFalse(self.issue_calls)

    def test_100_tasks_and_workers_supported_without_fabricated_independence(self):
        self.reply = response(proposal(100))
        value = self.generate(max_tasks=100, workers=100)
        self.assertEqual(len(value["plan"]["tasks"]), 100)
        self.assertEqual(value["analysis"]["parallel_frontier"], 100)
        self.assertTrue(value["can_apply"])
        self.reply = response(proposal(100, chain=True))
        chained = self.generate(goal="An explicitly sequential release pipeline", max_tasks=100, workers=100)
        self.assertEqual(chained["analysis"]["parallel_frontier"], 1)
        self.assertFalse(chained["can_apply"])
        self.assert_code("planning_validation_blocked", lambda: self.service.apply(chained["plan_id"], chained["review_binding"]))
        self.assertFalse(self.issue_calls)

    def test_100_task_materialization_keeps_100_capacity_without_enabling_spend(self):
        self.reply = response(proposal(100))
        generated = self.generate(max_tasks=100, workers=100)
        applied = self.service.apply(generated["plan_id"], generated["review_binding"])
        self.assertEqual(len(applied["task_ids"]), 100)
        self.assertEqual(len([call for call in self.issue_calls if "POST" in call]), 100)
        project = load_project(self.repo / "swarm.toml")
        self.assertEqual(len(list(project.tasks_dir.glob("*.json"))), 100)
        self.assertEqual(project.max_live_agents, 100)
        self.assertEqual(project.gateway_concurrency, 100)
        self.assertFalse(project.live_enabled)
        self.assertEqual(project.model_limit_cents, 0)

    def test_cycles_unknown_dependencies_and_ownership_conflicts_hold_apply(self):
        cases = [proposal(overlap=True), proposal(), proposal()]
        cases[1]["tasks"][0]["dependsOn"] = ["UNKNOWN"]
        cases[2]["tasks"][0]["dependsOn"] = ["T002"]
        cases[2]["tasks"][1]["dependsOn"] = ["T001"]
        for index, plan in enumerate(cases):
            self.reply = response(plan)
            result = self.generate(goal=f"Held plan case {index}")
            self.assertFalse(result["can_apply"])
            self.assertTrue(result["analysis"]["errors"])
            self.assert_code("planning_validation_blocked", lambda result=result: self.service.apply(result["plan_id"], result["review_binding"]))
        self.assertFalse(self.issue_calls)

    def test_apply_materializes_real_files_open_issue_gates_disabled_profile_and_registered_tasks(self):
        self.reply = response(proposal(chain=True))
        generated = self.generate(workers=1)
        applied = self.service.apply(generated["plan_id"], generated["review_binding"])
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(len(applied["task_ids"]), 2)
        self.assertEqual(len([call for call in self.issue_calls if "POST" in call]), 2)
        self.assertEqual(len([call for call in self.issue_calls if "POST" not in call]), 2)
        project = load_project(self.repo / "swarm.toml")
        self.assertFalse(project.live_enabled)
        self.assertEqual(project.model_limit_cents, 0)
        self.assertEqual(project.infra_limit_cents, 0)
        one, two = [json.loads((project.tasks_dir / (task_id + ".json")).read_text()) for task_id in applied["task_ids"]]
        self.assertEqual(two["dependsOn"], [one["id"]])
        self.assertTrue(all(task["status"] == "ready" and task["issueUrl"].startswith("https://github.com/example/project/issues/") for task in (one, two)))
        context = self.workspace.project_context(applied["project_id"])
        self.assertEqual({task["id"] for task in context["tasks"]}, set(applied["task_ids"]))
        self.assertFalse(applied["published"])
        self.assertFalse(applied["authorizes_worker_launch"])
        calls = len(self.issue_calls)
        self.assertEqual(self.service.apply(generated["plan_id"], generated["review_binding"]), applied)
        self.assertEqual(len(self.issue_calls), calls)

    def test_existing_profile_commands_and_versioned_tasks_are_preserved(self):
        self.repository.initialize(str(self.repo), install=[["true"]], checks=[["true"]])
        old = {"id": "OLD01", "title": "Published historical scope", "prompt": "Preserve it", "ownership": ["old.py"], "acceptance": ["Old criterion"], "status": "blocked"}
        self.repository.save_task(str(self.repo), old)
        self.git("add", ".")
        self.git("commit", "-m", "Reviewed profile and historical tasks")
        original = (self.repo / "swarm.toml").read_bytes()
        old_raw = (self.repo / "swarm/tasks/OLD01.json").read_bytes()
        value = self.generate()
        self.assertEqual(value["plan"]["checks"], [["true"]])
        applied = self.service.apply(value["plan_id"], value["review_binding"])
        self.assertFalse(applied["profile_created"])
        self.assertEqual((self.repo / "swarm.toml").read_bytes(), original)
        self.assertEqual((self.repo / "swarm/tasks/OLD01.json").read_bytes(), old_raw)

    def test_apply_rejects_review_source_account_or_record_drift_before_issue_writes(self):
        value = self.generate()
        self.assert_code("planning_review_changed", lambda: self.service.apply(value["plan_id"], "0" * 64))
        (self.repo / "main.py").write_text("source drift\n")
        self.assert_code("planning_source_changed", lambda: self.service.apply(value["plan_id"], value["review_binding"]))
        self.git("checkout", "--", "main.py")
        self.connections.configure_environment("CODEX_API_KEY=sk-replacement-fixture-0123456789", persist=False)
        self.assert_code("planning_accounts_changed", lambda: self.service.apply(value["plan_id"], value["review_binding"]))
        self.connections.configure_environment("CODEX_API_KEY=" + KEY, persist=False)
        store = PlanStore(self.workspace.path)
        changed = store.get(value["plan_id"])
        changed["plan"]["tasks"][0]["ownership"] = ["altered.py"]
        store.update(changed, "test_original_drift")
        self.assert_code("planning_review_changed", lambda: self.service.apply(value["plan_id"], value["review_binding"]))
        self.assertFalse(self.issue_calls)
        self.assertFalse((self.repo / "swarm.toml").exists())

    def test_untracked_existing_profile_content_is_also_bound(self):
        self.repository.initialize(str(self.repo), install=[["true"]], checks=[["true"]])
        value = self.generate()
        config = self.repo / "swarm.toml"
        config.write_text(config.read_text().replace('checks = [["true"]]', 'checks = [["false"]]'))
        self.assert_code("planning_source_changed", lambda: self.service.apply(value["plan_id"], value["review_binding"]))
        self.assertFalse(self.issue_calls)

    def test_uncertain_issue_post_and_known_issue_save_failure_never_duplicate_on_restart(self):
        generated = self.generate()
        with patch.object(self.repository, "save_task", side_effect=OSError("local disk failed")):
            self.assert_code("planning_apply_held", lambda: self.service.apply(generated["plan_id"], generated["review_binding"]))
        held = self.service.get(generated["plan_id"])
        self.assertEqual(held["state"], "apply_uncertain")
        self.assertEqual(held["apply_progress"][0]["state"], "issue_created")
        self.assertEqual(held["apply_progress"][0]["number"], 101)
        self.service = Planner(self.workspace, self.connections, self.repository, transport=httpx.MockTransport(self.http))
        self.assert_code("planning_apply_held", lambda: self.service.apply(generated["plan_id"], generated["review_binding"]))
        self.assertEqual(len([call for call in self.issue_calls if "POST" in call]), 1)

    def test_uncertain_issue_response_is_journaled_before_remote_call_and_never_repeated(self):
        value = self.generate()
        calls = []
        def lost(path, title, body, *, confirmed):
            record = PlanStore(self.workspace.path).get(value["plan_id"])
            self.assertEqual(record["state"], "applying")
            self.assertEqual(record["apply_progress"][0]["state"], "issue_creation_reserved")
            calls.append(True)
            raise TimeoutError("unknown remote write " + KEY)
        with patch.object(self.repository, "create_issue", side_effect=lost):
            self.assert_code("planning_apply_held", lambda: self.service.apply(value["plan_id"], value["review_binding"]))
            self.assert_code("planning_apply_held", lambda: self.service.apply(value["plan_id"], value["review_binding"]))
        self.assertEqual(calls, [True])
        held = self.service.get(value["plan_id"])
        self.assertEqual(held["apply_progress"][0]["state"], "issue_creation_reserved")
        self.assertIsNone(held["apply_progress"][0]["number"])
        self.assertNotIn(KEY, json.dumps(held))

    def test_unrelated_source_edit_during_issue_response_is_not_absorbed(self):
        value = self.generate()
        original = self.repository.create_issue
        def concurrent_edit(*args, **kwargs):
            issue = original(*args, **kwargs)
            (self.repo / "main.py").write_text("unrelated concurrent edit\n")
            return issue
        with patch.object(self.repository, "create_issue", side_effect=concurrent_edit):
            self.assert_code("planning_apply_held", lambda: self.service.apply(value["plan_id"], value["review_binding"]))
        held = self.service.get(value["plan_id"])
        self.assertEqual(held["apply_error_code"], "planning_source_changed")
        self.assertEqual(held["apply_progress"][0]["state"], "issue_created")
        self.assertEqual(len([call for call in self.issue_calls if "POST" in call]), 1)
        self.assertEqual(list((self.repo / "swarm/tasks").glob("*.json")), [])

    def test_unrelated_edit_during_initialize_or_save_is_not_absorbed(self):
        for method in ("initialize", "save_task"):
            with self.subTest(method=method):
                value = self.generate(goal="Hold concurrent " + method)
                original = getattr(self.repository, method)
                before_calls = len([call for call in self.issue_calls if "POST" in call])
                def concurrent_edit(*args, original=original, method=method, **kwargs):
                    result = original(*args, **kwargs)
                    (self.repo / "main.py").write_text("unrelated " + method + " edit\n")
                    return result
                with patch.object(self.repository, method, side_effect=concurrent_edit):
                    self.assert_code("planning_apply_held", lambda value=value: self.service.apply(value["plan_id"], value["review_binding"]))
                held = self.service.get(value["plan_id"])
                self.assertEqual(held["apply_error_code"], "planning_source_changed")
                after_calls = len([call for call in self.issue_calls if "POST" in call])
                self.assertEqual(after_calls - before_calls, 0 if method == "initialize" else 1)
                self.git("checkout", "--", "main.py")

    def test_failed_generation_job_returns_safe_attempt_identity_for_explicit_inspection(self):
        def fail(_):
            raise httpx.ReadTimeout("provider private error " + KEY)
        onboarding = Onboarding(self.workspace, connections=self.connections, repositories=self.repository,
                                planner_transport=httpx.MockTransport(fail))
        values = {"path": str(self.repo), "goal": "Implement useful behavior", "context_binding": self.inspect()["context_binding"],
                  "max_tasks": 2, "workers": 2, "plan_budget_usd": "2.00"}
        job = onboarding.start("planning.generate", values, True, "a" * 32)
        onboarding.wait()
        failed = onboarding.job(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "planning_call_unknown")
        self.assertRegex(failed["result"]["plan_id"], r"^[a-f0-9]{32}$")
        restarted = Onboarding(self.workspace, connections=self.connections, repositories=self.repository)
        self.assertEqual(restarted.job(job["id"])["result"]["plan_id"], failed["result"]["plan_id"])
        raw = json.dumps(failed)
        self.assertNotIn(KEY, raw)
        self.assertNotIn("provider private error", raw)
        self.assertNotIn(KEY, (onboarding.root / job["id"] / "request.json").read_text())

    def test_interrupted_setup_job_recovers_reserved_plan_id_without_replaying(self):
        operation_id = "b" * 32
        values = {"path": str(self.repo), "goal": "Implement independently useful behavior", "context_binding": self.inspect()["context_binding"],
                  "max_tasks": 2, "workers": 2, "plan_budget_usd": "2.00"}
        child = os.fork()
        if child == 0:
            onboarding = Onboarding(self.workspace, connections=self.connections, repositories=self.repository,
                                    planner_transport=httpx.MockTransport(lambda _: os._exit(23)))
            onboarding.start("planning.generate", values, True, operation_id)
            onboarding.wait()
            os._exit(24)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 23)
        restarted = Onboarding(self.workspace, connections=self.connections, repositories=self.repository)
        job = restarted.job(operation_id)
        self.assertEqual(job["status"], "uncertain")
        self.assertEqual(job["result"]["state"], "reserved")
        self.assertRegex(job["result"]["plan_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(restarted.start("planning.generate", values, True, operation_id), job)
        self.assertIsNone(restarted.thread)
        self.assertFalse(self.requests)

    def test_onboarding_selected_project_binds_explicit_profile_and_rejects_historical(self):
        self.repository.initialize(str(self.repo), install=[["true"]], checks=[["true"]])
        connected = self.workspace.connect_project(str(self.repo / "swarm.toml"))
        onboarding = Onboarding(self.workspace, connections=self.connections, repositories=self.repository)
        selected = onboarding.selected_repository(connected["project_id"])
        self.assertEqual(selected["path"], str(self.repo))
        inspected = onboarding.perform("planning.inspect", {"project_id": connected["project_id"]}, self.folder)
        self.assertEqual(inspected["project_id"], connected["project_id"])
        self.assertEqual(inspected["path"], str(self.repo))
        self.assertFalse(self.requests)
        self.assert_code("selected_project_unavailable", lambda: onboarding.selected_repository("unknown"))


if __name__ == "__main__":
    unittest.main()
