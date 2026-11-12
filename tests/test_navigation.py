"""Exercise the shipped browser router through dependency-free Node contract tests."""

import shutil
import subprocess
import unittest
from pathlib import Path


class NavigationTests(unittest.TestCase):
    def test_router_contract(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for browser router contract tests.")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, "--test", "tests/test_navigation.cjs"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
