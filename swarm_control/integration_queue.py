"""Offline integration scheduling: pending CI does not block independent work."""

import re
from datetime import UTC, datetime

from .modal_worker import valid_path

STAGES = {"ready", "preparing", "awaiting_review", "waiting_ci", "needs_repair", "merged"}
SHA = re.compile(r"[a-f0-9]{40}")
TASK = re.compile(r"[A-Z][A-Z0-9_-]{1,39}")


def _overlap(left, right):
    return any(a == b or a.startswith(b.rstrip("/") + "/")
               or b.startswith(a.rstrip("/") + "/") for a in left for b in right)


def plan_queue(source, *, now=None):
    """Choose preparation lanes, never merge or rerun anything.

    Input claims are operator observations, not authenticated acceptance. A live
    adapter must recheck source, review, base, tree and exact-head CI. Blocked
    dependencies and shared paths remain held; independent ready work can advance.
    """
    now = now or datetime.now(UTC)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("queue planning needs an aware observation time")
    if not isinstance(source, dict) or set(source) - {"base_sha", "lanes", "ci_wait_seconds", "items"}:
        raise ValueError("invalid integration queue input")
    base, lanes, wait = source.get("base_sha"), source.get("lanes", 2), source.get("ci_wait_seconds", 600)
    if not isinstance(base, str) or not SHA.fullmatch(base):
        raise ValueError("queue planning needs the current full base SHA")
    if type(lanes) is not int or not 1 <= lanes <= 10 or type(wait) is not int or not 1 <= wait <= 86400:
        raise ValueError("invalid integration lane or CI wait limit")
    items = source.get("items")
    if not isinstance(items, list) or len(items) > 1000:
        raise ValueError("invalid integration queue items")
    indexed = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid integration item")  # noqa: TRY004 - JSON schema errors share the CLI ValueError boundary.
        task = item.get("task_id")
        if not isinstance(task, str) or not TASK.fullmatch(task) or task in indexed:
            raise ValueError("integration task IDs must be unique")
        stage, ownership, dependencies = item.get("stage"), item.get("ownership"), item.get("depends_on", [])
        if not isinstance(stage, str) or stage not in STAGES:
            raise ValueError("unknown integration stage")
        if (not isinstance(ownership, list) or not ownership or len(ownership) > 100
                or not all(valid_path(p) for p in ownership)):
            raise ValueError("integration ownership must contain bounded repository paths")
        if (not isinstance(dependencies, list) or len(dependencies) > 1000
                or any(not isinstance(d, str) or not TASK.fullmatch(d) for d in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise ValueError("invalid integration dependencies")
        indexed[task] = item
    for task, item in indexed.items():
        if any(dep not in indexed or dep == task for dep in item.get("depends_on", [])):
            raise ValueError("queue dependencies must identify other recorded items")
    unresolved = set(indexed)
    while unresolved:
        ready = {task for task in unresolved
                 if not unresolved.intersection(indexed[task].get("depends_on", []))}
        if not ready:
            raise ValueError("integration dependency cycle")
        unresolved -= ready

    merged = {task for task, item in indexed.items() if item["stage"] == "merged"
              and isinstance(item.get("merge_commit_sha"), str) and SHA.fullmatch(item["merge_commit_sha"])
              and item.get("combined_ci_green") is True and item.get("independent_acceptance") is True}
    active = [item for item in items if item["stage"] in {"preparing", "awaiting_review", "waiting_ci"}]
    available = max(0, lanes - sum(item["stage"] == "preparing" for item in items))
    selected, decisions = [], []
    for item in items:
        task, stage = item["task_id"], item["stage"]
        decision = {"task_id": task, "action": "hold", "reason": stage}
        if stage == "merged":
            decision.update(action="none", reason="reported_qualified_merge" if task in merged else "merge_evidence_incomplete")
        elif stage == "waiting_ci":
            try:
                at = datetime.fromisoformat(item["waiting_since_utc"])
                if at.utcoffset() is None or at > now:
                    raise ValueError("invalid wait observation")
                age = (now - at).total_seconds()
                decision.update(action="quarantine" if age >= wait else "wait",
                                reason="ci_wait_limit_reached" if age >= wait else "ci_pending",
                                wait_seconds=age)
            except (ValueError, TypeError, KeyError, OverflowError):
                decision["reason"] = "ci_wait_observation_unknown"
        elif stage == "needs_repair":
            decision.update(action="assign_repair", reason="reviewed_repair_owner_required")
        elif stage == "ready":
            if any(dep not in merged for dep in item.get("depends_on", [])):
                decision["reason"] = "dependency_not_qualified_merged"
            elif any(_overlap(item["ownership"], other["ownership"]) for other in active + selected):
                decision["reason"] = "ownership_in_flight"
            elif len(selected) >= available:
                decision["reason"] = "preparation_lanes_full"
            else:
                decision.update(action="prepare", reason="independent_candidate_on_current_base",
                                required_base_sha=base)
                selected.append(item)
        decisions.append(decision)
    return {"schema_version": 1, "kind": "integration_queue_plan", "observed_at_utc": now.astimezone(UTC).isoformat(),
            "base_sha": base, "lanes": lanes, "prepare": [item["task_id"] for item in selected],
            "decisions": decisions, "authorizes_mutation": False, "authorizes_spend": False,
            "remote_actions_performed": 0, "revalidate_changed_base": True,
            "automatic_ci_retry": False, "automatic_worker_replacement": False}
