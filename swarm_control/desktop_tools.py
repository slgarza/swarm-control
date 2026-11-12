"""Optional per-user GitHub CLI installation from a pinned official release."""

import hashlib
import io
import json
import os
import platform
import shutil
import stat
import tarfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "2.103.3"
# Official release asset digests from GitHub's cli/cli v2.103.3 release API.
# This installer never evaluates a remote shell script or extracts an archive tree.
ASSETS = {
    ("Linux", "x86_64"): ("linux_amd64.tar.gz", "4c806978ffce1cec70853302a56d70ac6da4f19bae7adca4e1a8ab00633dd91d"),
    ("Linux", "aarch64"): ("linux_arm64.tar.gz", "ee32df2323c8847f5fac6cb6b4b69ca75828e9329a8f22f677021f7b3306ce63"),
    ("Darwin", "x86_64"): ("macOS_amd64.zip", "a2f86418f4350ae6e9ec60d5000bdf7665728436946a97fc27fe50e6ae752682"),
    ("Darwin", "arm64"): ("macOS_arm64.zip", "d700207eae9ae98fe2b89535c2c087c07823d4628c56364695a5a07ef311c062"),
}


def tools_directory(workspace_path):
    path = Path(workspace_path).expanduser().absolute()
    return path.with_name(path.name + ".tools")


def local_github(workspace_path):
    root = tools_directory(workspace_path)
    local = root / "gh"
    if (root.is_symlink() or local.is_symlink() or not local.is_file() or local.stat().st_size > 100 * 1024 * 1024
            or root.stat().st_mode & 0o077 or local.stat().st_uid != os.getuid() or not os.access(local, os.X_OK)):
        return None
    digest = None
    for receipt in sorted(root.glob("installation-*.json"))[-100:]:
        if receipt.is_symlink() or receipt.stat().st_size > 4096:
            continue
        try:
            record = json.loads(receipt.read_text())
        except (OSError, ValueError):
            continue
        if record.get("version") == VERSION and record.get("archive_sha256") in {row[1] for row in ASSETS.values()}:
            if digest is None:
                digest = hashlib.sha256(local.read_bytes()).hexdigest()
            if record.get("binary_sha256") == digest:
                return local
    return None


def status(workspace_path):
    available = local_github(workspace_path) is not None or shutil.which("gh") is not None
    return {"github_cli": {"available": bool(available), "installable": (platform.system(), platform.machine()) in ASSETS,
                           "installer_version": VERSION},
            "git": {"available": shutil.which("git") is not None},
            "python": {"available": True}}


def install_github(workspace_path):
    from .setup_common import SetupError, private_json, require
    artifact = ASSETS.get((platform.system(), platform.machine()))
    require(artifact is not None, "This platform needs its own GitHub CLI installation. Linux/WSL and macOS x64/ARM64 are supported.")
    root = tools_directory(workspace_path)
    require(not any(path.is_symlink() or (path / ".git").exists() for path in (root, *root.parents)),
            "Desktop tools must be outside repositories and symlink directories.")
    if (root / "gh").exists():
        require(local_github(workspace_path) is not None, "The desktop tool differs from its installation receipt. Inspect it before installing another copy.")
        return status(workspace_path)
    suffix, expected = artifact
    filename = f"gh_{VERSION}_{suffix}"
    url = f"https://github.com/cli/cli/releases/download/v{VERSION}/{filename}"
    request = urllib.request.Request(url, headers={"User-Agent": "swarm-control-desktop-setup"})
    try:
        deadline = time.monotonic() + 120
        archive = bytearray()
        with urllib.request.urlopen(request, timeout=30) as response:
            parsed = urlsplit(response.geturl())
            require(parsed.scheme == "https" and parsed.hostname in {
                "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"},
                "The tool download redirected outside its official distribution hosts.")
            while True:
                require(time.monotonic() < deadline, "The tool download timed out.")
                chunk = response.read(65536)
                if not chunk:
                    break
                archive.extend(chunk)
                require(len(archive) <= 64 * 1024 * 1024, "The tool download exceeded its size limit.")
        require(hashlib.sha256(archive).hexdigest() == expected, "The official release checksum did not match. No tool was installed.")
        prefix = filename.removesuffix(".tar.gz").removesuffix(".zip")
        member_name = prefix + "/bin/gh"
        if suffix.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(archive)) as container:
                info = container.getinfo(member_name)
                require(not stat.S_ISLNK(info.external_attr >> 16) and info.file_size <= 100 * 1024 * 1024,
                        "The tool archive entry is unsafe.")
                binary = container.read(info)
        else:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as container:
                info = container.getmember(member_name)
                require(info.isfile() and info.size <= 100 * 1024 * 1024, "The tool archive entry is unsafe.")
                binary = container.extractfile(info).read()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        receipt = root / ("installation-" + uuid.uuid4().hex + ".json")
        private_json(receipt, {"version": VERSION, "source": url, "archive_sha256": expected,
                              "binary_sha256": hashlib.sha256(binary).hexdigest(), "administrator_access": False})
        temporary = root / ("gh-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, 0o700)
                stream.write(binary)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, root / "gh")
        finally:
            temporary.unlink(missing_ok=True)
        return {**status(workspace_path), "installed": True, "source": "official_github_cli_release",
                "checksum_verified": True, "administrator_access": False}
    except SetupError:
        raise
    except Exception:  # noqa: BLE001 - network/archive exceptions must not become HTTP responses
        raise SetupError("tool_install_failed", "The GitHub CLI download could not complete. Check the connection and desktop tool directory before retrying.") from None
