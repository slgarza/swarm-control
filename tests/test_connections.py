"""Account setup fixtures never inspect real auth files or contact a provider."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from swarm_control.connection_worker import perform
from swarm_control.connections import (
    Connections,
    ConnectionsError,
    _execute,
    parse_environment,
)

GITHUB_TOKEN = "ghp_fixture_private_0123456789"
MODAL_ID = "ak-fixture-0123456789"
MODAL_SECRET = "as-fixture-private-0123456789"
OPENAI_KEY = "sk-fixture-private-0123456789"


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.workspace = "fixture-workspace"
        self.responses = []

    def __call__(self, command, *, env, input, timeout):
        request = json.loads(input) if input else None
        self.calls.append({"command": command, "environment": env, "request": request, "timeout": timeout})
        if self.responses:
            return self.responses.pop(0)
        if command[0] == "gh":
            return 0, b"fixture-user\n"
        return 0, json.dumps({"ok": True, "identity": self.workspace,
                              "secret_present": True}).encode()


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"PATH": os.defpath, "HOME": str(self.root),
                                              "MODAL_CONFIG_PATH": str(self.root / "never-read.toml")}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.path = self.root / "settings/workspace.json"
        self.runner = FakeRunner()
        self.service = Connections(self.path, base_environment={}, runner=self.runner)

    def assert_private(self, value):
        safe = json.loads(json.dumps(value))
        # Account setup intentionally shows the owner's private .env location.
        if "storage" in safe:
            safe["storage"].pop("path", None)
        raw = json.dumps(safe)
        for secret in (GITHUB_TOKEN, MODAL_ID, MODAL_SECRET, OPENAI_KEY, str(self.root)):
            self.assertNotIn(secret, raw)

    def modal(self, persist=False):
        return self.service.configure_modal(MODAL_ID, MODAL_SECRET, persist=persist)

    def test_status_is_cached_and_read_only_with_no_provider_or_credential_file_reads(self):
        with patch("subprocess.Popen", side_effect=AssertionError("network subprocess")):
            for _ in range(3):
                value = self.service.status()
                self.assertFalse(value["github"]["configured"])
                self.assertFalse(value["modal"]["verified"])
                self.assertFalse(value["openai"]["api_key_verified"])
                self.assert_private(value)
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.runner.calls, [])

    def test_session_credentials_are_verified_once_and_never_reach_argv_or_public_data(self):
        before = dict(os.environ)
        result = self.service.configure_github(GITHUB_TOKEN, persist=False)
        self.assertTrue(result["github"]["configured"])
        self.assertTrue(result["github"]["verified"])
        self.assertEqual(result["github"]["identity"], "fixture-user")
        self.assertEqual(result["github"]["source"], "session")
        self.assertEqual(len(self.runner.calls), 1)
        call = self.runner.calls[0]
        self.assertEqual(call["environment"]["GH_TOKEN"], GITHUB_TOKEN)
        self.assertNotIn(GITHUB_TOKEN, json.dumps(call["command"]))
        self.assertIsNone(call["request"])
        self.assert_private(result)
        self.assertEqual(dict(os.environ), before)
        self.assertFalse(self.path.parent.exists())

    def test_persistence_is_private_atomic_and_restores_only_cached_verification(self):
        self.service.configure_github(GITHUB_TOKEN)
        self.modal(persist=True)
        directory = self.path.with_name(self.path.name + ".connections")
        credential_file = directory / ".env"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(credential_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / "settings.lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(directory.glob("*.tmp")), [])
        self.assertFalse((directory / "credentials.json").exists())
        metadata = (directory / "settings.json").read_text()
        self.assertNotIn(GITHUB_TOKEN, metadata)
        self.assertNotIn(MODAL_SECRET, metadata)
        restored = Connections(self.path, base_environment={}, runner=FakeRunner())
        value = restored.status()
        self.assertTrue(value["github"]["persisted"])
        self.assertTrue(value["modal"]["verified"])
        self.assertIsNotNone(value["modal"]["checked_at_utc"])
        self.assertEqual(restored._runner.calls, [])
        self.assert_private(value)

    def test_session_override_does_not_erase_or_rewrite_prior_persisted_credentials(self):
        self.service.configure_github(GITHUB_TOKEN)
        path = self.path.with_name(self.path.name + ".connections") / ".env"
        before = path.read_bytes()
        self.service.configure_github("ghp_fixture_session_987654321", persist=False)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.service.environment()["GH_TOKEN"], "ghp_fixture_session_987654321")
        self.modal(persist=True)
        restored = Connections(self.path, base_environment={}, runner=FakeRunner())
        self.assertEqual(restored.environment()["GH_TOKEN"], GITHUB_TOKEN)

    def test_env_is_fresh_controller_only_and_explicit_modal_uses_no_global_profile(self):
        base = {"CODEX_API_KEY": OPENAI_KEY, "OPENAI_API_KEY": OPENAI_KEY,
                "OPENAI_ADMIN_KEY": OPENAI_KEY, "MODAL_OAUTH_REFRESH_TOKEN": "fixture-oauth",
                "HOME": str(self.root), "KEEP": "fixture"}
        self.service = Connections(self.path, base_environment=base, runner=self.runner)
        self.modal()
        env = self.service.environment()
        self.assertEqual(env["MODAL_TOKEN_SECRET"], MODAL_SECRET)
        self.assertEqual(env["MODAL_CONFIG_PATH"], os.devnull)
        self.assertNotIn("MODAL_OAUTH_REFRESH_TOKEN", env)
        for key in ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
            self.assertNotIn(key, env)
        env["KEEP"] = "changed"
        self.assertEqual(self.service.environment()["KEEP"], "fixture")
        self.assertEqual(base["KEEP"], "fixture")

    def test_existing_ambient_auth_is_only_verified_by_an_explicit_request(self):
        service = Connections(self.path, base_environment={"GH_TOKEN": GITHUB_TOKEN}, runner=self.runner)
        self.assertTrue(service.status()["github"]["configured"])
        self.assertFalse(service.status()["github"]["verified"])
        self.assertEqual(self.runner.calls, [])
        result = service.verify("github")
        self.assertTrue(result["github"]["verified"])
        self.assertEqual(result["github"]["source"], "ambient")
        self.assertFalse(self.path.parent.exists())
        self.assert_private(result)

    def test_failure_and_timeout_never_invent_authenticated_status_or_leak_provider_output(self):
        for response, code in (((1, GITHUB_TOKEN.encode()), "github_auth_failed"),
                               ((None, b""), "provider_timeout"),
                               ((0, (GITHUB_TOKEN + "suffix").encode()), "invalid_provider_response")):
            self.runner.responses.append(response)
            result = self.service.configure_github(GITHUB_TOKEN, persist=False)
            self.assertFalse(result["github"]["verified"])
            self.assertEqual(result["github"]["error_code"], code)
            self.assertTrue(result["github"]["configured"])
            self.assert_private(result)
        self.assertEqual(len(self.runner.calls), 3)

    def test_arbitrary_runner_exception_text_is_not_public(self):
        def failed(*args, **kwargs):
            raise RuntimeError(OPENAI_KEY)
        service = Connections(self.path, base_environment={}, runner=failed)
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            result = service.configure_github(GITHUB_TOKEN, persist=False)
        self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertEqual(result["github"]["error_code"], "invalid_provider_response")
        self.assert_private(result)

    def test_invalid_values_fail_before_subprocess_or_file_creation(self):
        for token in (None, "", "short", "has whitespace", "fixture\nsecret", "x" * 4097, ["fixture"]):
            with self.subTest(token_type=type(token).__name__), self.assertRaises(ConnectionsError) as error:
                self.service.configure_github(token)
            self.assertEqual(error.exception.code, "invalid_credential")
        for provider in ("unknown", "../../file"):
            with self.assertRaises(ConnectionsError):
                self.service.verify(provider)
        with self.assertRaises(ConnectionsError):
            self.service.configure_github(GITHUB_TOKEN, persist="yes")
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.path.parent.exists())

    def test_git_symlink_hardlink_and_public_permission_storage_is_rejected(self):
        git_root = self.root / "repo"
        (git_root / ".git").mkdir(parents=True)
        repo = Connections(git_root / "workspace.json", base_environment={}, runner=self.runner)
        self.assertFalse(repo.status()["storage"]["persistent_available"])
        with self.assertRaises(ConnectionsError):
            repo.configure_github(GITHUB_TOKEN)
        self.assertFalse((git_root / "workspace.json.connections").exists())
        bare_root = self.root / "bare"
        (bare_root / "objects").mkdir(parents=True)
        (bare_root / "refs").mkdir()
        (bare_root / "HEAD").write_text("ref: refs/heads/main\n")
        (bare_root / "config").write_text("[core]\n bare = true\n")
        bare = Connections(bare_root / "workspace.json", base_environment={}, runner=self.runner)
        with self.assertRaises(ConnectionsError):
            bare.configure_github(GITHUB_TOKEN)
        destination = self.root / "destination"
        destination.mkdir()
        link = self.root / "link"
        link.symlink_to(destination, target_is_directory=True)
        linked = Connections(link / "workspace.json", base_environment={}, runner=self.runner)
        with self.assertRaises(ConnectionsError):
            linked.configure_github(GITHUB_TOKEN)
        self.assertEqual(list(destination.iterdir()), [])
        self.service.configure_github(GITHUB_TOKEN)
        path = self.path.with_name(self.path.name + ".connections") / ".env"
        original = path.read_bytes()
        path.chmod(0o644)
        unsafe = Connections(self.path, base_environment={}, runner=self.runner)
        self.assertFalse(unsafe.status()["storage"]["persistent_available"])
        with self.assertRaises(ConnectionsError):
            unsafe.configure_github(GITHUB_TOKEN)
        self.assertEqual(path.read_bytes(), original)
        path.chmod(0o600)
        os.link(path, self.root / "another-credential-link")
        hardlinked = Connections(self.path, base_environment={}, runner=self.runner)
        self.assertFalse(hardlinked.status()["storage"]["persistent_available"])

    def test_duplicate_and_unknown_settings_are_preserved_as_corrupt(self):
        directory = self.path.with_name(self.path.name + ".connections")
        directory.mkdir(parents=True, mode=0o700)
        file = directory / "credentials.json"
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"schema_version":1,"api_key":"private"}'):
            file.write_bytes(raw)
            file.chmod(0o600)
            service = Connections(self.path, base_environment={}, runner=self.runner)
            self.assertEqual(service.status()["storage"]["error_code"], "storage_corrupt")
            with self.assertRaises(ConnectionsError):
                service.configure_github(GITHUB_TOKEN)
            self.assertEqual(file.read_bytes(), raw)

    def test_two_instances_merge_provider_updates_without_losing_credentials(self):
        other = Connections(self.path, base_environment={}, runner=FakeRunner())
        self.service.configure_github(GITHUB_TOKEN)
        other.configure_modal(MODAL_ID, MODAL_SECRET)
        restored = Connections(self.path, base_environment={}, runner=FakeRunner())
        env = restored.environment()
        self.assertEqual(env["GH_TOKEN"], GITHUB_TOKEN)
        self.assertEqual(env["MODAL_TOKEN_SECRET"], MODAL_SECRET)

    def test_old_verification_cannot_authenticate_new_credential_and_status_does_not_wait(self):
        started, release = threading.Event(), threading.Event()
        def runner(command, *, env, input, timeout):
            if env.get("GH_TOKEN") == GITHUB_TOKEN:
                started.set()
                self.assertTrue(release.wait(2))
                return 0, b"old-user"
            return 0, b"new-user"
        service = Connections(self.path, base_environment={}, runner=runner)
        old = threading.Thread(target=lambda: service.configure_github(GITHUB_TOKEN, persist=False))
        old.start()
        self.assertTrue(started.wait(2))
        self.assertFalse(service.status()["github"]["verified"])
        current = service.configure_github("ghp_fixture_new_0123456789", persist=False)
        self.assertEqual(current["github"]["identity"], "new-user")
        release.set()
        old.join(2)
        self.assertFalse(old.is_alive())
        self.assertEqual(service.status()["github"]["identity"], "new-user")

    def test_openai_key_is_only_in_stdin_and_never_saved_or_claimed_valid(self):
        self.modal()
        result = self.service.configure_openai(OPENAI_KEY, "fixture-openai", "main")
        call = self.runner.calls[-1]
        self.assertEqual(call["request"]["api_key"], OPENAI_KEY)
        self.assertEqual(call["request"]["expected_workspace"], "fixture-workspace")
        self.assertFalse(call["request"]["replace"])
        self.assertNotIn(OPENAI_KEY, json.dumps(call["environment"]))
        self.assertNotIn(OPENAI_KEY, json.dumps(call["command"]))
        self.assertTrue(result["openai"]["verified"])
        self.assertEqual(result["openai"]["secret_name"], "fixture-openai")
        self.assertEqual(result["openai"]["environment"], "main")
        self.assertFalse(result["openai"]["api_key_verified"])
        self.assertFalse(result["openai"]["model_access_verified"])
        self.assertFalse(result["openai"]["quota_verified"])
        self.assert_private(result)
        for file in self.path.parent.rglob("*"):
            if file.is_file():
                self.assertNotIn(OPENAI_KEY.encode(), file.read_bytes())
        self.service.verify("openai")
        self.assertNotIn("api_key", self.runner.calls[-1]["request"])

    def test_openai_replacement_is_explicit_and_requires_a_verified_known_workspace(self):
        with self.assertRaises(ConnectionsError) as error:
            self.service.configure_openai(OPENAI_KEY, "fixture-openai")
        self.assertEqual(error.exception.code, "modal_not_verified")
        self.assertEqual(self.runner.calls, [])
        self.modal()
        self.service.configure_openai(OPENAI_KEY, "fixture-openai", replace=True)
        self.assertTrue(self.runner.calls[-1]["request"]["replace"])
        for name in ("../secret", "with spaces", "--flag", OPENAI_KEY):
            with self.assertRaises(ConnectionsError):
                self.service.configure_openai(OPENAI_KEY, name)

    def test_uncertain_mutation_stays_attributed_even_after_secret_presence_lookup(self):
        self.modal()
        self.runner.responses.append((None, b""))
        result = self.service.configure_openai(OPENAI_KEY, "fixture-openai")
        self.assertEqual(result["openai"]["error_code"], "provider_timeout")
        self.assertTrue(result["openai"]["last_operation_uncertain"])
        self.assertFalse(result["openai"]["verified"])
        result = self.service.verify("openai")
        self.assertTrue(result["openai"]["verified"])
        self.assertTrue(result["openai"]["last_operation_uncertain"])
        self.assertFalse(result["openai"]["api_key_verified"])
        self.assertEqual(len(self.runner.calls), 3)

    def test_changing_modal_workspace_invalidates_old_secret_verification_across_restart(self):
        self.modal(persist=True)
        self.service.configure_openai(OPENAI_KEY, "fixture-openai")
        self.runner.workspace = "second-workspace"
        self.service.configure_modal("ak-fixture-another-id", "as-fixture-another-secret")
        self.assertFalse(self.service.status()["openai"]["verified"])
        restored = Connections(self.path, base_environment={}, runner=FakeRunner())
        self.assertFalse(restored.status()["openai"]["verified"])

    def test_env_import_is_literal_private_and_has_no_network_or_implicit_secret_creation(self):
        text = ('# My own computer\nGH_TOKEN="' + GITHUB_TOKEN + '"\n'
                + "MODAL_TOKEN_ID='" + MODAL_ID + "'\nMODAL_TOKEN_SECRET=" + MODAL_SECRET + "\n"
                + "OPENAI_API_KEY=" + OPENAI_KEY + "\n")
        result = self.service.configure_environment(text)
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(result["github"]["configured"])
        self.assertFalse(result["github"]["verified"])
        self.assertTrue(result["openai"]["local_key_configured"])
        self.assertFalse(result["openai"]["configured"])
        self.assert_private(result)
        path = Path(result["storage"]["path"])
        self.assertEqual(path.name, ".env")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        parsed = parse_environment(path.read_text())
        self.assertEqual(parsed["CODEX_API_KEY"], OPENAI_KEY)
        self.assertNotIn("OPENAI_API_KEY", parsed)
        metadata = (path.parent / "settings.json").read_text()
        for value in (GITHUB_TOKEN, MODAL_ID, MODAL_SECRET, OPENAI_KEY):
            self.assertNotIn(value, metadata)
        self.assertNotIn("CODEX_API_KEY", self.service.environment())
        self.assertNotIn("OPENAI_API_KEY", self.service.environment())

    def test_saved_env_key_requires_separate_explicit_modal_secret_action(self):
        self.service.configure_environment("CODEX_API_KEY=" + OPENAI_KEY)
        self.assertEqual(self.runner.calls, [])
        self.modal()
        result = self.service.configure_openai(secret_name="fixture-openai")
        self.assertTrue(result["openai"]["verified"])
        self.assertEqual(self.runner.calls[-1]["request"]["api_key"], OPENAI_KEY)
        self.assert_private(result)

    def test_model_key_persistence_requires_explicit_choice_when_entered_in_secret_form(self):
        self.modal()
        result = self.service.configure_openai(OPENAI_KEY, "fixture-openai")
        self.assertFalse(result["openai"]["local_key_configured"])
        result = self.service.configure_openai(OPENAI_KEY, "fixture-openai", replace=True, persist_key=True)
        self.assertTrue(result["openai"]["local_key_configured"])
        self.assertEqual(parse_environment(Path(result["storage"]["path"]).read_text())["CODEX_API_KEY"], OPENAI_KEY)
        self.assert_private(result)

    def test_session_env_and_template_never_return_values(self):
        result = self.service.configure_environment("GITHUB_TOKEN=" + GITHUB_TOKEN + "\nCODEX_API_KEY=" + OPENAI_KEY,
                                                    persist=False)
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(result["openai"]["local_key_source"], "session")
        template = self.service.environment_template()
        self.assertEqual(set(parse_environment(template["template"], allow_empty=True)), set())
        self.assertEqual({row["name"] for row in template["variables"]},
                         {"GH_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "CODEX_API_KEY"})
        self.assertTrue(next(row for row in template["variables"] if row["name"] == "GH_TOKEN")["configured"])
        self.assert_private(template)

    def test_env_parser_rejects_expansion_commands_unknowns_duplicates_and_multiline_values(self):
        invalid = ["GH_TOKEN=$(get-token)", 'GH_TOKEN="${TOKEN}"', "GH_TOKEN=`get-token`",
                   "PATH=fixture-path", "export GH_TOKEN=fixture-token", "GH_TOKEN=fixture-token\nGH_TOKEN=second-token",
                   "GH_TOKEN=fixture-token\nGITHUB_TOKEN=second-token", 'GH_TOKEN="fixture\\nsecret"',
                   'GH_TOKEN="fixture\\u0024secret"', "MODAL_TOKEN_ID=fixture-token", "GH_TOKEN=fixture token",
                   "GH_TOKEN=fixture-token\nrun-something", "", "CODEX_API_KEY=" + "x" * 4097]
        for text in invalid:
            with self.subTest(kind=text[:24]), self.assertRaises(ConnectionsError) as error:
                self.service.configure_environment(text)
            self.assertEqual(error.exception.code, "invalid_environment")
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.path.parent.exists())

    def test_external_env_edit_invalidates_cached_auth_without_any_network(self):
        self.service.configure_github(GITHUB_TOKEN)
        path = Path(self.service.status()["storage"]["path"])
        path.write_text("GH_TOKEN=ghp_fixture_changed_0123456789\n")
        restored = Connections(self.path, base_environment={}, runner=FakeRunner())
        self.assertTrue(restored.status()["github"]["configured"])
        self.assertFalse(restored.status()["github"]["verified"])
        self.assertIsNone(restored.status()["github"]["checked_at_utc"])
        self.assertEqual(restored._runner.calls, [])

    def test_legacy_private_json_is_read_but_new_writes_use_env_and_metadata_only(self):
        directory = self.path.with_name(self.path.name + ".connections")
        directory.mkdir(mode=0o700, parents=True)
        old = directory / "credentials.json"
        old.write_text(json.dumps({"schema_version": 1, "github": {"token": GITHUB_TOKEN,
            "observation": {"verified": True, "identity": "fixture-user",
                            "checked_at_utc": "2026-11-12T17:00:00+00:00", "error_code": None,
                            "last_operation_uncertain": False}}}))
        old.chmod(0o600)
        original = old.read_bytes()
        restored = Connections(self.path, base_environment={}, runner=self.runner)
        self.assertTrue(restored.status()["github"]["verified"])
        restored.configure_modal(MODAL_ID, MODAL_SECRET)
        self.assertEqual(old.read_bytes(), original)
        self.assertEqual(parse_environment((directory / ".env").read_text())["GH_TOKEN"], GITHUB_TOKEN)
        self.assertNotIn(GITHUB_TOKEN, (directory / "settings.json").read_text())

    def test_redaction_scrubs_saved_model_key_session_base_tokens_and_unknown_known_patterns(self):
        self.service = Connections(self.path, base_environment={"OPENAI_ADMIN_KEY": "fixture-admin-0123456789"},
                                   runner=self.runner)
        self.service.configure_environment("CODEX_API_KEY=" + OPENAI_KEY)
        self.service.configure_github(GITHUB_TOKEN, persist=False)
        self.modal()
        raw = (f"{OPENAI_KEY} {GITHUB_TOKEN} {MODAL_ID} {MODAL_SECRET} fixture-admin-0123456789 "
               "sk-proj-previous-account-0123456789 as-previous-secret-0123456789 "
               "Bearer unknown-sensitive-value CODEX_API_KEY=unknown-nonstandard-value")
        safe = self.service.redact(raw)
        for value in raw.split():
            self.assertNotIn(value, safe)
        self.assertIn("[redacted]", safe)
        self.assertNotIn("CODEX_API_KEY", self.service.environment())
        self.assertEqual(self.service.redact("450 tests passed"), "450 tests passed")
        self.assertEqual(self.service.redact(self.service.environment_template()["template"]),
                         self.service.environment_template()["template"])


class ProcessBoundaryTests(unittest.TestCase):
    def test_time_and_output_limits_stop_local_subprocesses_without_logs(self):
        env = {"PATH": os.defpath}
        started = time.monotonic()
        code, output = _execute([sys.executable, "-c", "import time;time.sleep(30)"],
                                env=env, input=b"x" * 1000000, timeout=.15)
        self.assertIsNone(code)
        self.assertEqual(output, b"")
        self.assertLess(time.monotonic() - started, 2)
        code, output = _execute([sys.executable, "-c", "import os;os.write(1,b'x'*1000000)"], env=env, timeout=1)
        self.assertEqual((code, output), (-1, b""))

    def test_natural_child_exit_with_descendant_is_bounded_by_outer_pipe_deadline(self):
        code = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])"
        started = time.monotonic()
        result = _execute([sys.executable, "-c", code], env={"PATH": os.defpath}, timeout=.15)
        self.assertEqual(result, (None, b""))
        self.assertLess(time.monotonic() - started, 2)

    def test_pinned_sdk_worker_discards_native_and_python_sdk_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "modal"
            package.mkdir()
            (package / "__init__.py").write_text(
                "import os\n"
                "class Secret:\n"
                " @staticmethod\n"
                " def from_name(*a,**k): return Secret()\n"
                " def hydrate(self): return self\n"
                " class objects:\n"
                "  @staticmethod\n"
                "  def create(name,values,**kwargs):\n"
                "   print(values['CODEX_API_KEY']);os.write(1,values['CODEX_API_KEY'].encode());"
                "os.write(2,values['CODEX_API_KEY'].encode())\n")
            (package / "workspace.py").write_text(
                "class Workspace:\n"
                " name='fixture-workspace'\n"
                " @staticmethod\n"
                " def from_context(): return Workspace()\n"
                " def hydrate(self): return self\n")
            (package / "exception.py").write_text(
                "class AlreadyExistsError(Exception): pass\n"
                "class AuthError(Exception): pass\n"
                "class NotFoundError(Exception): pass\n")
            env = {"PATH": os.defpath, "PYTHONPATH": str(root) + os.pathsep + str(Path(__file__).resolve().parents[1]),
                   "MODAL_CONFIG_PATH": str(root / "nonexistent-modal.toml")}
            request = {"operation": "openai_configure", "api_key": OPENAI_KEY,
                       "secret_name": "fixture-openai", "environment": "main", "replace": False,
                       "expected_workspace": "fixture-workspace"}
            result = subprocess.run([sys.executable, "-m", "swarm_control.connection_worker"],
                                     env=env, input=json.dumps(request), text=True,
                                     capture_output=True, timeout=3, check=True)
            self.assertEqual(result.stderr, "")
            self.assertNotIn(OPENAI_KEY, result.stdout)
            self.assertEqual(json.loads(result.stdout), {"ok": True, "identity": "fixture-workspace",
                                                        "secret_present": True})

    def test_worker_rejects_other_sdk_before_importing_provider(self):
        module = types.ModuleType("modal_version")
        module.__version__ = "0.0.0-fixture"
        with patch.dict(sys.modules, {"modal_version": module}):
            self.assertEqual(perform({"operation": "modal_verify"}),
                             {"ok": False, "error_code": "sdk_version_mismatch"})

    def test_worker_create_collision_and_replacement_use_only_reviewed_sdk_actions(self):
        provider = types.ModuleType("modal")
        exceptions = types.ModuleType("modal.exception")
        workspace_module = types.ModuleType("modal.workspace")
        for name in ("AlreadyExistsError", "AuthError", "NotFoundError"):
            setattr(exceptions, name, type(name, (Exception,), {}))
        workspace = types.SimpleNamespace(name="fixture-workspace")
        workspace.hydrate = lambda: workspace
        workspace_module.Workspace = types.SimpleNamespace(from_context=lambda: workspace)
        handle = Mock()
        handle.hydrate.return_value = handle
        updates = []
        handle.update.side_effect = lambda values: updates.append(dict(values))
        create = Mock(side_effect=exceptions.AlreadyExistsError(OPENAI_KEY))
        provider.Secret = types.SimpleNamespace(objects=types.SimpleNamespace(create=create),
                                                from_name=Mock(return_value=handle))
        request = {"operation": "openai_configure", "api_key": OPENAI_KEY,
                   "secret_name": "fixture-openai", "environment": "main", "replace": False,
                   "expected_workspace": "fixture-workspace"}
        with patch.dict(sys.modules, {"modal": provider, "modal.exception": exceptions,
                                      "modal.workspace": workspace_module}):
            self.assertEqual(perform(dict(request)), {"ok": False, "error_code": "secret_exists", "uncertain": False})
            handle.update.assert_not_called()
            result = perform({**request, "replace": True})
            self.assertTrue(result["ok"])
            self.assertEqual(create.call_count, 1)
            handle.update.assert_called_once()
            self.assertEqual(updates, [{"CODEX_API_KEY": OPENAI_KEY}])
            provider.Secret.from_name.assert_called_with("fixture-openai", environment_name="main",
                                                         required_keys=["CODEX_API_KEY"])
            result = perform({**request, "expected_workspace": "different-workspace"})
            self.assertEqual(result, {"ok": False, "error_code": "workspace_changed"})
            self.assertEqual(create.call_count, 1)


if __name__ == "__main__":
    unittest.main()
