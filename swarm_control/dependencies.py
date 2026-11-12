"""Bounded dependency and parallel-frontier analysis without executing target code.

Declared prerequisites and unique artifact providers form directed edges. Reads,
ownership and limited static imports supply conservative review hints instead of
inventing a direction between tasks which happen to touch related code.
"""

import ast
import bisect
import hashlib
import heapq
import re
import subprocess
import threading
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath

from .modal_worker import valid_path

MAX_TASKS = 10000
MAX_RESOURCES = 256
MAX_RESOURCE_ENTRIES = 100000
MAX_EDGES = 200000
MAX_HINTS = 2000
MAX_DIAGNOSTIC_TASKS = 100
MAX_CYCLES = 100
MAX_TREE_BYTES = 16 * 1024 * 1024
MAX_TREE_FILES = 100000
MAX_SCAN_FILES = 1000
MAX_SOURCE_BYTES = 256 * 1024
MAX_SCAN_BYTES = 8 * 1024 * 1024
TASK_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,39}")
ARTIFACT = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{0,119}")
SECRET = re.compile(r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", re.IGNORECASE)
JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
SOURCE_SUFFIXES = (*JS_SUFFIXES, ".py")
JS_IMPORT = re.compile(
    r"(?:^\s*(?:import|export)\s+(?:[^\n;]{0,500}?\s+from\s*)?|\b(?:import|require)\s*\()"
    r"\s*['\"](\.[^'\"\n]{0,200})['\"]", re.MULTILINE,
)


class TaskResourceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _id(value):
    return isinstance(value, str) and TASK_ID.fullmatch(value) is not None and not SECRET.search(value)


def _path(value):
    return (valid_path(value) and len(value) <= 1024
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and not SECRET.search(value))


def _artifact(value):
    return (isinstance(value, str) and ARTIFACT.fullmatch(value) is not None
            and not SECRET.search(value) and ".." not in value.split("/"))


def validate_task_resources(task):
    """Validate optional resource metadata; existing task definitions stay valid.

    ``produces`` and ``consumes`` contain stable artifact identities, not prose or
    credentials. ``reads`` contains repository-relative files or directories.
    Callers can reuse this at their ordinary task-validation boundary.
    """
    if not isinstance(task, dict):
        raise TypeError("task resource metadata requires an object")
    for name in ("produces", "consumes", "reads"):
        values = task.get(name, [])
        check = _path if name == "reads" else _artifact
        if (not isinstance(values, list) or len(values) > MAX_RESOURCES
                or any(not check(value) for value in values)
                or len(set(values)) != len(values)):
            raise ValueError(f"{name} must be a bounded list of unique safe resource identities")
    historical = (task.get("status") == "merged" and type(task.get("pullRequest")) is int
                  and task["pullRequest"] > 0)
    if not historical:
        ownership = task.get("ownership", [])
        for artifact in task.get("produces", []):
            if (":" not in artifact and "/" in artifact and PurePosixPath(artifact).suffix
                    and (not isinstance(ownership, list) or not any(
                        _path(path) and (artifact == path or artifact.startswith(path + "/"))
                        for path in ownership))):
                raise TaskResourceError("produced_path_outside_ownership",
                                        "produced file paths must stay inside task ownership")


def _overlap(left, right):
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


class _Paths:
    """Path trie for overlap lookups without comparing every pair of tasks."""

    def __init__(self):
        self.root = {"owners": set(), "ordered": None, "children": {}}

    def add(self, path, task_id):
        current = self.root
        for part in path.split("/"):
            current = current["children"].setdefault(part, {"owners": set(), "ordered": None, "children": {}})
        current["owners"].add(task_id)
        current["ordered"] = None

    def overlapping(self, path, *, limit=None):
        result, current = set(), self.root

        def include(node):
            if limit is None:
                result.update(node["owners"])
            else:
                if node["ordered"] is None:
                    node["ordered"] = sorted(node["owners"])
                for owner in node["ordered"]:
                    result.add(owner)
                    if len(result) >= limit:
                        return True
            return False

        for part in path.split("/"):
            if include(current):
                return result
            current = current["children"].get(part)
            if current is None:
                return result
        stack = [current]
        while stack:
            current = stack.pop()
            if include(current):
                return result
            stack.extend(current["children"].values())
        return result


class _FrozenSource:
    def __init__(self, root, base_sha):
        self.root = Path(root)
        self.base_sha = base_sha
        self.files = {}
        self.inventory_sha256 = None
        self.scan = {"enabled": True, "files_scanned": 0, "source_bytes_scanned": 0,
                     "truncated": False, "unavailable_files": 0,
                     "parse_failures": 0,
                     "syntax": "relative JS/TS import/export/require and Python import statements",
                     "semantic_import_resolution_verified": False}
        raw = self._git(["ls-tree", "-rlz", "--full-tree", base_sha], MAX_TREE_BYTES)
        self.inventory_sha256 = hashlib.sha256(raw).hexdigest()
        entries = raw.split(b"\0")
        if len(entries) > MAX_TREE_FILES + 1:
            raise ValueError("frozen source inventory exceeds the analysis bound")
        for entry in entries:
            if not entry:
                continue
            try:
                metadata, name = entry.split(b"\t", 1)
                mode, kind, oid, size = metadata.split()
                path = name.decode("utf-8")
                if (mode not in {b"100644", b"100755"} or kind != b"blob"
                        or not size.isdigit() or not _path(path)):
                    continue
                self.files[path] = {"oid": oid.decode("ascii"), "size": int(size)}
            except (ValueError, UnicodeError):
                continue
        self.paths = sorted(self.files)

    def contains(self, path):
        if path in self.files:
            return True
        index = bisect.bisect_left(self.paths, path + "/")
        return index < len(self.paths) and self.paths[index].startswith(path + "/")

    def _git(self, args, maximum):
        # Git object reads cannot execute target source or trigger a lazy fetch.
        import os
        environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}
        output, read_failed = bytearray(), []
        with subprocess.Popen(["git", "--no-replace-objects", *args], cwd=self.root,
                              env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            def drain():
                try:
                    while len(output) <= maximum:
                        chunk = process.stdout.read(min(65536, maximum - len(output) + 1))
                        if not chunk:
                            break
                        output.extend(chunk)
                    if len(output) > maximum:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                except (OSError, ValueError):
                    read_failed.append(True)

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise ValueError("frozen source read exceeded its deadline") from None
            finally:
                reader.join(timeout=5)
            if process.returncode or read_failed or reader.is_alive() or len(output) > maximum:
                raise ValueError("frozen source is unavailable or exceeds the analysis bound")
        return bytes(output)

    def read(self, path):
        source = self.files[path]
        if (source["size"] > MAX_SOURCE_BYTES or self.scan["files_scanned"] >= MAX_SCAN_FILES
                or self.scan["source_bytes_scanned"] + source["size"] > MAX_SCAN_BYTES):
            self.scan["truncated"] = True
            return None
        self.scan["files_scanned"] += 1
        self.scan["source_bytes_scanned"] += source["size"]
        try:
            raw = self._git(["cat-file", "blob", source["oid"]], MAX_SOURCE_BYTES)
            text = raw.decode("utf-8")
            if "\x00" in text:
                raise ValueError("binary source")
            return text
        except (ValueError, OSError, UnicodeError, subprocess.SubprocessError):
            self.scan["unavailable_files"] += 1
            return None


def _secret_file(path):
    name = PurePosixPath(path).name.lower()
    return (name == ".env" or name.startswith(".env.") or name.endswith((".pem", ".key"))
            or "service-account" in name or "credentials" in name)


def _relative_path(folder, ref):
    parts = list(PurePosixPath(folder).parts)
    for part in ref.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    result = "/".join(parts)
    return result if _path(result) else None


def _source_imports(path, text):
    folder = str(PurePosixPath(path).parent)
    folder = "" if folder == "." else folder
    refs = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return [], False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            prefix = "../" * (node.level - 1)
            modules = [node.module.replace(".", "/")] if node.module else [n.name for n in node.names]
            for module in modules:
                resolved = _relative_path(folder, prefix + module)
                if resolved is not None:
                    refs.append((resolved, "python"))
    else:
        # Lexical review only: aliases, dynamic imports and generated code remain
        # outside this limited detector. No source text appears in its output.
        code = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", text)
        for match in JS_IMPORT.finditer(code):
            resolved = _relative_path(folder, match.group(1))
            if resolved is not None:
                refs.append((resolved, "javascript"))
    return list(dict.fromkeys(refs)), True


def _import_candidates(path, language):
    if language == "python":
        return [path + ".py", path + "/__init__.py"]
    paths = [path]
    suffix = PurePosixPath(path).suffix
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        stem = path[:-len(suffix)]
        paths.extend(stem + extension for extension in (".ts", ".tsx"))
    if not suffix:
        paths.extend(path + extension for extension in JS_SUFFIXES)
        paths.extend(path + "/index" + extension for extension in JS_SUFFIXES)
    return paths


def _cycles(nodes, outgoing):
    """Return bounded cycle witnesses using iterative DFS, including self loops."""
    colors, witnesses = {}, []
    for start in sorted(nodes):
        if colors.get(start, 0):
            continue
        path, positions = [start], {start: 0}
        colors[start] = 1
        stack = [(start, iter(sorted(outgoing[start])))]
        while stack:
            current, neighbors = stack[-1]
            child = next(neighbors, None)
            if child is None:
                colors[current] = 2
                stack.pop()
                positions.pop(current)
                path.pop()
            elif colors.get(child, 0) == 0:
                colors[child] = 1
                positions[child] = len(path)
                path.append(child)
                stack.append((child, iter(sorted(outgoing[child]))))
            elif colors.get(child) == 1:
                witnesses.append([*path[positions[child]:], child])
                if len(witnesses) >= MAX_CYCLES:
                    return witnesses
    return witnesses


def _layers(nodes, incoming, outgoing):
    degree = {node: len(incoming[node] & nodes) for node in nodes}
    ready = sorted(node for node in nodes if not degree[node])
    waves, order = [], []
    while ready:
        waves.append(ready)
        order.extend(ready)
        following = []
        for node in ready:
            for child in outgoing[node] & nodes:
                degree[child] -= 1
                if not degree[child]:
                    heapq.heappush(following, child)
        ready = [heapq.heappop(following) for _ in range(len(following))]
    if len(order) != len(nodes):
        return waves, [], None
    distances, previous = {}, {}
    for node in order:
        parents = incoming[node] & nodes
        parent = min(parents, key=lambda p: (-distances[p], p)) if parents else None
        distances[node] = distances[parent] + 1 if parent else 1
        previous[node] = parent
    last = min(nodes, key=lambda n: (-distances[n], n)) if nodes else None
    chain = []
    while last is not None:
        chain.append(last)
        last = previous[last]
    return waves, list(reversed(chain)), max(distances.values(), default=0)


def analyze_dependencies(tasks, *, root=None, base_sha=None, selected_ids=None,
                         workers=100, allow_overlap=False, completed_ids=()):
    """Inspect a selected workload and its prerequisite closure without mutation.

    A frontier is a conservative maximal set under the relationships observed,
    not an exact graph-width optimum or proof of semantic independence. Completed
    metadata and caller-supplied completion IDs still need base/PR verification
    at live admission. Future waves never authorize dependent work on stale code.
    """
    if not isinstance(tasks, (list, tuple)) or len(tasks) > MAX_TASKS:
        raise ValueError("dependency analysis accepts at most10000task objects")
    if type(workers) is not int or not 1 <= workers <= MAX_TASKS:
        raise ValueError("requested workers must be between1and10000")
    if type(allow_overlap) is not bool:
        raise ValueError("allow_overlap must be boolean")
    if (root is None) != (base_sha is None):
        raise ValueError("static import review requires both root and an exact Git base")
    if base_sha is not None and (not isinstance(base_sha, str)
                                or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", base_sha) is None):
        raise ValueError("static import review requires an exact Git commit")
    inventory, duplicates = {}, set()
    resource_entries = 0
    for task in tasks:
        if not isinstance(task, dict) or not _id(task.get("id")):
            raise ValueError("dependency analysis requires safe task IDs")
        if task["id"] in inventory:
            duplicates.add(task["id"])
        else:
            inventory[task["id"]] = task
        resource_entries += sum(len(task[name]) for name in ("ownership", "dependsOn", "produces", "consumes", "reads")
                                if isinstance(task.get(name), list))
        if resource_entries > MAX_RESOURCE_ENTRIES:
            raise ValueError("task resource metadata exceeds100000entries")
    selected = list(inventory) if selected_ids is None else selected_ids
    if (not isinstance(selected, (list, tuple)) or len(selected) > MAX_TASKS
            or any(not _id(task_id) for task_id in selected)):
        raise ValueError("select a bounded list of safe task IDs")
    selected = list(selected)
    if (not isinstance(completed_ids, (list, tuple, set, frozenset))
            or len(completed_ids) > MAX_TASKS or any(not _id(item) for item in completed_ids)):
        raise ValueError("completed task IDs must be bounded safe identities")
    completed = set(completed_ids)
    completed.update(task_id for task_id, task in inventory.items()
                     if task.get("status") == "merged" and type(task.get("pullRequest")) is int
                     and task["pullRequest"] > 0)
    errors, hints, edge_records = [], [], []
    error_keys, hint_keys, edge_keys = set(), set(), set()
    hints_truncated = False
    errors_truncated = False
    invalid_tasks = set()

    def error(code, task_ids, **details):
        nonlocal errors_truncated
        identities = sorted(set(task_ids))
        invalid_tasks.update(identities)
        if len(errors) >= MAX_HINTS:
            errors_truncated = True
            return
        record = {"code": code, "task_ids": identities[:MAX_DIAGNOSTIC_TASKS], **details}
        if len(identities) > MAX_DIAGNOSTIC_TASKS:
            record.update(task_ids_truncated=True, total_task_count=len(identities))
        key = repr(record)
        if key not in error_keys:
            errors.append(record)
            error_keys.add(key)

    def hint(code, task_ids, **details):
        nonlocal hints_truncated
        if len(hints) >= MAX_HINTS:
            hints_truncated = True
            return
        identities = sorted(set(task_ids))
        record = {"code": code, "task_ids": identities[:MAX_DIAGNOSTIC_TASKS], **details}
        if len(identities) > MAX_DIAGNOSTIC_TASKS:
            record.update(task_ids_truncated=True, total_task_count=len(identities))
        key = repr(record)
        if key in hint_keys:
            return
        if len(hints) >= MAX_HINTS:
            hints_truncated = True
            return
        hints.append(record)
        hint_keys.add(key)

    artifact_providers, declared_file_owners = defaultdict(set), defaultdict(set)
    for task_id, task in inventory.items():
        values = task.get("produces", [])
        if isinstance(values, list) and len(values) <= MAX_RESOURCES:
            for value in values:
                if _artifact(value):
                    artifact_providers[value].add(task_id)
        owned = task.get("ownership", [])
        if isinstance(owned, list) and len(owned) <= MAX_RESOURCES:
            for path in owned:
                if _path(path):
                    declared_file_owners[path].add(task_id)
    relevant, incoming, outgoing = set(), defaultdict(set), defaultdict(set)
    if len(set(selected)) != len(selected):
        error("duplicate_selection", selected)
    selected = sorted(set(selected))
    pending = deque(selected)
    ownership, reads = {}, {}

    source = None
    source_unavailable = False
    if root is not None:
        try:
            source = _FrozenSource(root, base_sha)
        except (ValueError, OSError, subprocess.SubprocessError):
            source_unavailable = True
            hint("static_source_unavailable", selected)

    def edge(producer, consumer, kind, **details):
        key = producer, consumer, kind, tuple(sorted(details.items()))
        if key in edge_keys:
            return
        if len(edge_keys) >= MAX_EDGES:
            raise ValueError("dependency graph exceeds200000edges")
        edge_keys.add(key)
        edge_records.append({"producer": producer, "consumer": consumer, "kind": kind, **details})
        incoming[consumer].add(producer)
        outgoing[producer].add(consumer)
        pending.append(producer)

    while pending:
        task_id = pending.popleft()
        if task_id in relevant:
            continue
        relevant.add(task_id)
        if task_id not in inventory:
            error("unknown_task", [task_id])
            continue
        task = inventory[task_id]
        if task_id in duplicates:
            error("duplicate_task", [task_id])
        try:
            validate_task_resources(task)
        except ValueError as failure:
            error(failure.code if isinstance(failure, TaskResourceError) else "invalid_resource_metadata", [task_id])
            ownership[task_id], reads[task_id] = [], []
            continue
        if task_id in completed:
            # Historical dependency records may contain only id/status/PR.
            # They schedule no new work; ordinary live admission still verifies
            # the actual PR merge and ancestry in the selected source base.
            ownership[task_id], reads[task_id] = [], []
            continue
        owned = task.get("ownership")
        if (not isinstance(owned, list) or not 1 <= len(owned) <= MAX_RESOURCES
                or any(not _path(path) for path in owned) or len(set(owned)) != len(owned)):
            error("invalid_ownership", [task_id])
            owned = []
        ownership[task_id], reads[task_id] = owned, list(task.get("reads", []))
        dependencies = task.get("dependsOn", [])
        if (not isinstance(dependencies, list) or len(dependencies) > MAX_RESOURCES
                or any(not _id(item) for item in dependencies)):
            error("invalid_dependency_metadata", [task_id])
            dependencies = []
        if len(set(dependencies)) != len(dependencies):
            error("duplicate_dependency", [task_id])
        for dependency in sorted(set(dependencies)):
            if dependency not in inventory:
                error("unknown_dependency", [task_id], dependency_id=dependency)
            else:
                edge(dependency, task_id, "declared")
                if dependency == task_id:
                    error("self_dependency", [task_id])
        for artifact in task.get("consumes", []):
            providers = sorted(artifact_providers[artifact])
            if not providers:
                error("unknown_artifact_provider", [task_id], artifact=artifact)
            elif len(providers) != 1:
                error("ambiguous_artifact_provider", [task_id, *providers], artifact=artifact)
            else:
                edge(providers[0], task_id, "artifact", artifact=artifact)
                if providers[0] == task_id:
                    error("self_artifact_dependency", [task_id], artifact=artifact)
        if source is not None:
            for path in task.get("reads", []):
                providers = artifact_providers.get(path, set())
                if (not source.contains(path) and len(providers) == 1
                        and task_id not in providers):
                    edge(next(iter(providers)), task_id, "missing_input", artifact=path)
    write_index = _Paths()
    for task_id in sorted(relevant):
        for path in ownership.get(task_id, []):
            write_index.add(path, task_id)

    unresolved_readers = set()
    if source is not None:
        new_owners, missing_owned_paths = set(), 0
        for task_id in sorted(relevant):
            if task_id not in completed:
                for path in ownership.get(task_id, []):
                    if not source.contains(path):
                        missing_owned_paths += 1
                        new_owners.add(task_id)
        source.scan["new_owned_paths_not_in_base"] = missing_owned_paths
        if missing_owned_paths:
            hint("new_owned_paths_not_in_base", new_owners,
                 severity="coverage_note", path_count=missing_owned_paths)
        for task_id in sorted(relevant):
            if task_id in completed:
                continue
            for path in reads.get(task_id, []):
                if not source.contains(path) and not any(_overlap(path, p) for p in ownership.get(task_id, [])):
                    providers = artifact_providers.get(path, set()) or declared_file_owners.get(path, set())
                    if len(providers) == 1:
                        provider = next(iter(providers))
                        hint("missing_input_with_provider", [task_id, provider], paths=[path])
                    else:
                        hint("missing_input_at_base", [task_id], paths=[path])
                    unresolved_readers.add(task_id)
        for path in sorted(source.files):
            if not path.endswith(SOURCE_SUFFIXES) or _secret_file(path):
                continue
            owners = write_index.overlapping(path)
            owners = {task_id for task_id in owners if task_id not in completed}
            if not owners:
                continue
            text = source.read(path)
            if text is None:
                continue
            imports, parsed = _source_imports(path, text)
            if not parsed:
                source.scan["unavailable_files"] += 1
                source.scan["parse_failures"] += 1
                hint("source_parse_unavailable", owners, paths=[path])
                continue
            for imported, language in imports:
                candidates = _import_candidates(imported, language)
                resolved = next((p for p in candidates if p in source.files), None)
                if resolved is not None:
                    for task_id in owners:
                        if resolved not in reads[task_id]:
                            reads[task_id].append(resolved)
                    providers = write_index.overlapping(resolved) - completed
                    for task_id in sorted(owners):
                        other = providers - {task_id}
                        if other:
                            hint("static_import_dependency_review", [task_id, *other], paths=[path, resolved])
                else:
                    providers = set().union(*(artifact_providers.get(p, set()) for p in candidates))
                    providers.update(set().union(*(declared_file_owners.get(p, set()) for p in candidates)))
                    for task_id in sorted(owners):
                        other = providers - {task_id}
                        if other:
                            hint("missing_import_with_provider", [task_id, *other], paths=[path, imported])
                            unresolved_readers.add(task_id)

    # Report only bounded examples; frontier construction below independently
    # checks path intersections and does not rely on a truncated hint list.
    previous_writes = _Paths()
    for task_id in sorted(relevant):
        if len(hints) >= MAX_HINTS:
            hints_truncated = True
            break
        if task_id in completed:
            continue
        for path in ownership.get(task_id, []):
            others = previous_writes.overlapping(path, limit=MAX_HINTS + 1)
            if others:
                hint("shared_ownership", [task_id, *sorted(others)[:MAX_HINTS]], paths=[path])
            previous_writes.add(path, task_id)
        for path in reads.get(task_id, []):
            providers = write_index.overlapping(path, limit=MAX_HINTS + 1) - {task_id} - completed
            if providers:
                hint("read_write_dependency_review", [task_id, *sorted(providers)[:MAX_HINTS]], paths=[path])

    active = relevant - completed
    # Unselected unknown identifiers are represented only as diagnostics, not
    # scheduling nodes which could accidentally satisfy a prerequisite.
    active &= inventory.keys()
    waves, critical, critical_length = _layers(active, incoming, outgoing)
    cycles = _cycles(active, {node: outgoing[node] & active for node in active})
    if cycles:
        error("dependency_cycle", sorted({node for cycle in cycles for node in cycle}))
    blocked = {}
    for task_id in selected:
        task = inventory.get(task_id)
        reasons = []
        if task is None or task_id in invalid_tasks:
            reasons.append({"code": "invalid_task_or_dependency_metadata"})
        if task_id in completed:
            reasons.append({"code": "task_already_completed"})
        if task is not None:
            if task.get("status", "ready") != "ready" and task_id not in completed:
                reasons.append({"code": "task_not_ready"})
            if task.get("blockedBy"):
                reasons.append({"code": "declared_blockers"})
            prerequisites = sorted(incoming[task_id] - completed)
            if prerequisites:
                reasons.append({"code": "dependency_not_merged", "dependency_ids": prerequisites})
        if reasons:
            blocked[task_id] = reasons
    ready = [task_id for task_id in selected if task_id not in blocked]
    candidates = [task_id for task_id in ready if task_id not in unresolved_readers]
    candidate_writes, candidate_reads = _Paths(), _Paths()
    for task_id in candidates:
        for path in ownership.get(task_id, []):
            candidate_writes.add(path, task_id)
        for path in reads.get(task_id, []):
            candidate_reads.add(path, task_id)

    def conflict_score(task_id):
        neighbors = set()
        for path in ownership.get(task_id, []):
            neighbors.update(candidate_writes.overlapping(path, limit=65))
            neighbors.update(candidate_reads.overlapping(path, limit=65))
            if len(neighbors - {task_id}) >= 64:
                break
        if len(neighbors - {task_id}) < 64:
            for path in reads.get(task_id, []):
                neighbors.update(candidate_writes.overlapping(path, limit=65))
                if len(neighbors - {task_id}) >= 64:
                    break
        specificity = min((path.count("/") for path in ownership.get(task_id, [])), default=0)
        return min(64, len(neighbors - {task_id})), -specificity, task_id

    safe, dispatch_writes, dispatch_reads = [], _Paths(), _Paths()
    for task_id in sorted(candidates, key=conflict_score):
        writes, inputs = ownership.get(task_id, []), reads.get(task_id, [])
        if (any(dispatch_writes.overlapping(path, limit=1) or dispatch_reads.overlapping(path, limit=1)
                for path in writes)
                or any(dispatch_writes.overlapping(path, limit=1) for path in inputs)):
            continue
        safe.append(task_id)
        for path in writes:
            dispatch_writes.add(path, task_id)
        for path in inputs:
            dispatch_reads.add(path, task_id)
    source_incomplete = source_unavailable or source is not None and (
        source.scan["truncated"] or source.scan["unavailable_files"] > 0)
    if source is not None:
        source.scan["incomplete"] = bool(source_incomplete)
    eligible = not errors and not cycles and not source_incomplete
    advice = []
    width = max((len(wave) for wave in waves), default=0)
    if errors:
        advice.append("repair_dependency_metadata_before_dispatch")
    if cycles:
        advice.append("resolve_cycles_before_dispatch")
    if len(safe) < workers:
        advice.append("merge_prerequisites_then_replan_from_new_base" if width >= workers
                      else "prepare_more_independent_work_or_reduce_this_stage")
    if any(item["code"] != "new_owned_paths_not_in_base" for item in hints):
        advice.append("review_inferred_relationships_and_freeze_explicit_contracts")
    if source_incomplete:
        advice.append("complete_static_review_before_claiming_frontier_capacity")
    return {
        "schema_version": 1, "scope": "local_metadata_and_limited_static_import_review",
        "inventory_task_count": len(tasks), "task_count": len(relevant),
        "selected_task_ids": selected, "selected_task_count": len(selected),
        "requested_workers": workers, "overlap_allowed": allow_overlap,
        "edges": sorted(edge_records, key=lambda item: (item["producer"], item["consumer"], item["kind"])),
        "errors": sorted(errors, key=repr), "errors_truncated": errors_truncated,
        "review_hints": sorted(hints, key=repr),
        "review_hints_truncated": hints_truncated,
        "cycles": cycles, "cycle_witnesses_bounded": len(cycles) >= MAX_CYCLES,
        "waves": waves, "wave_sizes": [len(wave) for wave in waves],
        "waves_are_theoretical": True, "critical_path": critical,
        "critical_path_length": critical_length, "max_wave_width": width,
        "max_wave_width_is_exact_graph_width": False,
        "ready_task_ids": ready, "ready_count": len(ready),
        "safe_frontier_ids": safe, "safe_frontier_count": len(safe),
        "safe_frontier_is_maximal_not_maximum": True,
        "safe_frontier_algorithm": "bounded_conflict_degree_then_path_specificity_then_task_id",
        "suggested_dispatch_ids": safe[:workers] if eligible else [],
        "requested_parallelism_possible_now": eligible and len(safe) >= workers,
        "supports_100_now": eligible and len(safe) >= 100,
        "blocked_tasks": blocked,
        "selected_blockers": [{"task_id": task_id, **reason}
                              for task_id, reasons in blocked.items() for reason in reasons],
        "review_required_task_ids": sorted(unresolved_readers & set(selected)),
        "shortfall": {"requested": workers, "currently_ready": len(ready),
                      "conflict_free_ready": len(safe), "missing": max(0, workers - len(safe))},
        "staging_advice": advice,
        "static_scan": source.scan if source else {"enabled": root is not None,
                         "unavailable": source_unavailable, "truncated": False,
                         "incomplete": source_unavailable,
                         "files_scanned": 0, "source_bytes_scanned": 0},
        "base_sha": base_sha, "source_inventory_sha256": source.inventory_sha256 if source else None,
        "completion_scope": "local_metadata_or_caller_claims;verify_actual_merged_base_before_launch",
        "semantic_independence_verified": False, "provider_capacity_verified": False,
        "authorizes_spend": False,
    }
