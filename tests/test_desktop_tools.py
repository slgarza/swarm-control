"""Installer exercises local archives only; no download or real tool execution."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from swarm_control import desktop_tools as tools
from swarm_control.connections import Connections
from swarm_control.setup_common import SetupError


class Download(io.BytesIO):
    def geturl(self):
        return "https://release-assets.githubusercontent.com/official-fixture"


class DesktopToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name) / "private/workspace.json"
        self.binary = b"#!/bin/sh\nexit 0\n"
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as container:
            member = tarfile.TarInfo(f"gh_{tools.VERSION}_linux_amd64/bin/gh")
            member.size = len(self.binary)
            container.addfile(member, io.BytesIO(self.binary))
            # A foreign archive path is never extracted.
            unrelated = tarfile.TarInfo("../../outside")
            unrelated.size = 1
            container.addfile(unrelated, io.BytesIO(b"x"))
        self.archive = archive.getvalue()

    def install(self, digest=None):
        with (patch.object(tools.platform, "system", return_value="Linux"),
              patch.object(tools.platform, "machine", return_value="x86_64"),
              patch.dict(tools.ASSETS, {("Linux", "x86_64"): ("linux_amd64.tar.gz", digest or hashlib.sha256(self.archive).hexdigest())}),
              patch.object(tools.urllib.request, "urlopen", return_value=Download(self.archive))):
            return tools.install_github(self.workspace)

    def test_only_exact_binary_installs_after_checksum_and_local_receipt_verifies_before_path_use(self):
        result = self.install()
        root = tools.tools_directory(self.workspace)
        self.assertTrue(result["checksum_verified"])
        self.assertEqual((root / "gh").read_bytes(), self.binary)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.workspace.parent.parent / "outside").exists())
        receipt = json.loads(next(root.glob("installation-*.json")).read_text())
        with patch.dict(tools.ASSETS, {("Linux", "x86_64"): ("linux_amd64.tar.gz", receipt["archive_sha256"])}):
            self.assertEqual(tools.local_github(self.workspace), root / "gh")
            env = Connections(self.workspace, base_environment={"PATH": "/usr/bin"}).environment()
            self.assertEqual(env["PATH"].split(":")[0], str(root))
            (root / "gh").write_bytes(b"changed executable")
            self.assertIsNone(tools.local_github(self.workspace))

    def test_checksum_mismatch_and_failed_receipt_never_install_executable(self):
        with self.assertRaises(SetupError):
            self.install(digest="0" * 64)
        self.assertFalse((tools.tools_directory(self.workspace) / "gh").exists())
        with patch("swarm_control.setup_common.private_json", side_effect=OSError("disk unavailable")), self.assertRaises(SetupError):
            self.install()
        self.assertFalse((tools.tools_directory(self.workspace) / "gh").exists())
