import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from swarm_control import modal_runner as runner
from swarm_control import pilot_integrate as integration
from swarm_control.modal_worker import run_commands
from swarm_control.pilot_state import Ledger, common_state, verify_github_tasks
from swarm_control.project import load_project
from tests.support import make_project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_two_repositories_keep_target_state_commands_and_routing_separate(self):
        for name in ("web", "service"):
            project = make_project(self.root / name, f"different-owner/{name}", check_marker=name)
            project.verify_origin()
            calls = []

            def github(path, calls=calls):
                calls.append(path)
                return [] if "/pulls?" in path else {"state": "open"}

            verify_github_tasks([{"id": "A1", "issue": 1}], project.root, "a" * 40,
                                repository=project.repository, tasks_dir=project.tasks_dir,
                                query=github)
            self.assertTrue(all(path.startswith(f"repos/different-owner/{name}/") for path in calls))
            self.assertEqual(common_state(project.root), project.root / ".swarm")
            log = self.root / f"{name}.log"
            self.assertEqual(run_commands(project.checks, project.root, log), 0)
            self.assertEqual(log.read_text().strip(), name)
            output = io.StringIO()
            with (patch.multiple(runner, PROJECT=None, ROOT=project.root, REPOSITORY=None, APP=None),
                  patch("sys.argv", ["swarm-control", "plan", "A1", "--project", str(project.path),
                                     "--base", "HEAD"]), redirect_stdout(output)):
                runner.main()
            self.assertEqual(json.loads(output.getvalue())["repository"], project.repository)

    def test_origin_mismatch_and_embedded_credentials_are_rejected(self):
        project = make_project(self.root)
        subprocess.run(["git", "remote", "set-url", "origin", "https://github.com/other/repo.git"],
                       cwd=self.root, check=True)
        with self.assertRaisesRegex(ValueError, "origin does not match"):
            project.verify_origin()
        project.path.write_text(project.path.read_text().replace("example/project", "token@github.com/example/project"))
        with self.assertRaisesRegex(ValueError, "without credentials"):
            load_project(project.path)

    def test_unknown_policy_setting_does_not_silently_use_a_default(self):
        project = make_project(self.root)
        project.path.write_text(project.path.read_text().replace("model_limit_cents", "model_limit_cent"))
        with self.assertRaisesRegex(ValueError, "unknown project setting"):
            load_project(project.path)

    def test_larger_agent_capacity_is_explicit_and_does_not_authorize_spend(self):
        project = make_project(self.root)
        self.assertEqual(project.max_live_agents, 10)
        self.assertEqual(project.gateway_concurrency, 20)
        project.path.write_text(project.path.read_text() + "max_live_agents = 100\n")
        larger = load_project(project.path)
        self.assertEqual(larger.max_live_agents, 100)
        self.assertFalse(larger.live_enabled)
        self.assertEqual(larger.model_limit_cents, 1800)
        with self.assertRaisesRegex(ValueError, "live execution is disabled"):
            larger.require_live()
        project.path.write_text(project.path.read_text().replace("max_live_agents = 100", "max_live_agents = 101"))
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            load_project(project.path)

    def test_gateway_concurrency_is_validated_separately_from_live_spend(self):
        project = make_project(self.root)
        original = project.path.read_text()
        for value in (0, 101, True, 1.5, "100"):
            project.path.write_text(original.replace("[budget]", "[runtime]\ngateway_concurrency = " + json.dumps(value) + "\n[budget]"))
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "gateway_concurrency"):
                load_project(project.path)
        project.path.write_text(original.replace("[budget]", "[runtime]\ngateway_concurrency = 100\n[budget]"))
        configured = load_project(project.path)
        self.assertEqual(configured.gateway_concurrency, 100)
        self.assertFalse(configured.live_enabled)
        self.assertEqual(configured.model_limit_cents, 1800)

    def test_gateway_provider_pacing_is_paired_bounded_and_disabled_by_default(self):
        project = make_project(self.root)
        self.assertIsNone(project.gateway_tokens_per_minute)
        self.assertIsNone(project.gateway_requests_per_minute)
        original = project.path.read_text()
        invalid = (
            "gateway_tokens_per_minute = 800000", "gateway_requests_per_minute = 4000",
            "gateway_tokens_per_minute = true\ngateway_requests_per_minute = 4000",
            "gateway_tokens_per_minute = 0\ngateway_requests_per_minute = 4000",
            "gateway_tokens_per_minute = 1000000001\ngateway_requests_per_minute = 4000",
            "gateway_tokens_per_minute = 800000\ngateway_requests_per_minute = 1000001",
            "gateway_tokens_per_minute = 800000\ngateway_requests_per_minute = -1",
        )
        for values in invalid:
            project.path.write_text(original.replace("[budget]", f"[runtime]\n{values}\n[budget]"))
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "gateway_"):
                load_project(project.path)
        project.path.write_text(original.replace("[budget]", "[runtime]\ngateway_tokens_per_minute = 800000\ngateway_requests_per_minute = 4000\n[budget]"))
        configured = load_project(project.path)
        self.assertEqual(configured.gateway_tokens_per_minute, 800000)
        self.assertEqual(configured.gateway_requests_per_minute, 4000)
        self.assertFalse(configured.live_enabled)
        self.assertEqual(configured.model_limit_cents, 1800)
        self.assertEqual(configured.infra_limit_cents, 700)

    def test_deployment_pacing_comes_only_from_the_reviewed_profile(self):
        from swarm_control import deployment

        project = make_project(self.root)
        original = project.path.read_text().replace("live_enabled = false", "live_enabled = true")
        for configured in (False, True):
            text = original
            if configured:
                text = text.replace("[budget]", "[runtime]\ngateway_tokens_per_minute = 800000\ngateway_requests_per_minute = 4000\n[budget]")
            project.path.write_text(text)
            configured_project = load_project(project.path)
            with (
                self.subTest(configured=configured),
                patch.dict(os.environ, {"SWARM_GATEWAY_TPM": "999", "SWARM_GATEWAY_RPM": "999"}),
                patch("sys.argv", ["deploy-gateway", "--project", str(project.path)]),
                patch.object(deployment, "load_project", return_value=configured_project),
                patch.object(type(project), "verify_origin"),
                patch.object(deployment.subprocess, "run") as run,
            ):
                deployment.main()
            environment = run.call_args.kwargs["env"]
            if configured:
                self.assertEqual(environment["SWARM_GATEWAY_TPM"], "800000")
                self.assertEqual(environment["SWARM_GATEWAY_RPM"], "4000")
            else:
                self.assertNotIn("SWARM_GATEWAY_TPM", environment)
                self.assertNotIn("SWARM_GATEWAY_RPM", environment)

    def test_deployment_preserves_configured_app_ledger_and_secret_names(self):
        project = make_project(self.root)
        project.path.write_text(project.path.read_text().replace(
            "[budget]", '[runtime]\ngateway_app = "existing-gateway"\n'
            'gateway_ledger = "existing-budget"\ngateway_concurrency = 100\nopenai_secret = "existing-secret"\n[budget]'
        ).replace("live_enabled = false", "live_enabled = true"))
        from swarm_control import deployment
        project = load_project(project.path)
        project.verify_origin()
        with (patch("sys.argv", ["deploy-gateway", "--project", str(project.path)]),
              patch.object(deployment, "load_project", return_value=project),
              patch.object(type(project), "verify_origin"),
              patch.object(deployment.subprocess, "run") as run):
            deployment.main()
        call = run.call_args
        self.assertEqual(call.kwargs["env"]["SWARM_GATEWAY_APP"], "existing-gateway")
        self.assertEqual(call.kwargs["env"]["SWARM_GATEWAY_LEDGER"], "existing-budget")
        self.assertEqual(call.kwargs["env"]["SWARM_GATEWAY_CONCURRENCY"], "100")
        self.assertEqual(call.kwargs["env"]["SWARM_OPENAI_SECRET"], "existing-secret")

    def test_disabled_migration_profile_cannot_start_workers_or_probes(self):
        project = make_project(self.root)
        from swarm_control.probe_gateway import run_probe
        with self.assertRaisesRegex(ValueError, "live execution is disabled"):
            run_probe("https://test.modal.run", project)
        with (patch.multiple(runner, PROJECT=None, ROOT=self.root, REPOSITORY=None, APP=None),
              patch("sys.argv", ["swarm-control", "smoke", "A1", "--project", str(project.path)]),
              self.assertRaisesRegex(ValueError, "live execution is disabled")):
            runner.main()
        self.assertFalse((self.root / ".swarm").exists())

    def test_validation_is_argv_not_shell_and_stops_after_failure(self):
        project = make_project(self.root)
        marker = self.root / "should-not-exist"
        log = self.root / "checks.log"
        commands = [list(project.checks[0][:-1]) + ["raise SystemExit(7)"],
                    list(project.checks[0][:-1]) + [f"open({str(marker)!r}, 'w').write('bad')"]]
        self.assertEqual(run_commands(commands, self.root, log), 7)
        self.assertFalse(marker.exists())
        with self.assertRaises(ValueError):
            run_commands(["echo unsafe"], self.root, log)

    def test_task_directory_cannot_escape_through_a_symlink(self):
        project = make_project(self.root / "target")
        outside = self.root / "outside"
        outside.mkdir()
        (project.root / "external").symlink_to(outside, target_is_directory=True)
        project.path.write_text(project.path.read_text().replace("[project]", '[project]\ntasks_dir = "external"'))
        with self.assertRaisesRegex(ValueError, "inside the target checkout"):
            load_project(project.path)

    def test_individual_task_symlink_is_rejected_before_reading_external_content(self):
        project = make_project(self.root / "target")
        outside = self.root / "outside.json"
        outside.write_text("this private text is not JSON")
        task = project.tasks_dir / "A1.json"
        task.unlink()
        task.symlink_to(outside)
        with (patch.object(runner, "PROJECT", project),
              self.assertRaisesRegex(ValueError, "inside the configured task directory")):
            runner.load_task("A1")

    def test_required_ci_names_are_project_specific(self):
        payload = {"check_runs": [{"name": "backend", "app": {"slug": "github-actions"},
                                  "head_sha": "a", "status": "completed", "conclusion": "success"}]}
        integration.green_checks(payload, "a", ("backend",))
        with self.assertRaisesRegex(ValueError, "required CI checks missing"):
            integration.green_checks(payload, "a", ("frontend",))

    def test_integration_runs_target_commands_in_a_real_candidate_worktree(self):
        project = make_project(self.root / "target", check_marker="target checks passed")
        original_git = integration.git
        base = original_git(project.root, "rev-parse", "HEAD")
        source = project.root / "src/item.txt"
        source.write_text("changed\n")
        patch_bytes = subprocess.check_output(["git", "diff", "--binary"], cwd=project.root)
        source.write_text("baseline\n")
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        (artifacts / "changes.patch").write_bytes(patch_bytes)
        task = json.loads((project.tasks_dir / "A1.json").read_text())
        digest = hashlib.sha256(patch_bytes).hexdigest()
        report = {"task_id": task["id"], "base_sha": base, "status": "validated",
                  "agent_exit": 0, "check_exit": 0, "patch_sha256": digest,
                  "checked_patch_sha256": digest, "changes": {"head_unchanged": True,
                  "ownership_violations": [], "prohibited_files": []}}
        (artifacts / "manifest.json").write_text(json.dumps({"allow_overlap": False,
            "repository": project.clone_url, "base_sha": base, "task": task}))
        (artifacts / "report.json").write_text(json.dumps(report))

        def local_git(root, *args):
            if args[0] == "fetch":
                return original_git(root, "update-ref", "refs/remotes/origin/main", base)
            return original_git(root, *args)

        with (patch.multiple(integration, PROJECT=project, ROOT=project.root,
                             REPO=project.repository, BASE_BRANCH="main"),
              patch.object(integration, "git", side_effect=local_git)):
            state = integration.prepare(artifacts, task, report, digest,
                                        worktree=self.root / "candidate")
            integration.check_source_bindings(artifacts, state, task, report)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual((self.root / "candidate/src/item.txt").read_text(), "changed\n")
        self.assertEqual((artifacts / "integration-install-1.log").read_text().strip(), "installed")
        self.assertEqual((artifacts / "integration-checks-1.log").read_text().strip(), "target checks passed")

    def test_budget_policy_cannot_be_changed_by_editing_the_profile(self):
        path = self.root / "ledger.db"
        Ledger(path, model_limit_cents=500, infra_limit_cents=200).allocate("first", 100, 50)
        with self.assertRaisesRegex(ValueError, "explicit budget migration"):
            Ledger(path, model_limit_cents=600, infra_limit_cents=200)
        ledger = Ledger(path, model_limit_cents=500, infra_limit_cents=200)
        with ledger.connect() as db:
            self.assertEqual(db.execute("SELECT SUM(cents) FROM pilot_allocations").fetchone()[0], 100)

    def test_legacy_budget_and_completed_tasks_survive_extraction(self):
        path = self.root / "legacy.db"
        ledger = Ledger(path)
        ledger.claim([{"id": "A1"}], "original", "a" * 40, budget_cents=1800, infra_cents=670)
        ledger.finish("A1", "original", "merged", "report.json")
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE budget_policy")
        with self.assertRaisesRegex(ValueError, "legacy pilot allowance"):
            Ledger(path, model_limit_cents=2500)
        migrated = Ledger(path)
        self.assertEqual(migrated.rows()[0]["state"], "merged")
        with self.assertRaisesRegex(ValueError, "model allowance is exhausted"):
            migrated.allocate("another", 1)
        with migrated.connect() as db:
            self.assertEqual(db.execute("SELECT SUM(cents) FROM pilot_infra_allocations").fetchone()[0], 670)


if __name__ == "__main__":
    unittest.main()
