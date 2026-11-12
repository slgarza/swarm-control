"""Frozen context sizing is private, cached and explicit about unknown tokens."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_control import context_packet
from swarm_control.context_packet import build_context_readiness
from tests.support import make_project


class ContextReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = make_project(self.root / "target")
        self.base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.project.root, text=True).strip()
        self.task = json.loads((self.project.tasks_dir / "A1.json").read_text())

    def test_frozen_instructions_have_no_raw_text_paths_or_guessed_tokens(self):
        before = len((self.project.root / "README.md").read_bytes())
        (self.project.root / "README.md").write_text("different mutable contents")
        result = build_context_readiness([self.task], self.project, self.base)
        self.assertEqual(result["instruction_count"], 2)
        self.assertEqual(result["instruction_bytes"], before + len((self.project.root / "AGENTS.md").read_bytes()))
        self.assertIsNone(result["token_count"])
        self.assertIn("task_context_selectors_missing", result["warnings"])
        self.assertIn("task_acceptance_checks_missing", result["warnings"])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn("Fixture project", json.dumps(result))
        self.assertNotIn("README.md", json.dumps(result))
        self.assertFalse((self.project.root / ".swarm").exists())

    def test_hundred_tasks_share_two_instruction_reads(self):
        tasks = [{**self.task, "id": f"T{i:03}"} for i in range(100)]
        original = context_packet._frozen_document
        with patch.object(context_packet, "_frozen_document", wraps=original) as read:
            result = build_context_readiness(tasks, self.project, self.base)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(result["task_count"], 100)
        self.assertEqual(len(result["tasks"]), 100)
        self.assertEqual(result["tasks_with_context"], 0)

    def test_packets_and_protected_acceptance_inputs_are_hash_bound(self):
        task = {**self.task, "acceptance_checks": [["python3", "-c", "assert True"]],
                "acceptance_files": ["README.md"]}
        packets = {"A1": {"metadata": {"prompt_bytes": 1234, "prompt_sha256": "a" * 64,
                                      "excerpt_bytes": 0, "sha256": "b" * 64}}}
        result = build_context_readiness([task], self.project, self.base, packets=packets)
        self.assertEqual(result["tasks_with_acceptance_checks"], 1)
        self.assertEqual(result["total_prompt_bytes"], 1234)
        self.assertEqual(result["max_prompt_bytes"], 1234)
        self.assertEqual(result["tasks"][0]["protected_acceptance_inputs"], 1)
        self.assertEqual(len(result["tasks"][0]["protected_acceptance_input_sha256"][0]), 64)

    def test_invalid_acceptance_and_missing_protected_base_input_fail_readiness(self):
        for changes in ({"acceptance_checks": "python test.py"},
                        {"acceptance_checks": [["sh", "-c", "echo bad"]]},
                        {"acceptance_checks": [["python3", "-c", "pass"]], "acceptance_files": ["missing.py"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_context_readiness([{**self.task, **changes}], self.project, self.base)

    def test_git_reads_forbid_lazy_fetch_and_replace_objects(self):
        original = subprocess.check_output
        with patch("swarm_control.context_packet.subprocess.check_output", wraps=original) as command:
            build_context_readiness([self.task], self.project, self.base)
        for call in command.call_args_list:
            self.assertEqual(call.kwargs["env"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(call.kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
            self.assertEqual(call.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
