"""Read-only project workload analysis and conservative launch prerequisites."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from .dashboard_catalog import SECRET
from .project import load_project

TASK_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,39}")
MAX_TASKS = 10000
MAX_TASK_BYTES = 256 * 1024
MAX_INVENTORY_BYTES = 32 * 1024 * 1024


def _public_task_id(value):
    return isinstance(value, str) and TASK_ID.fullmatch(value) and not SECRET.search(value)


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate task metadata key")
            value[key] = item
        return value

    def nonfinite(_):
        raise ValueError("Nonfinite task metadata")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def load_inventory(project):
    """Bound and hash every definition; no GitHub, ledger or target commands."""
    entries = sorted(project.tasks_dir.glob("*.json"))
    if len(entries) > MAX_TASKS:
        raise ValueError("Workload analysis supports at most 10000 task definitions")
    tasks, hashes, total = [], {}, 0
    directory = project.tasks_dir.resolve()
    for entry in entries:
        if not TASK_ID.fullmatch(entry.stem):
            continue
        if entry.is_symlink() or not entry.resolve().is_relative_to(directory):
            raise ValueError("Task metadata must be a regular file inside its configured directory")
        if not entry.is_file() or entry.stat().st_size > MAX_TASK_BYTES:
            raise ValueError("Task metadata exceeds the per-file limit")
        raw = entry.read_bytes()
        total += len(raw)
        if len(raw) > MAX_TASK_BYTES or total > MAX_INVENTORY_BYTES:
            raise ValueError("Task inventory exceeds the bounded analysis size")
        value = _json(raw)
        if not isinstance(value, dict) or value.get("id") != entry.stem:
            raise ValueError("Task metadata ID must match its filename")
        tasks.append(value)
        hashes[entry.stem] = hashlib.sha256(raw).hexdigest()
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    return tasks, {"inventory_sha256": hashlib.sha256(encoded).hexdigest(),
                   "inventory_task_count": len(tasks), "inventory_bytes": total}


def analyze_project(project, selected_ids=None, *, workers=100, base="HEAD", allow_overlap=False):
    from .dependencies import analyze_dependencies

    if not isinstance(base, str) or not base or base.startswith("-") or len(base) > 200:
        raise ValueError("Analysis needs a bounded Git base reference")
    try:
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{base}^{{commit}}"], cwd=project.root,
            text=True, stderr=subprocess.DEVNULL, timeout=10,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        raise ValueError("The selected Git base is unavailable for dependency analysis") from None
    tasks, identity = load_inventory(project)
    report = analyze_dependencies(tasks, root=project.root, base_sha=base_sha,
        selected_ids=selected_ids, workers=workers, allow_overlap=allow_overlap)
    report.update(**identity, base_sha=base_sha, repository=project.repository,
                  source="local_metadata_and_frozen_source", read_only=True,
                  analysis_mode="backlog" if selected_ids is None else "cohort",
                  provider_capacity_verified=False, launch_ready=False)
    return report


def preparation_blockers(report, require_parallel=None):
    blockers = []
    if report.get("errors") or report.get("cycles"):
        blockers.append("task_dependency_graph_invalid")
    if report.get("selected_blockers"):
        blockers.append("task_dependencies_not_ready")
    if require_parallel is not None:
        if type(require_parallel) is not int or not 1 <= require_parallel <= 100:
            raise ValueError("Required parallelism must be between 1 and 100")
        if report.get("safe_frontier_count", 0) < require_parallel:
            blockers.append("independent_frontier_below_required_parallelism")
        if isinstance(report.get("static_scan"), dict) and report["static_scan"].get("incomplete") is True:
            blockers.append("dependency_source_scan_incomplete")
    return blockers


def require_cohort(report, require_parallel=None):
    blockers = preparation_blockers(report, require_parallel)
    if blockers:
        raise ValueError("Dependency preparation is blocked: " + ", ".join(blockers)
                         + ". Inspect swarm-control analyze before launching.")


def effective_tasks(tasks, report):
    """Use inferred artifact prerequisites in the ordinary real-PR/base gate.

    Original task files stay immutable. Derived edges cannot let a locally
    claimed merge bypass the runner's GitHub and ancestor verification.
    """
    prerequisites = {}
    for edge in report.get("edges", []):
        prerequisites.setdefault(edge["consumer"], set()).add(edge["producer"])
    return [{**task, "dependsOn": sorted(set(task.get("dependsOn", []))
                | prerequisites.get(task["id"], set()))} for task in tasks]


def public_analysis(source):
    """Project only bounded numbers, codes and task IDs to the browser."""
    if not isinstance(source, dict):
        return None
    result = {"semantic_independence_verified": False, "authorizes_spend": False,
              "provider_capacity_verified": False, "waves_are_theoretical": True,
              "safe_frontier_is_maximal_not_maximum": True}
    for key in ("task_count", "selected_task_count", "requested_workers", "ready_count",
                "safe_frontier_count", "max_wave_width", "critical_path_length", "inventory_task_count"):
        value = source.get(key)
        result[key] = value if type(value) is int and 0 <= value <= MAX_TASKS else None
    for key in ("requested_parallelism_possible_now", "supports_100_now"):
        result[key] = source.get(key) if type(source.get(key)) is bool else None
    for key in ("ready_task_ids", "safe_frontier_ids", "suggested_dispatch_ids", "critical_path"):
        values = source.get(key, [])
        result[key] = [v for v in values[:1000] if _public_task_id(v)] \
            if isinstance(values, list) else []
    sizes = source.get("wave_sizes", [])
    result["wave_sizes"] = [v for v in sizes[:1000] if type(v) is int and 0 <= v <= MAX_TASKS] \
        if isinstance(sizes, list) else []
    for key in ("errors", "review_hints"):
        rows = source.get(key, [])
        result[key] = []
        if not isinstance(rows, list):
            continue
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            code = row.get("code")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,100}", code):
                continue
            ids = row.get("task_ids", [])
            result[key].append({"code": code, "task_ids": [v for v in ids[:100]
                if _public_task_id(v)] if isinstance(ids, list) else []})
    for count_key, rows_key in (("error_count", "errors"), ("review_hint_count", "review_hints")):
        count = source.get(count_key)
        result[count_key] = count if type(count) is int and 0 <= count <= 1000000 else \
            len(source.get(rows_key, [])) if isinstance(source.get(rows_key), list) else 0
    scan = source.get("static_scan")
    result["source_scan_incomplete"] = scan.get("incomplete") if isinstance(scan, dict) else \
        source.get("source_scan_incomplete") if type(source.get("source_scan_incomplete")) is bool else None
    result["edges"] = []
    for edge in source.get("edges", [])[:200] if isinstance(source.get("edges"), list) else []:
        if (isinstance(edge, dict) and all(_public_task_id(edge.get(k))
                                          for k in ("producer", "consumer"))):
            result["edges"].append({"producer": edge["producer"], "consumer": edge["consumer"]})
    return result


def public_context(source):
    """Keep raw instructions, paths and per-command content out of HTTP."""
    if not isinstance(source, dict):
        return None
    result = {"token_count": None, "semantic_acceptance_verified": False}
    for key in ("instruction_count", "instruction_bytes", "total_prompt_bytes", "max_prompt_bytes",
                "task_count", "tasks_with_context", "tasks_with_acceptance_checks"):
        value = source.get(key)
        if type(value) is int and 0 <= value <= 1024 * 1024 * 1024:
            result[key] = value
    warnings = source.get("warnings", [])
    result["warnings"] = [v for v in warnings[:100] if isinstance(v, str)
        and re.fullmatch(r"[a-z_]{1,100}", v)] if isinstance(warnings, list) else []
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", nargs="*", help="Selected task IDs; omit to inspect the whole backlog")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, help="Explicit frozen JSON manifest containing taskIds")
    parser.add_argument("--workers", "--agents", type=int, default=100)
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--allow-overlap", action="store_true")
    parser.add_argument("--require-parallel", type=int,
                        help="Fail if fewer than this many tasks form a conservative independent ready frontier")
    args = parser.parse_args()
    selected = args.tasks or None
    if args.manifest:
        if selected:
            parser.error("Choose positional task IDs or --manifest")
        with args.manifest.open("rb") as stream:
            raw = stream.read(MAX_INVENTORY_BYTES + 1)
        if len(raw) > MAX_INVENTORY_BYTES:
            parser.error("Manifest exceeds the analysis size limit")
        value = _json(raw)
        selected = value.get("taskIds") if isinstance(value, dict) else None
        if not isinstance(selected, list) or not selected:
            parser.error("Manifest needs a nonempty taskIds array")
    report = analyze_project(load_project(args.project), selected, workers=args.workers,
                             base=args.base, allow_overlap=args.allow_overlap)
    report["required_parallelism"] = args.require_parallel
    report["validation_blockers"] = preparation_blockers(report, args.require_parallel)
    if report["analysis_mode"] == "backlog":
        report["validation_blockers"] = [value for value in report["validation_blockers"]
                                         if value != "task_dependencies_not_ready"]
    print(json.dumps(report, indent=2, allow_nan=False))
    return 1 if report["validation_blockers"] else 0
