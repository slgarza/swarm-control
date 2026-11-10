"""Exercise the public tutorial without cloud credentials or paid services."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CREATOR = ROOT / "examples/create_demo.py"


class DocumentedDemoTests(unittest.TestCase):
    def test_generated_target_baseline_plan_and_disabled_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "demo"
            subprocess.run([sys.executable, str(CREATOR), str(target)], check=True,
                           capture_output=True)
            baseline = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                                      cwd=target, capture_output=True, text=True, check=True)
            self.assertIn("Ran 2 tests", baseline.stderr)
            plan = subprocess.check_output([
                sys.executable, "-m", "swarm_control", "plan", "D01", "D02",
                "--project", str(target / "swarm.toml"), "--workers", "2",
                "--synchronized", "--base", "HEAD",
            ], cwd=ROOT, text=True)
            result = json.loads(plan)
            self.assertEqual(result["task_states"], {"D01": "ready", "D02": "ready"})
            self.assertEqual(result["maximum_active_agents"], 2)
            self.assertFalse(result["live_execution_enabled"])
            self.assertFalse((target / ".swarm").exists())
            blocked = subprocess.run([
                sys.executable, "-m", "swarm_control", "smoke", "D01",
                "--project", str(target / "swarm.toml"),
            ], cwd=ROOT, text=True, capture_output=True, check=False)
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("live execution is disabled", blocked.stderr)
            self.assertFalse((target / ".swarm").exists())

    def test_creator_refuses_existing_content_and_invalid_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            existing = Path(temporary) / "existing"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_text("user data")
            result = subprocess.run([sys.executable, str(CREATOR), str(existing)],
                                    capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(), "user data")
            invalid = Path(temporary) / "invalid"
            result = subprocess.run([sys.executable, str(CREATOR), str(invalid),
                                     "--repository", 'owner/repo"bad'],
                                    capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(invalid.exists())
