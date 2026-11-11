"""Explicit task contract excerpts from a frozen base, retaining global rules."""

import hashlib
import json
import re
import subprocess

from .modal_worker import valid_path

MAX_FILES = 16
MAX_SOURCE_BYTES = 1024 * 1024
MAX_EXCERPT_BYTES = 24 * 1024


def task_packet(task, project, base_sha, mode="full"):
    """Return private prompt material plus metadata safe for a run plan.

    Task mode requires explicit task-owned ``context_files`` line selectors.
    Required project instructions remain mandatory in both modes. Source text is
    obtained from the selected Git commit, never a mutable working-tree file.
    """
    if mode not in {"full", "task"}:
        raise ValueError("context mode must be full or task")
    if not isinstance(base_sha, str) or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
        raise ValueError("context requires an exact Git commit")
    excerpts = []
    if mode == "task":
        refs = task.get("context_files")
        if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_FILES:
            raise ValueError("task context requires 1–16 explicit context_files selectors")
        if project is None:
            raise ValueError("task context requires a configured project")
        total, seen = 0, set()
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"path", "start_line", "end_line"}:
                raise ValueError("context selector requires path, start_line and end_line")
            path, start, end = ref["path"], ref["start_line"], ref["end_line"]
            if (not valid_path(path) or type(start) is not int or type(end) is not int
                    or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or not 1 <= start <= end <= 100000 or end - start >= 2000):
                raise ValueError("invalid task context selector")
            if path not in project.instructions and not (
                path.startswith("docs/") and path.endswith((".md", ".txt", ".json", ".toml"))
            ):
                raise ValueError("task context must select instruction or contract documents")
            identity = path, start, end
            if identity in seen:
                raise ValueError("duplicate task context selector")
            seen.add(identity)
            try:
                entry = subprocess.check_output(
                    ["git", "ls-tree", "-z", base_sha, "--", path], cwd=project.root,
                    stderr=subprocess.DEVNULL, timeout=10,
                )
                if (not entry or not entry.startswith((b"100644 blob ", b"100755 blob "))
                        or entry.count(b"\x00") != 1):
                    raise ValueError("task context source must be one regular document")
                size = subprocess.check_output(
                    ["git", "cat-file", "-s", f"{base_sha}:{path}"], cwd=project.root,
                    stderr=subprocess.DEVNULL, text=True, timeout=10,
                ).strip()
                if not size.isdigit() or int(size) > MAX_SOURCE_BYTES:
                    raise ValueError("task context source is too large")
                raw = subprocess.check_output(
                    ["git", "show", f"{base_sha}:{path}"], cwd=project.root,
                    stderr=subprocess.DEVNULL, timeout=10,
                )
                text = raw.decode("utf-8")
            except (subprocess.SubprocessError, UnicodeError, OSError):
                raise ValueError("task context source is unavailable at the selected base") from None
            lines = text.splitlines(keepends=True)
            if end > len(lines) or "\x00" in text:
                raise ValueError("task context line range is unavailable")
            excerpt = "".join(lines[start - 1:end])
            total += len(excerpt.encode())
            if total > MAX_EXCERPT_BYTES:
                raise ValueError("task context excerpts exceed 24 KiB")
            excerpts.append({"path": path, "start_line": start, "end_line": end,
                             "source_sha256": hashlib.sha256(raw).hexdigest(),
                             "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                             "text": excerpt})
    binding = {"mode": mode, "base_sha": base_sha, "task": task,
               "required_instructions": list(project.instructions) if project else ["README.md", "AGENTS.md"],
               "excerpts": excerpts}
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    metadata = {"mode": mode, "sha256": hashlib.sha256(encoded).hexdigest(),
                "excerpt_count": len(excerpts),
                "excerpt_bytes": sum(len(e["text"].encode()) for e in excerpts),
                "required_instructions_preserved": True}
    return excerpts, metadata
