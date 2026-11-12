"""The beginner entry point opens a local workspace without launching workers."""

import errno
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from swarm_control import cli, launcher
from swarm_control.workspace import Workspace
from tests.support import make_project

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / "private workspace" / "workspace.json"

    def start(self, *args, server=None):
        server = server or Mock(server_port=8787)
        with (patch("sys.argv", ["swarm-control", "start", "--workspace", str(self.registry), *args]),
              patch("swarm_control.launcher.make_server", return_value=server),
              patch("swarm_control.launcher._open_browser") as browser,
              redirect_stdout(io.StringIO()) as output):
            result = cli.main()
        return result, output.getvalue(), server, browser

    def test_default_and_help_show_beginner_commands_without_initializing_state(self):
        for arguments in ([], ["--help"]):
            with (patch("sys.argv", ["swarm-control", *arguments]),
                  patch("swarm_control.workspace.default_workspace", return_value=self.registry),
                  redirect_stdout(io.StringIO()) as output):
                self.assertEqual(cli.main(), 0)
            self.assertIn("swarm-control start", output.getvalue())
            self.assertIn("./easy-run", output.getvalue())
        self.assertFalse(self.registry.exists())

    def test_restart_reuses_workspace_and_history_without_opening_browser(self):
        registry = Workspace(self.registry)
        registry.initialize("My workspace")
        before = self.registry.read_bytes()
        for _ in range(2):
            result, output, server, browser = self.start("--no-open")
            self.assertEqual(result, 0)
            self.assertIn("http://127.0.0.1:8787", output)
            server.serve_forever.assert_called_once()
            server.server_close.assert_called_once()
            browser.assert_not_called()
        self.assertEqual(self.registry.read_bytes(), before)

    def test_connect_prepared_folder_is_idempotent_and_selected_in_url(self):
        project = make_project(self.root / "target with spaces")
        with patch("swarm_control.launcher.threading.Thread") as thread:
            for _ in range(2):
                result, output, _, _ = self.start("--project", str(project.root))
                self.assertEqual(result, 0)
                self.assertIn("/?project=", output)
            thread.return_value.start.assert_called()
        projects = Workspace(self.registry).public_catalog()["projects"]
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["repository"], project.repository)
        self.assertFalse((project.root / ".swarm").exists())

    def test_busy_port_has_actionable_error_and_leaves_registry_intact(self):
        Workspace(self.registry).initialize()
        before = self.registry.read_bytes()
        with (patch("sys.argv", ["start", "--workspace", str(self.registry), "--no-open"]),
              patch("swarm_control.launcher.make_server", side_effect=OSError(errno.EADDRINUSE, "busy")),
              patch("sys.stderr", new_callable=io.StringIO) as errors,
              self.assertRaises(SystemExit) as caught):
            launcher.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("--port 8788", errors.getvalue())
        self.assertEqual(self.registry.read_bytes(), before)

    def test_invalid_port_fails_before_creating_workspace(self):
        with (patch("sys.argv", ["start", "--workspace", str(self.registry), "--port", "65536"]),
              redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()),
              self.assertRaises(SystemExit) as caught):
            launcher.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.registry.exists())

    def test_interrupt_closes_server_and_browser_failure_prints_url(self):
        server = Mock(server_port=9999)
        server.serve_forever.side_effect = KeyboardInterrupt
        result, output, server, _ = self.start("--no-open", server=server)
        self.assertEqual(result, 0)
        self.assertIn("Closing", output)
        server.server_close.assert_called_once()
        with (patch("webbrowser.open", side_effect=OSError), redirect_stdout(io.StringIO()) as output):
            launcher._open_browser("http://127.0.0.1:9999")
        self.assertIn("http://127.0.0.1:9999", output.getvalue())

    def test_checkout_bootstrap_help_works_before_install_even_with_spaces(self):
        checkout = self.root / "fresh checkout"
        checkout.mkdir()
        script = checkout / "easy-run"
        shutil.copy2(ROOT / "easy-run", script)
        result = subprocess.run([str(script), "--help"], cwd=self.root,
                                capture_output=True, text=True, check=True)
        self.assertIn("--project", result.stdout)
        self.assertIn("Python 3.12", result.stdout)
        self.assertFalse((checkout / ".venv").exists())

    def test_incomplete_environment_without_pip_has_recovery_guidance(self):
        checkout = self.root / "incomplete checkout"
        checkout.mkdir()
        shutil.copy2(ROOT / "easy-run", checkout / "easy-run")
        shutil.copy2(ROOT / "pyproject.toml", checkout / "pyproject.toml")
        venv.create(checkout / ".venv", with_pip=False)
        result = subprocess.run([str(checkout / "easy-run"), "--no-open"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing pip", result.stderr)
        self.assertIn("Move that incomplete folder aside", result.stderr)
        self.assertNotIn("internet connection", result.stderr)

    def test_target_checkout_cannot_shadow_controller_module_on_startup(self):
        checkout = self.root / "controller with spaces"
        scripts = checkout / ".venv" / "bin"
        scripts.mkdir(parents=True)
        shutil.copy2(ROOT / "easy-run", checkout / "easy-run")
        shutil.copy2(ROOT / "pyproject.toml", checkout / "pyproject.toml")
        shutil.copy2(ROOT / "pyproject.toml", checkout / ".venv" / ".easy-run-installed")
        # Reuse the real environment for the version check; the entry-point
        # fixture prints argv so this test starts no HTTP server or workers.
        (scripts / "python").symlink_to(sys.executable)
        entry = scripts / "swarm-control"
        entry.write_text('#!/bin/sh\nprintf "%s\\n" "installed-controller" "$@"\n')
        entry.chmod(0o755)
        target = self.root / "unrelated target"
        shadow = target / "swarm_control"
        shadow.mkdir(parents=True)
        (shadow / "__init__.py").write_text("")
        (shadow / "__main__.py").write_text('print("target-code-executed")\n')
        result = subprocess.run([str(checkout / "easy-run"), "--project", "./target with spaces", "--no-open"],
                                cwd=target, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(),
                         ["installed-controller", "start", "--project", "./target with spaces", "--no-open"])
        self.assertNotIn("target-code-executed", result.stdout)
