"""Catalog selection never exposes paths or performs work for a selected project."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_control.dashboard_catalog import load_catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "catalog.json"
        self.data = {
            "schema_version": 1,
            "workspace_label": "My workspace",
            "projects": [
                {
                    "id": "one",
                    "label": "Project One",
                    "repository": "example/one",
                    "runs": [
                        {
                            "id": "first",
                            "label": "First run",
                            "snapshot": "missing.json",
                        }
                    ],
                },
                {
                    "id": "two",
                    "label": "Project Two",
                    "repository": "example/two",
                    "runs": [
                        {
                            "id": "other",
                            "label": "Other run",
                            "snapshot": str(self.root / "other.json"),
                        }
                    ],
                },
            ],
        }

    def load(self):
        self.path.write_text(json.dumps(self.data))
        return load_catalog(self.path)

    def test_two_projects_default_explicit_selection_and_path_privacy(self):
        catalog = self.load()
        self.assertEqual(
            catalog.resolve(),
            (self.root / "missing.json", "example/one", "one", "first"),
        )
        self.assertEqual(
            catalog.resolve("two", "other"),
            (self.root / "other.json", "example/two", "two", "other"),
        )
        public = catalog.public()
        self.assertEqual(public["default_project_id"], "one")
        self.assertEqual(public["default_run_id"], "first")
        self.assertNotIn(str(self.root), json.dumps(public))
        self.assertNotIn("snapshot", json.dumps(public))
        public["projects"].clear()
        self.assertEqual(len(catalog.public()["projects"]), 2)

    def test_empty_project_and_empty_workspace_do_not_invent_runs(self):
        self.data["projects"][0]["runs"] = []
        catalog = self.load()
        self.assertEqual(catalog.resolve("one"), (None, "example/one", "one", None))
        self.assertIsNone(catalog.public()["default_run_id"])
        self.data["projects"] = []
        catalog = self.load()
        self.assertEqual(catalog.resolve(), (None, "", None, None))
        self.assertIsNone(catalog.public()["default_project_id"])

    def test_unknown_or_malformed_ids_fail_generically(self):
        catalog = self.load()
        for project, run in [
            ("unknown", None),
            ("one", "other"),
            ("../private", None),
            ("sk-" + "A" * 32, None),
            ("one", "../private"),
            (True, None),
        ]:
            with (
                self.subTest(project=project, run=run),
                self.assertRaises(ValueError) as error,
            ):
                catalog.resolve(project, run)
            self.assertNotIn(str(self.root), str(error.exception))

    def test_duplicate_ids_extra_fields_and_untrusted_types_are_rejected(self):
        original = copy.deepcopy(self.data)
        mutations = [
            lambda d: d["projects"].append(copy.deepcopy(d["projects"][0])),
            lambda d: d["projects"][0]["runs"].append(
                copy.deepcopy(d["projects"][0]["runs"][0])
            ),
            lambda d: d.update(secret="PRIVATE_SENTINEL"),
            lambda d: d["projects"][0].update(extra=True),
            lambda d: d.update(schema_version=True),
            lambda d: d["projects"][0].update(repository="../private"),
            lambda d: d["projects"][0]["runs"][0].update(snapshot=5),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.data = copy.deepcopy(original)
                mutate(self.data)
                with self.assertRaises(ValueError) as error:
                    self.load()
                self.assertNotIn("PRIVATE_SENTINEL", str(error.exception))

    def test_private_paths_controls_and_token_like_labels_never_leak(self):
        for value in [
            "/home/private/run",
            "C:\\private\\run",
            "unsafe\nlabel",
            "sk-" + "A" * 32,
            "Bearer " + "A" * 32,
        ]:
            with self.subTest(value=value):
                self.data["projects"][0]["label"] = value
                with self.assertRaises(ValueError):
                    self.load()

    def test_missing_snapshot_can_appear_and_change_without_registry_writes_or_commands(
        self,
    ):
        catalog = self.load()
        snapshot = catalog.resolve()[0]
        self.assertFalse(snapshot.exists())
        before = self.path.read_bytes()
        with patch("subprocess.run", side_effect=AssertionError("external operation")):
            snapshot.write_text('{"version":1}')
            self.assertEqual(catalog.resolve()[0].read_text(), '{"version":1}')
            snapshot.write_text('{"version":2}')
            self.assertEqual(catalog.resolve()[0].read_text(), '{"version":2}')
        self.assertEqual(self.path.read_bytes(), before)

    def test_size_count_and_duplicate_json_key_limits(self):
        self.path.write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaises(ValueError):
            load_catalog(self.path)
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError):
            load_catalog(self.path)
        self.data["projects"] = [
            dict(self.data["projects"][0], id=f"p{i}") for i in range(101)
        ]
        with self.assertRaises(ValueError):
            self.load()
