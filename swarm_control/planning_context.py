"""Bounded, read-only source context. Repository prose is never executed."""

import codecs
import hashlib
import json
import os
import re
import stat
from pathlib import PurePosixPath

from .repository_setup import _relative
from .setup_common import require

MAX_CONTEXT_BYTES = 65536
MAX_TRACKED_FILES = 10000
MAX_AUDIT_BYTES = 128 * 1024 * 1024
MAX_EXCERPT_BYTES = 6144
EXCLUDED_PARTS = {".git", ".swarm", ".ssh", ".aws", ".kube", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "coverage"}
EXCLUDED_NAMES = {"credentials.json", "settings.json", "service-account.json", "id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc"}
LOCK_NAMES = {"package-lock.json", "uv.lock", "poetry.lock"}
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)\b[A-Za-z0-9_]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key)"
    r"[A-Za-z0-9_]*[\"']?\s*[:=]\s*[^\r\n]+")
_OTHER_SECRET = re.compile(r"\b(?:AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{25,}|xox[baprs]-[A-Za-z0-9-]{12,})\b")


def scrub_source(text, scrub):
    text = scrub(text)
    text = _SECRET_ASSIGNMENT.sub("[redacted credential assignment]", text)
    text = _OTHER_SECRET.sub("[redacted]", text)
    return re.sub(r"-----BEGIN [^\r\n]*PRIVATE KEY-----[\s\S]*", "[redacted private key]", text)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def safe_source_path(name):
    path = PurePosixPath(name)
    return (_relative(name) and not any(part in EXCLUDED_PARTS for part in path.parts)
            and not any(part.lower().startswith(".env") or any(word in part.lower() for word in ("secret", "credential", "service_account", "service-account"))
                        for part in path.parts)
            and path.name.lower() not in EXCLUDED_NAMES
            and path.suffix.lower() not in {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db"})


def _priority(name):
    base = PurePosixPath(name).name.lower()
    if name.lower() in {"readme.md", "agents.md", "swarm.toml"}:
        return 0, name
    if base in {"package.json", "pyproject.toml", "cargo.toml", "go.mod", "makefile", "requirements.txt"}:
        return 1, name
    if name.startswith(".github/workflows/"):
        return 2, name
    if PurePosixPath(name).suffix.lower() in {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".rb", ".cs", ".cpp", ".h", ".c", ".swift", ".kt", ".html", ".css", ".svelte", ".vue", ".sql"}:
        return 3, name
    if name.startswith("docs/"):
        return 4, name
    return 5, name


def audit(repository, path, scrub, *, binding_exclusions=()):
    """Hash bounded regular tracked inputs; select and scrub at most 64 KiB.

    Secrets, symlinks, generated/runtime files and untracked source are excluded
    from model context. Untracked path identities still bind apply admission.
    No filters, hooks, target commands, network requests or source imports run.
    """
    root = repository._root(path)
    base = repository._git(root, "rev-parse", "HEAD").decode().strip()
    names = repository._git(root, "ls-files", "-z").decode("utf-8", "strict").split("\0")
    names = sorted({name for name in names if name})
    require(len(names) <= MAX_TRACKED_FILES, "This repository exceeds the bounded planning inventory. Narrow its tracked source first.", "planning_source_limit")
    status = repository._git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    index = repository._git(root, "ls-files", "--stage", "-z")
    others = repository._git(root, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8", "strict").split("\0")
    untracked = {name for name in others if name and safe_source_path(name)} - set(names)
    require(len(names) + len(untracked) <= MAX_TRACKED_FILES,
            "This repository exceeds the bounded source inventory.", "planning_source_limit")
    files, fingerprints, excerpts, total, selected_bytes = [], [], [], 0, 0
    for name in sorted(({name for name in names if safe_source_path(name)} | untracked), key=_priority):
        target = root / name
        # Every path component is checked; directory symlinks cannot escape root.
        if any(part.is_symlink() for part in [target, *target.parents] if part != root.parent):
            fingerprints.append([name, "symlink-excluded"])
            continue
        try:
            info = target.lstat()
        except FileNotFoundError:
            fingerprints.append([name, "missing"])
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            fingerprints.append([name, "nonregular-excluded"])
            continue
        total += info.st_size
        require(total <= MAX_AUDIT_BYTES, "This repository exceeds the bounded planning source size. Narrow its tracked source first.", "planning_source_limit")
        hasher, sample = hashlib.sha256(), bytearray()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            require(stat.S_ISREG(opened.st_mode) and opened.st_ino == info.st_ino,
                    "Source changed during inspection. Inspect it again.", "planning_source_changed")
            observed = 0
            while chunk := stream.read(65536):
                observed += len(chunk)
                require(observed <= info.st_size, "Source changed during inspection. Inspect it again.", "planning_source_changed")
                hasher.update(chunk)
                if len(sample) < MAX_EXCERPT_BYTES:
                    sample.extend(chunk[:MAX_EXCERPT_BYTES - len(sample)])
            after = os.fstat(stream.fileno())
        require(observed == info.st_size and after.st_mtime_ns == info.st_mtime_ns,
                "Source changed during inspection. Inspect it again.", "planning_source_changed")
        fingerprints.append([name, hasher.hexdigest(), info.st_size])
        # Untracked files bind local drift, but are never sent to the model.
        # Credential-bearing names remain in the private source identity, while
        # neither the public inventory nor any outbound path exposes their text.
        if name in untracked or scrub_source(name, scrub) != name:
            continue
        row = {"path": name, "bytes": info.st_size, "selected": False}
        files.append(row)
        if b"\0" in sample or PurePosixPath(name).name in LOCK_NAMES:
            continue
        try:
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            text = decoder.decode(bytes(sample), final=info.st_size <= len(sample))
        except UnicodeDecodeError:
            continue
        text = scrub_source(text, scrub)
        excerpt = {"path": name, "text": text, "truncated": info.st_size > len(sample)}
        size = len(canonical(excerpt))
        if selected_bytes + size <= MAX_CONTEXT_BYTES and len(excerpts) < 40:
            excerpts.append(excerpt)
            selected_bytes += size
            row["selected"] = True
    identity = {"path": str(root), "repository": repository._origin(root), "base_sha": base,
                "status_sha256": digest(status), "index_sha256": digest(index), "files": fingerprints}
    protected = {key: value for key, value in identity.items() if key not in {"status_sha256", "files"}}
    protected["files"] = [row for row in fingerprints if row[0] not in binding_exclusions]
    # The second read detects ordinary edits during the bounded snapshot itself.
    require(repository._git(root, "rev-parse", "HEAD").decode().strip() == base
            and repository._git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") == status
            and repository._git(root, "ls-files", "--stage", "-z") == index,
            "Source changed during inspection. Inspect it again.", "planning_source_changed")
    return {"path": str(root), "repository": identity["repository"], "base_sha": base,
            "source_binding": digest(identity), "files": files, "selected_bytes": selected_bytes,
            "protected_binding": digest(protected),
            "excerpts": excerpts, "tracked_file_count": len(names)}
