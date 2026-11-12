"""Configured task acceptance cannot silently become an unchanged baseline pass."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from swarm_control.modal_worker import acceptance_commands, run_acceptance
from tests.support import make_project


class AcceptanceCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = make_project(self.root / "target")
        self.base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.project.root, text=True).strip()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()

    def execute(self, command, *, protected=None):
        task = {"acceptance_checks": [command], "acceptance_files": protected or []}
        code = run_acceptance({"task": task, "base_sha": self.base}, self.project.root, self.artifacts)
        return code, json.loads((self.artifacts / "acceptance.json").read_text())

    def test_behavior_check_fails_before_feature_then_passes_with_source_change(self):
        command = [sys.executable, "-c", "from pathlib import Path; assert Path('src/item.txt').read_text() == 'implemented\\n'"]
        code, receipt = self.execute(command)
        self.assertNotEqual(code, 0)
        self.assertFalse(receipt["independent_product_acceptance"])
        (self.project.root / "src/item.txt").write_text("implemented\n")
        code, receipt = self.execute(command)
        self.assertEqual(code, 0)
        self.assertEqual(receipt["source"], "frozen_task_commands")
        self.assertEqual(len(receipt["checks_sha256"]), 64)
        self.assertTrue(receipt["inputs_unchanged"])

    def test_protected_input_changed_before_or_during_command_is_rejected(self):
        script = [sys.executable, "-c", "from pathlib import Path; Path('README.md').write_text('relaxed test')"]
        code, receipt = self.execute(script, protected=["README.md"])
        self.assertEqual(code, 125)
        self.assertFalse(receipt["inputs_unchanged"])
        code, _ = self.execute([sys.executable, "-c", "raise SystemExit(0)"], protected=["README.md"])
        self.assertEqual(code, 125)

    def test_absent_checks_are_not_invented_acceptance(self):
        self.assertIsNone(acceptance_commands({}))
        code = run_acceptance({"task": {}, "base_sha": self.base}, self.project.root, self.artifacts)
        self.assertIsNone(code)
        data = json.loads((self.artifacts / "acceptance.json").read_text())
        self.assertFalse(data["configured"])
        self.assertIsNone(data["exit_code"])

    def test_string_shell_bool_and_unbounded_commands_are_rejected(self):
        for checks in ("pytest", [], [["bash", "-c", "pytest"]], [[False]], [["python", "x\x00"]],
                       [["python", "x" * 8193]], [["python"]] * 17):
            with self.subTest(checks=str(checks)[:60]), self.assertRaises(ValueError):
                acceptance_commands({"acceptance_checks": checks})
