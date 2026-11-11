"""Pure, conservative CI observations; recommendations never execute actions.

Inputs are complete GitHub check-run inventories and head-filtered workflow runs.
Workflow ``jobs`` may be an explicit list or a complete ``{total_count, jobs}``
inventory. Missing jobs are unknown, rather than evidence that no job started.
Freshness uses each record's updated/completed/started/created timestamp. A caller
must collect fresh source observations; this module neither fetches nor retries.
"""

import math
import re
from datetime import UTC, datetime
from typing import Any

RECOMMENDED_ACTIONS = frozenset({
    "none", "wait", "inspect_metadata", "propose_manual_rerun", "review_or_repair",
})
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PENDING = {"queued", "requested", "waiting", "pending", "in_progress"}
_TERMINAL = {
    "success", "failure", "cancelled", "timed_out", "action_required", "neutral",
    "skipped", "stale", "startup_failure",
}


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(UTC)


def _inventory(value: Any, key: str) -> list[dict]:
    if not isinstance(value, dict):
        raise TypeError("complete inventory required")
    rows, total = value.get(key), value.get("total_count")
    if not isinstance(rows, list) or type(total) is not int or total != len(rows):
        raise ValueError("inventory incomplete")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid record")
    return rows


def _unique_ids(rows: list[dict]) -> None:
    ids = [row.get("id") for row in rows]
    if any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("unique positive record IDs required")


def _record_time(row: dict) -> datetime:
    for field in ("updated_at", "completed_at", "started_at", "created_at"):
        if row.get(field) is not None:
            return _utc(row[field])
    raise ValueError("record timestamp missing")


def _validate_times(row: dict, now: datetime) -> None:
    for field in ("updated_at", "completed_at", "started_at", "created_at"):
        if row.get(field) is not None and _utc(row[field]) > now:
            raise ValueError("future nested timestamp")


def _jobs(run: dict) -> list[dict] | None:
    value = run.get("jobs")
    if value is None:
        return None
    if isinstance(value, dict):
        rows = _inventory(value, "jobs")
    elif isinstance(value, list) and all(isinstance(row, dict) for row in value):
        rows = value
    else:
        raise ValueError("invalid jobs inventory")
    return rows


def _steps_succeeded(jobs: list[dict] | None) -> bool:
    return bool(jobs) and all(
        isinstance(job.get("steps"), list) and bool(job["steps"])
        and all(isinstance(step, dict) and step.get("status") == "completed"
                and step.get("conclusion") == "success" for step in job["steps"])
        for job in jobs
    )


def classify_ci(
    head_sha: str,
    required_checks: list[str] | tuple[str, ...],
    check_payload: dict,
    workflow_runs: list[dict] | dict,
    *,
    observed_at: datetime | str,
    stale_after_seconds: float = 600,
) -> dict:
    """Classify source metadata without granting integration or spend authority.

    ``allowed_to_merge`` covers only strict CI evidence, never semantic review,
    branch protection, a current base, ownership or owner authorization. Even a
    manual rerun is merely proposed. Inputs are not mutated or included verbatim
    in the output. ``observed_at`` and all source times must be timezone-aware.
    """
    result = {
        "state": "metadata_invalid", "allowed_to_merge": False,
        "recommended_action": "inspect_metadata", "reason": "Invalid CI metadata.",
        "head_sha": head_sha if isinstance(head_sha, str) and _SHA.fullmatch(head_sha) else None,
        "observed_at": None, "required_check_count": 0, "check_count": None,
        "workflow_count": None, "authorizes_mutation": False,
        "authorizes_spend": False,
    }

    def answer(state: str, action: str, reason: str) -> dict:
        return {**result, "state": state, "recommended_action": action, "reason": reason,
                "allowed_to_merge": state == "green"}

    try:
        now = _utc(observed_at)
        result["observed_at"] = now.isoformat()
        if result["head_sha"] is None:
            raise ValueError("invalid full head SHA")
        if (not isinstance(required_checks, (list, tuple)) or not required_checks
                or any(not isinstance(name, str) or not name.strip() for name in required_checks)
                or len(set(required_checks)) != len(required_checks)):
            raise ValueError("nonempty unique required checks needed")
        if (isinstance(stale_after_seconds, bool)
                or not isinstance(stale_after_seconds, (int, float))
                or not math.isfinite(stale_after_seconds) or stale_after_seconds <= 0):
            raise ValueError("invalid freshness window")
        result["required_check_count"] = len(required_checks)
        checks = _inventory(check_payload, "check_runs")
        if isinstance(workflow_runs, dict):
            workflows = _inventory(workflow_runs, "workflow_runs")
        elif isinstance(workflow_runs, list) and all(isinstance(row, dict) for row in workflow_runs):
            workflows = workflow_runs
        else:
            raise ValueError("invalid workflow inventory")
        _unique_ids(checks)
        _unique_ids(workflows)
        result.update(check_count=len(checks), workflow_count=len(workflows))
        for row in checks + workflows:
            if row.get("head_sha") != head_sha:
                return answer("head_mismatch", "inspect_metadata", "Source metadata belongs to another head.")
            age = (now - _record_time(row)).total_seconds()
            if age < 0:
                return answer("future_metadata", "inspect_metadata", "Source timestamp is after the observation.")
            if age > stale_after_seconds:
                return answer("stale_metadata", "inspect_metadata", "Source metadata exceeds the freshness window.")
            _validate_times(row, now)
            status, conclusion = row.get("status"), row.get("conclusion")
            if status not in _PENDING | {"completed"}:
                raise ValueError("unknown status")
            if status == "completed" and conclusion not in _TERMINAL:
                raise ValueError("completed result lacks known conclusion")
            if status != "completed" and conclusion is not None:
                raise ValueError("pending result has terminal conclusion")
        if any(not isinstance(row.get("name"), str) or not row["name"].strip() for row in checks):
            raise ValueError("check name missing")
        jobs_by_run = [_jobs(run) for run in workflows]
        for jobs in jobs_by_run:
            if jobs is None:
                continue
            for job in jobs:
                _validate_times(job, now)
                if job.get("head_sha", head_sha) != head_sha:
                    raise ValueError("job head mismatch")
                if job.get("status") not in _PENDING | {"completed"}:
                    raise ValueError("job status missing")
                if job["status"] == "completed" and job.get("conclusion") not in _TERMINAL:
                    raise ValueError("job conclusion missing")
                if job["status"] != "completed" and job.get("conclusion") is not None:
                    raise ValueError("pending job conclusion")
    except (TypeError, ValueError, OverflowError):
        return answer("metadata_invalid", "inspect_metadata", "CI metadata is incomplete, malformed or not time-bound.")

    # Pending aggregate metadata outranks successful steps and incomplete checks.
    for run, jobs in zip(workflows, jobs_by_run):
        if run["status"] != "completed" and _steps_succeeded(jobs):
            return answer("metadata_unresolved", "wait", "Steps succeeded but authoritative workflow metadata is still pending.")
    if any(row["status"] in {"queued", "requested", "waiting", "pending"} for row in workflows + checks):
        return answer("queued", "wait", "CI is queued; waiting is not authorization to rerun or kick it.")
    if any(row["status"] == "in_progress" for row in workflows + checks):
        return answer("in_progress", "wait", "Authoritative CI metadata remains in progress.")
    known_jobs = [job for jobs in jobs_by_run if jobs is not None for job in jobs]
    if any(job["status"] != "completed" for job in known_jobs):
        return answer("metadata_unresolved", "inspect_metadata", "Completed aggregate metadata conflicts with unfinished job metadata.")
    if any(job["conclusion"] != "success" for job in known_jobs):
        return answer("ci_failed", "review_or_repair", "A reported job did not succeed; aggregate success cannot override it.")
    if any("steps" in job and not _steps_succeeded([job]) for job in known_jobs):
        return answer("metadata_unresolved", "inspect_metadata", "Successful aggregate metadata conflicts with its reported steps.")
    for run, jobs in zip(workflows, jobs_by_run):
        if run["conclusion"] in {"failure", "startup_failure"} and jobs == []:
            return answer("ci_not_started", "propose_manual_rerun", "A completed failed workflow explicitly reports zero jobs; manual review may propose a rerun.")
    if any(row["conclusion"] == "cancelled" for row in workflows + checks):
        return answer("cancelled", "inspect_metadata", "CI was cancelled; success is not established.")
    if any(row["conclusion"] != "success" for row in workflows + checks):
        return answer("ci_failed", "review_or_repair", "A completed CI result is not successful; inspect evidence before a separately authorized repair.")

    trusted_names = {
        row.get("name") for row in checks
        if isinstance(row.get("app"), dict) and row["app"].get("slug") == "github-actions"
    }
    if not set(required_checks).issubset(trusted_names):
        return answer("missing_required_checks", "inspect_metadata", "Required GitHub Actions checks are absent; inspect event delivery and inventory.")
    return answer("green", "none", "Complete fresh exact-head checks all succeeded; other integration gates remain separate.")
