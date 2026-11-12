"""Explicit task contract excerpts from a frozen base, retaining global rules."""

import hashlib
import json
import os
import re
import subprocess

from .modal_worker import acceptance_commands, acceptance_files, valid_path

MAX_FILES = 16
MAX_SOURCE_BYTES = 1024 * 1024
MAX_EXCERPT_BYTES = 24 * 1024
MAX_READINESS_TASKS = 10000
MAX_INSTRUCTION_BYTES = 4 * 1024 * 1024
MAX_READINESS_FILES = 4096
MAX_READINESS_BLOB_BYTES = 128 * 1024 * 1024


def _frozen_environment():
    return {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1"}


def _frozen_document(project, base_sha, path, *, text=True):
    if (not valid_path(path) or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or not isinstance(base_sha, str) or re.fullmatch(r"[a-f0-9]{40}", base_sha) is None):
        raise ValueError("readiness requires a regular document at an exact base")
    try:
        entry = subprocess.check_output(["git", "ls-tree", "-z", base_sha, "--", path],
            cwd=project.root, stderr=subprocess.DEVNULL, timeout=10, env=_frozen_environment())
        if not entry.startswith((b"100644 blob ", b"100755 blob ")) or entry.count(b"\0") != 1:
            raise ValueError("readiness source must be one regular file")
        size = subprocess.check_output(["git", "cat-file", "-s", f"{base_sha}:{path}"],
            cwd=project.root, stderr=subprocess.DEVNULL, text=True, timeout=10,
            env=_frozen_environment()).strip()
        if not size.isdigit() or int(size) > MAX_SOURCE_BYTES:
            raise ValueError("readiness source exceeds the file size limit")
        raw = subprocess.check_output(["git", "show", f"{base_sha}:{path}"],
            cwd=project.root, stderr=subprocess.DEVNULL, timeout=10, env=_frozen_environment())
        if text:
            raw.decode("utf-8")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ValueError("readiness source is unavailable at the selected base") from None
    return raw


def build_context_readiness(tasks, project, base_sha, context_mode="full", packets=None):
    """Inspect frozen context once per document, without counting guessed tokens.

    Public metadata contains digests and sizes, never source text or local paths.
    Missing selectors are a full-mode warning, not permission to omit global rules.
    """
    if (context_mode not in {"full", "task"} or not isinstance(tasks, (list, tuple))
            or not 1 <= len(tasks) <= MAX_READINESS_TASKS):
        raise ValueError("context readiness needs bounded tasks and a known context mode")
    if project is None:
        raise ValueError("context readiness requires a project")
    cache = {}
    scanned_bytes = 0

    def frozen(path, *, text=True):
        nonlocal scanned_bytes
        if path not in cache:
            if len(cache) >= MAX_READINESS_FILES:
                raise ValueError("context readiness exceeds the distinct file limit")
            raw = _frozen_document(project, base_sha, path, text=text)
            scanned_bytes += len(raw)
            if scanned_bytes > MAX_READINESS_BLOB_BYTES:
                raise ValueError("context readiness exceeds the total input size limit")
            # Retain metadata rather than every protected source blob in RAM.
            cache[path] = len(raw), hashlib.sha256(raw).hexdigest()
        return cache[path]

    instruction_rows, instruction_bytes = [], 0
    for name in project.instructions:
        size, digest = frozen(name)
        instruction_bytes += size
        if instruction_bytes > MAX_INSTRUCTION_BYTES:
            raise ValueError("required instructions exceed the readiness size limit")
        instruction_rows.append({"byte_count": size, "sha256": digest})
    rows, warnings, seen = [], [], set()
    for task in tasks:
        task_id = task.get("id")
        if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Z][A-Z0-9_-]{1,39}", task_id)
                or task_id in seen):
            raise ValueError("context readiness needs unique valid task IDs")
        seen.add(task_id)
        commands = acceptance_commands(task)
        protected = acceptance_files(task)
        protected_sha = []
        for name in protected:
            _, digest = frozen(name, text=False)
            protected_sha.append(digest)
        metadata = (packets or {}).get(task_id, {}).get("metadata")
        if metadata is None:
            _, metadata = task_packet(task, project, base_sha, context_mode)
        refs = task.get("context_files")
        has_context = isinstance(refs, list) and bool(refs)
        prompt_bytes = metadata.get("prompt_bytes")
        if prompt_bytes is not None and (type(prompt_bytes) is not int or prompt_bytes < 0):
            raise ValueError("invalid context packet size")
        rows.append({"task_id": task_id, "context_selectors_present": has_context,
                     "configured_acceptance_checks": commands is not None,
                     "protected_acceptance_inputs": len(protected),
                     "protected_acceptance_input_sha256": protected_sha,
                     "prompt_bytes": prompt_bytes, "prompt_sha256": metadata.get("prompt_sha256"),
                     "excerpt_bytes": metadata["excerpt_bytes"], "context_sha256": metadata["sha256"]})
    if any(not row["context_selectors_present"] for row in rows):
        warnings.append("task_context_selectors_missing")
    if any(not row["configured_acceptance_checks"] for row in rows):
        warnings.append("task_acceptance_checks_missing")
    if instruction_bytes > 48 * 1024:
        warnings.append("large_required_instruction_payload")
    known_prompt_bytes = [row["prompt_bytes"] for row in rows if row["prompt_bytes"] is not None]
    max_prompt = max(known_prompt_bytes, default=None)
    if max_prompt is not None and max_prompt + instruction_bytes > 64 * 1024:
        warnings.append("large_context_payload_review_required")
    warnings.append("context_token_count_unmeasured")
    return {"schema_version": 1, "scope": "frozen_context_metadata_only", "base_sha": base_sha,
            "mode": context_mode, "instruction_count": len(instruction_rows),
            "instruction_bytes": instruction_bytes, "instruction_digests": instruction_rows,
            "task_count": len(rows), "tasks_with_context": sum(row["context_selectors_present"] for row in rows),
            "tasks_with_acceptance_checks": sum(row["configured_acceptance_checks"] for row in rows),
            "total_prompt_bytes": sum(known_prompt_bytes) if len(known_prompt_bytes) == len(rows) else None,
            "max_prompt_bytes": max_prompt, "token_count": None,
            "token_count_reason": "No tokenizer or provider token-count observation was performed",
            "required_instructions_preserved": True, "independent_acceptance_verified": False,
            "authorizes_spend": False, "warnings": warnings, "tasks": rows}


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
                    stderr=subprocess.DEVNULL, timeout=10, env=_frozen_environment(),
                )
                if (not entry or not entry.startswith((b"100644 blob ", b"100755 blob "))
                        or entry.count(b"\x00") != 1):
                    raise ValueError("task context source must be one regular document")
                size = subprocess.check_output(
                    ["git", "cat-file", "-s", f"{base_sha}:{path}"], cwd=project.root,
                    stderr=subprocess.DEVNULL, text=True, timeout=10, env=_frozen_environment(),
                ).strip()
                if not size.isdigit() or int(size) > MAX_SOURCE_BYTES:
                    raise ValueError("task context source is too large")
                raw = subprocess.check_output(
                    ["git", "show", f"{base_sha}:{path}"], cwd=project.root,
                    stderr=subprocess.DEVNULL, timeout=10, env=_frozen_environment(),
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
