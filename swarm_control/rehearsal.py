"""Compare bounded dispatch and delivery schedules using synthetic phase times."""

import argparse
import hashlib
import heapq
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .project import load_project
from .rollout import MINIMUM_VALIDATED_PERCENT, rollout_waves, wave_passes
from .workload import (
    _json,
    _public_task_id,
    analyze_project,
    read_task_manifest,
    require_cohort,
)

MAX_TASKS = 100
MAX_SCENARIO_BYTES = 256 * 1024


@dataclass(frozen=True)
class TaskTiming:
    setup_seconds: int = 20
    model_seconds: int = 90
    validation_seconds: int = 20
    review_seconds: int = 60
    ci_seconds: int = 120
    worker_outcome: str = "validated"
    delivery_outcome: str = "accepted"


@dataclass(frozen=True)
class Scenario:
    provider_slots: int
    review_slots: int
    ci_slots: int
    review_release: str
    tasks: Mapping[str, TaskTiming]


PRESETS = {
    "healthy": {},
    "worker-fault": {"defaults": {"worker_outcome": "needs_repair"}},
    "provider-constrained": {"provider_slots": 10},
    "review-constrained": {"review_slots": 4, "ci_slots": 8},
}


def _bounded_integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _validate_keys(value, allowed, label):
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError(f"{label} must be an object containing only supported fields")


def _task_timing(value, defaults):
    _validate_keys(value, TaskTiming.__dataclass_fields__.keys(), "Task timing")
    timing = replace(defaults, **value)
    for field, item in asdict(timing).items():
        if field.endswith("_seconds"):
            _bounded_integer(item, int(field == "model_seconds"), 86400, field)
    if timing.worker_outcome not in ("validated", "needs_repair"):
        raise ValueError("worker_outcome must be validated or needs_repair")
    if timing.delivery_outcome not in ("accepted", "review_rejected", "ci_failed"):
        raise ValueError("delivery_outcome must be accepted, review_rejected or ci_failed")
    return timing


def parse_scenario(value, task_ids, *, provider_default=100):
    _validate_keys(value, {"provider_slots", "review_slots", "ci_slots", "review_release",
                           "defaults", "tasks"}, "Scenario")
    overrides = value.get("tasks", {})
    _validate_keys(overrides, set(task_ids), "Task overrides")
    defaults = _task_timing(value.get("defaults", {}), TaskTiming())
    release = value.get("review_release", "after_workers")
    if release not in ("after_workers", "as_validated"):
        raise ValueError("review_release must be after_workers or as_validated")
    return Scenario(
        provider_slots=_bounded_integer(value.get("provider_slots", provider_default), 1, 100,
                                        "provider_slots"),
        review_slots=_bounded_integer(value.get("review_slots", 100), 1, 100, "review_slots"),
        ci_slots=_bounded_integer(value.get("ci_slots", 100), 1, 100, "ci_slots"),
        review_release=release,
        tasks={task_id: _task_timing(overrides[task_id], defaults) if task_id in overrides
               else defaults for task_id in task_ids},
    )


def _workers(task_ids, scenario, start, capacity):
    """Run one admitted group, refilling slots at simulated worker completion."""
    rows = []
    events = []
    provider_lanes = [start] * scenario.provider_slots

    def launch(task_id, at):
        index = len(rows)
        setup_end = at + scenario.tasks[task_id].setup_seconds
        rows.append({"task_id": task_id, "worker_start": at, "model_ready": setup_end})
        # Process completions before setup arrivals at the same timestamp.
        heapq.heappush(events, (setup_end, 1, index))

    for task_id in task_ids[:capacity]:
        launch(task_id, start)
    while events:
        at, phase, index = heapq.heappop(events)
        row = rows[index]
        timing = scenario.tasks[row["task_id"]]
        if phase == 0:
            row.update(worker_end=at, worker_outcome=timing.worker_outcome)
            if len(rows) < len(task_ids):
                launch(task_ids[len(rows)], at)
        else:
            model_start = max(at, provider_lanes[0])
            model_end = model_start + timing.model_seconds
            heapq.heapreplace(provider_lanes, model_end)
            row.update(model_start=model_start, model_end=model_end)
            heapq.heappush(events, (model_end + timing.validation_seconds, 0, index))
    return rows


def _lane(rows, slots, phase, scenario):
    available = [0] * slots
    ready_key = f"{phase}_ready"
    for row in sorted(rows, key=lambda item: item[ready_key]):
        start = max(row[ready_key], available[0])
        end = start + getattr(scenario.tasks[row["task_id"]], f"{phase}_seconds")
        heapq.heapreplace(available, end)
        row.update({f"{phase}_start": start, f"{phase}_end": end})


def _peak(intervals):
    events = [(time, delta) for start, end in intervals if start < end
              for time, delta in ((start, 1), (end, -1))]
    active = peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    return peak


def _queue(rows, phase):
    waits, service = [], []
    for row in rows:
        if f"{phase}_start" in row:
            start = row[f"{phase}_start"]
            waits.append((row[f"{phase}_ready"], start))
            service.append((start, row[f"{phase}_end"]))
    ordered = sorted(end - start for start, end in waits)
    return {
        "observations": len(waits),
        "peak_queued": _peak(waits),
        "peak_in_service": _peak(service),
        "p50_wait_seconds": ordered[(50 * len(ordered) - 1) // 100] if ordered else None,
        "p95_wait_seconds": ordered[(95 * len(ordered) - 1) // 100] if ordered else None,
    }


def simulate(task_ids, scenario, workers, *, ramp_start=None, trace=False):
    plan = [len(task_ids)] if ramp_start is None else rollout_waves(len(task_ids), workers, ramp_start)
    rows, waves = [], []
    workers_end = 0
    for size in plan:
        wave = _workers(task_ids[len(rows):len(rows) + size], scenario, workers_end, min(size, workers))
        rows.extend(wave)
        validated = sum(row["worker_outcome"] == "validated" for row in wave)
        complete_at = max(row["worker_end"] for row in wave)
        waves.append({"started_at": workers_end, "completed_at": complete_at,
                      "attempted": size, "validated": validated})
        workers_end = complete_at
        if ramp_start is not None and not wave_passes(validated, size):
            break

    reviewed = [row for row in rows if row["worker_outcome"] == "validated"]
    for row in reviewed:
        row["review_ready"] = workers_end if scenario.review_release == "after_workers" else row["worker_end"]
        row["delivery_outcome"] = scenario.tasks[row["task_id"]].delivery_outcome
    _lane(reviewed, scenario.review_slots, "review", scenario)
    ci_rows = [row for row in reviewed if row["delivery_outcome"] != "review_rejected"]
    for row in ci_rows:
        row["ci_ready"] = row["review_end"]
    _lane(ci_rows, scenario.ci_slots, "ci", scenario)
    accepted = [row["ci_end"] for row in ci_rows if row["delivery_outcome"] == "accepted"]
    held = len(task_ids) - len(rows)
    result = {
        "policy": "fixed" if ramp_start is None else "progressive",
        "worker_limit": workers,
        "planned_wave_sizes": plan if ramp_start is not None else None,
        "waves": waves if ramp_start is not None else None,
        "attempted": len(rows),
        "validated": len(reviewed),
        "accepted": len(accepted),
        "review_rejected": len(reviewed) - len(ci_rows),
        "ci_failed": len(ci_rows) - len(accepted),
        "not_dispatched": held,
        "hold_reason": "rollout_validation_hold" if held else None,
        "peak_active_workers": _peak((row["worker_start"], row["worker_end"]) for row in rows),
        "workers_finished_seconds": workers_end,
        "first_accepted_seconds": min(accepted, default=None),
        "last_accepted_seconds": max(accepted, default=None),
        "finished_seconds": max([workers_end, *(row["review_end"] for row in reviewed),
                                 *(row["ci_end"] for row in ci_rows)]),
        "queues": {phase: _queue(rows, phase) for phase in ("model", "review", "ci")},
    }
    if trace:
        result["tasks"] = rows
        result["not_dispatched_task_ids"] = task_ids[len(rows):]
    return result


def _read_scenario(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_SCENARIO_BYTES + 1)
    if len(raw) > MAX_SCENARIO_BYTES:
        raise ValueError("Scenario exceeds the size limit")
    try:
        return _json(raw)
    except ValueError:
        raise ValueError("Scenario must contain valid JSON with unique keys and finite values") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic-tasks", type=int, help="Generate a cohort of 2–100 tasks")
    source.add_argument("--project", type=Path, help="Read a prepared target project")
    parser.add_argument("--manifest", type=Path, help="Required with --project; a frozen taskIds array")
    parser.add_argument("--base", help="Git commit or local ref for a project cohort")
    parser.add_argument("--workers", type=int, nargs="+", default=[25, 50, 100],
                        help="Concurrent worker limits to compare, default: 25 50 100")
    parser.add_argument("--ramp-start", type=int, default=5, help="Initial wave size, default: 5")
    scenarios = parser.add_mutually_exclusive_group()
    scenarios.add_argument("--preset", choices=PRESETS)
    scenarios.add_argument("--scenario", type=Path)
    parser.add_argument("--trace", action="store_true", help="Include synthetic per-task phase times")
    args = parser.parse_args()

    limits = sorted(set(args.workers))
    if len(limits) > 10 or not all(2 <= limit <= 100 for limit in limits):
        parser.error("Choose up to 10 distinct worker limits from 2 to 100")
    project, frontier = None, None
    if args.project:
        if not args.manifest:
            parser.error("--project requires --manifest")
        tasks = read_task_manifest(args.manifest)
        if (not 2 <= len(tasks) <= MAX_TASKS or not all(_public_task_id(task) for task in tasks)
                or len(set(tasks)) != len(tasks)):
            parser.error("Rehearsal requires 2–100 distinct valid task IDs")
        project = load_project(args.project)
        frontier = analyze_project(project, tasks, workers=max(limits), base=args.base or project.base_ref)
        require_cohort(frontier, len(tasks))
    else:
        if args.manifest or args.base:
            parser.error("--manifest and --base require --project")
        count = _bounded_integer(args.synthetic_tasks, 2, MAX_TASKS, "--synthetic-tasks")
        tasks = [f"T{index:03}" for index in range(1, count + 1)]
    rollout_waves(len(tasks), min(limits), args.ramp_start)

    preset = args.preset or "healthy"
    scenario_input = _read_scenario(args.scenario) if args.scenario else PRESETS[preset]
    scenario = parse_scenario(scenario_input, tasks,
                              provider_default=project.gateway_concurrency if project else 100)
    scenario_data = asdict(scenario)
    timings = scenario_data.pop("tasks")
    base_sha = frontier["base_sha"] if frontier else None
    inventory_sha = frontier["inventory_sha256"] if frontier else None
    identity = json.dumps({
        "task_ids": tasks,
        "scenario": scenario_data,
        "timings": timings,
        "base_sha": base_sha,
        "inventory_sha256": inventory_sha,
        "worker_limits": limits,
        "ramp_start": args.ramp_start,
        "minimum_validated_percent": MINIMUM_VALIDATED_PERCENT,
    }, sort_keys=True, separators=(",", ":"))
    result = {
        "schema_version": 1,
        "scope": "offline_simulation",
        "authorizes_spend": False,
        "live_capacity_verified": False,
        "input_sha256": hashlib.sha256(identity.encode()).hexdigest(),
        "task_count": len(tasks),
        "base_sha": base_sha,
        "inventory_sha256": inventory_sha,
        "safe_frontier_count": frontier["safe_frontier_count"] if frontier else None,
        "project_worker_limit": project.max_live_agents if project else None,
        "scenario": {"preset": None if args.scenario else preset, **scenario_data},
        "ramp_start": args.ramp_start,
        "ramp_minimum_validated_percent": MINIMUM_VALIDATED_PERCENT,
        "unmodeled_live_controls": ["operations_hold", "gateway_expiry", "provider_quota",
                                    "budget_accounting", "git_and_ci_validation"],
        "trials": [simulate(tasks, scenario, limit, ramp_start=ramp, trace=args.trace)
                   for limit in limits for ramp in (None, args.ramp_start)],
    }
    print(json.dumps(result, indent=2))
    return 0
