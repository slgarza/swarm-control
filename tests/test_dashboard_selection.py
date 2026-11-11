"""Actual loopback HTTP checks for project isolation and source selection."""

import http.client
import json
import threading
import unittest
from datetime import timedelta

from swarm_control.dashboard import load_snapshot, make_server
from tests.test_dashboard import SnapshotFixture, task


class CatalogHTTPTests(SnapshotFixture):
    def setUp(self):
        super().setUp()
        self.catalog = self.root / "catalog.json"
        self.config = {"schema_version": 1, "workspace_label": "Local projects", "projects": [
            {"id": "first", "label": "First project", "repository": "example/project", "runs": [
                {"id": "current", "label": "Current run", "snapshot": str(self.path)},
                {"id": "missing", "label": "Missing source", "snapshot": "absent.json"}]},
            {"id": "empty", "label": "Empty project", "repository": "example/empty", "runs": []}]}
        self.catalog.write_text(json.dumps(self.config))
        self.server = make_server(catalog_path=self.catalog)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
        self.addCleanup(cleanup)

    def request(self, target, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", target, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_catalog_has_only_public_metadata_and_no_filesystem_paths(self):
        code, raw = self.request("/api/catalog")
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(raw)["projects"]), 2)
        self.assertNotIn(str(self.root).encode(), raw)
        self.assertNotIn(b"snapshot", raw)

    def test_selecting_empty_or_missing_source_does_not_return_previous_project(self):
        code, raw = self.request("/api/snapshot?project=first&run=current")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["selected_project_id"], "first")
        for query in ("project=empty", "project=first&run=missing"):
            with self.subTest(query=query):
                code, raw = self.request("/api/snapshot?" + query)
                self.assertEqual(code, 503)
                self.assertFalse(json.loads(raw)["available"])
                self.assertNotIn(b"original_patch", raw)
                self.assertNotIn(str(self.path).encode(), raw)

    def test_cross_project_or_unknown_query_cannot_select_arbitrary_file(self):
        for query in ("project=empty&run=current", "project=unknown", "path=private.txt",
                      "project=first&project=empty", "project=", "run=../../private"):
            with self.subTest(query=query):
                code, raw = self.request("/api/snapshot?" + query)
                self.assertEqual(code, 400)
                self.assertNotIn(str(self.root).encode(), raw)

    def test_selected_snapshot_updates_are_read_without_restarting_catalog(self):
        self.source["rows"].append(task("T02"))
        self.save()
        code, raw = self.request("/api/snapshot?project=first&run=current")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["summary"]["assigned"], 2)

    def test_catalog_is_not_cross_site_readable(self):
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://foreign.invalid"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/catalog", headers)[0], 403)

    def test_selected_repo_must_match_the_snapshot_repo(self):
        self.source["repository"] = "example/unrelated"
        self.save()
        code, raw = self.request("/api/snapshot?project=first&run=current")
        self.assertEqual(code, 503)
        self.assertFalse(json.loads(raw)["available"])
        self.assertNotIn(b"example/unrelated", raw)

    def test_expired_window_never_fabricates_a_completed_cohort_clock(self):
        self.source["clock"]["delivery_cutoff_utc"] = (self.now - timedelta(minutes=5)).isoformat()
        self.save()
        projected = load_snapshot(self.path, now=self.now)
        self.assertEqual(projected["phase"], "window_closed")
        self.assertIsNone(projected["clock"]["t1"])
        self.source["as_of_utc"] = (self.now - timedelta(minutes=10)).isoformat()
        self.save()
        self.assertEqual(load_snapshot(self.path, now=self.now)["phase"], "integration")


if __name__ == "__main__":
    unittest.main()
