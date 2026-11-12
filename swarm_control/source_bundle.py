"""One bounded, private, self-contained Git source bundle per explicit run."""

import asyncio
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_BUNDLE_BYTES = 64 * 1024 * 1024
PREPARATION_SECONDS = 120
UPLOAD_SECONDS = 120
WORKER_BUNDLE_PATH = "/tmp/swarm-source.bundle"
BUNDLE_REF = "refs/heads/swarm-source"
_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "core.attributesFile=/dev/null", "-c", "protocol.allow=never",
        "-c", "protocol.file.allow=always", "-c", "fetch.recurseSubmodules=false",
        "-c", "submodule.recurse=false", "-c", "uploadpack.packObjectsHook=",
        "-c", "gc.auto=0", "-c", "maintenance.auto=false"]


class SourceBundleError(ValueError):
    """Fixed diagnostics exclude command output and private source paths."""


def _environment():
    removed = {"GH_TOKEN", "GITHUB_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY",
               "OPENAI_ADMIN_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "SSH_ASKPASS"}
    env = {key: value for key, value in os.environ.items()
           if key not in removed and not key.startswith(("GIT_", "MODAL_OAUTH_"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file")
    return env


def _git(root, arguments, deadline, *, growing=None, maximum=MAX_BUNDLE_BYTES,
         bounded_file=None, maximum_file_bytes=MAX_BUNDLE_BYTES):
    process = None
    try:
        if time.monotonic() >= deadline:
            raise SourceBundleError("Source bundle preparation exceeded its time limit")
        process = subprocess.Popen([*_GIT, *arguments], cwd=root, env=_environment(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None or selector.get_map():
                if time.monotonic() >= deadline:
                    raise SourceBundleError("Source bundle preparation exceeded its time limit")
                if bounded_file is not None and bounded_file.exists() and bounded_file.stat().st_size > maximum_file_bytes:
                    raise SourceBundleError("Source bundle exceeded its bounded storage limit")
                if growing is not None:
                    total, count = 0, 0
                    for parent, _, files in os.walk(growing):
                        for filename in files:
                            count += 1
                            path = Path(parent) / filename
                            try:
                                total += path.stat().st_size
                            except FileNotFoundError:
                                continue
                            if total > maximum or count > 32768:
                                raise SourceBundleError("Source bundle exceeded its bounded storage limit")
                for key, _ in selector.select(.01):
                    chunk = os.read(key.fd, 8193 - len(output))
                    if chunk:
                        output.extend(chunk)
                        if len(output) > 8192:
                            raise SourceBundleError("Source bundle Git output exceeded its limit")
                    else:
                        selector.unregister(key.fileobj)
        if process.returncode != 0:
            raise SourceBundleError("Source bundle could not be built offline from the selected complete base")
        return bytes(output)
    except OSError:
        raise SourceBundleError("Local Git source bundle tooling is unavailable") from None
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.SubprocessError:
                pass
            process.stdout.close()


def _private_directory(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise SourceBundleError("Source bundle storage must not use symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SourceBundleError("Source bundle storage must be private to its owner")


@dataclass(frozen=True)
class SourceBundle:
    path: Path
    base_sha: str
    sha256: str
    byte_count: int

    def metadata(self):
        return {"schema_version": 1, "transport": "bundle", "base_sha": self.base_sha,
                "sha256": self.sha256, "bytes": self.byte_count, "worker_path": WORKER_BUNDLE_PATH}

    def upload(self, sandbox, *, timeout=UPLOAD_SECONDS):
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size != self.byte_count
                or not 0 < self.byte_count <= MAX_BUNDLE_BYTES):
            raise SourceBundleError("Prepared source bundle changed or is unsafe")

        async def copy():
            # Modal 1.6.3 exposes filesystem.copy_from_local(.aio), not Sandbox.open.
            await asyncio.wait_for(sandbox.filesystem.copy_from_local.aio(
                str(self.path), WORKER_BUNDLE_PATH), timeout=timeout)
        asyncio.run(copy())


def build_source_bundle(root, base_sha, state, *, timeout=PREPARATION_SECONDS, maximum=MAX_BUNDLE_BYTES):
    """Build locally before resource admission; mutate no target refs/index/files."""
    if not isinstance(base_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", base_sha):
        raise SourceBundleError("Source bundles require an exact SHA-1 commit")
    if type(maximum) is not int or not 1 <= maximum <= MAX_BUNDLE_BYTES or not 0 < timeout <= PREPARATION_SECONDS:
        raise SourceBundleError("Invalid bounded source bundle policy")
    root, state = Path(root).absolute(), Path(state).absolute()
    start, started = time.monotonic(), datetime.now(UTC).isoformat()
    deadline = start + timeout
    common = Path(_git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"], deadline).decode().strip())
    if state != common.parent / ".swarm":
        raise SourceBundleError("Source bundle cache must use the repository's common private state")
    # Require an existing ignore rule; this feature does not edit target ignore files.
    _git(common.parent, ["check-ignore", "--no-index", "-q", ".swarm/source-bundles/"], deadline)
    _git(root, ["cat-file", "-e", base_sha + "^{commit}"], deadline)
    if _git(root, ["rev-parse", "--is-shallow-repository"], deadline).strip() != b"false":
        raise SourceBundleError("Source bundle requires a complete local history; shallow clones are unsupported")
    cache = state / "source-bundles"
    _private_directory(cache)
    directory = cache / ("source-" + uuid.uuid4().hex)
    _private_directory(directory)
    receipt = {"schema_version": 1, "started_at_utc": started, "base_sha": base_sha,
               "status": "preparing", "maximum_bytes": maximum, "timeout_seconds": timeout,
               "cost_usd": None, "provider_calls": 0, "target_modified": False}
    temporary, bundle = directory / "objects.git", directory / "source.bundle"
    try:
        template = directory / "empty-template"
        template.mkdir(mode=0o700)
        _git(None, ["init", "--bare", "--quiet", f"--template={template}", str(temporary)], deadline)
        _git(temporary, ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "--",
                         str(root), base_sha + ":" + BUNDLE_REF], deadline,
             growing=temporary, maximum=maximum * 3)
        _git(temporary, ["bundle", "create", "--version=2", str(bundle), BUNDLE_REF], deadline,
             growing=directory, maximum=maximum * 4, bounded_file=bundle, maximum_file_bytes=maximum)
        size = bundle.stat().st_size
        if not 0 < size <= maximum:
            raise SourceBundleError("Source bundle exceeds 64 MiB or the selected lower limit")
        bundle.chmod(0o600)
        expected = f"{base_sha} {BUNDLE_REF}\n".encode()
        if _git(temporary, ["bundle", "list-heads", str(bundle)], deadline) != expected:
            raise SourceBundleError("Source bundle refs differ from the selected base")
        _git(temporary, ["bundle", "verify", str(bundle)], deadline)
        with bundle.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if time.monotonic() >= deadline:
            raise SourceBundleError("Source bundle preparation exceeded its time limit")
        result = SourceBundle(bundle, base_sha, digest, size)
        receipt.update(status="ready", **result.metadata())
        return result
    except BaseException as error:
        receipt.update(status="failed", error_type=type(error).__name__)
        if bundle.exists():
            receipt["partial_bytes"] = bundle.stat().st_size
            bundle.unlink()
        raise
    finally:
        receipt["cleanup_errors"] = []
        for leftover in (temporary, directory / "empty-template"):
            if leftover.exists():
                try:
                    shutil.rmtree(leftover)
                except OSError as error:
                    receipt["cleanup_errors"].append({"component": leftover.name, "error_type": type(error).__name__})
        receipt.update(finished_at_utc=datetime.now(UTC).isoformat(), elapsed_seconds=time.monotonic() - start)
        path = directory / "receipt.json"
        with path.open("x") as stream:
            os.chmod(path, 0o600)
            json.dump(receipt, stream, indent=2)
            stream.write("\n")
