"""Pure failure feedback and conservative scaling advice; no execution authority."""

import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

FAILURES = frozenset(
    {
        "recovery_failure",
        "ownership_violation",
        "validation_failure",
        "worker_incomplete",
        "launch_failure",
    }
)


def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _finite(value, lower=0, upper=math.inf):
    try:
        return (
            type(value) in (int, float)
            and math.isfinite(value)
            and lower <= value <= upper
        )
    except (OverflowError, TypeError):
        return False


@dataclass(frozen=True)
class FailurePolicy:
    window_seconds: int = 300
    minimum_failures: int = 3
    minimum_tasks: int = 3
    minimum_share: float = 0.5

    def __post_init__(self):
        if not _integer(self.window_seconds, 1, 86400):
            raise ValueError("window_seconds must be an integer from1 to86400")
        if not _integer(self.minimum_failures, 1, 100000):
            raise ValueError("minimum_failures must be an integer from1 to100000")
        if not _integer(self.minimum_tasks, 1, 100000):
            raise ValueError("minimum_tasks must be an integer from1 to100000")
        if not _finite(self.minimum_share, 0, 1) or self.minimum_share == 0:
            raise ValueError("minimum_share must be finite and greater than0 up to1")


def classify_worker_result(report):
    """Return fixed categories, never exception messages, commands or logs."""
    if not isinstance(report, dict):
        return {"status": "unknown", "cause": "unknown"}
    status = report.get("status")
    if not isinstance(status, str):
        return {"status": "unknown", "cause": "unknown"}
    changes = report.get("changes")
    changes = changes if isinstance(changes, dict) else {}
    if report.get("recovery_errors"):
        cause = "recovery_failure"
    elif (
        report.get("ownership_violations")
        or report.get("violations")
        or report.get("prohibited_files")
        or changes.get("ownership_violations")
        or changes.get("prohibited_files")
    ):
        cause = "ownership_violation"
    elif type(report.get("check_exit")) is int and report["check_exit"] != 0:
        cause = "validation_failure"
    elif status == "validated":
        return {"status": "success", "cause": "validated_success"}
    elif status == "launch_failed":
        cause = "launch_failure"
    elif status in {"needs_repair", "failed", "stopped", "incomplete"}:
        cause = "worker_incomplete"
    else:
        return {"status": "unknown", "cause": "unknown"}
    return {"status": "failure", "cause": cause}


def _aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("aware timestamp required")
    return value.astimezone(UTC)


def _identity(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 200
        and not any(ord(c) < 32 for c in value)
    )


def failure_decision(observations, policy, *, now):
    """Deduplicate attempts; require a shared cause across distinct task IDs.

    Counts describe latest batch/task observations. Shares use distinct task IDs,
    so repeated attempts or a task repeated across batches cannot create scale.
    Empty valid history is not evidence of systemic failure. Unknown results hold.
    """
    if not isinstance(policy, FailurePolicy):
        raise TypeError("FailurePolicy required")
    now = _aware(now)
    result = {
        "hold": False,
        "cause_counts": {},
        "cause_task_counts": {},
        "reason": "no_recent_observations",
        "observations": 0,
        "distinct_tasks": 0,
        "advisory_only": True,
        "authorizes_spend": False,
    }
    if not isinstance(observations, (list, tuple)) or len(observations) > 100000:
        return dict(result, hold=True, reason="invalid_observations")
    latest = {}
    for row in observations:
        try:
            if (
                not isinstance(row, dict)
                or not _identity(row.get("batch_id"))
                or not _identity(row.get("task_id"))
            ):
                raise ValueError("invalid observation identity")
            at = _aware(row.get("observed_at_utc"))
            if at > now:
                raise ValueError("future observation")
            status, cause = row.get("status"), row.get("cause")
            if not (
                (status == "success" and cause == "validated_success")
                or (
                    status == "failure" and isinstance(cause, str) and cause in FAILURES
                )
                or (status == "unknown" and cause == "unknown")
            ):
                raise ValueError("invalid observation category")
            if at < now - timedelta(seconds=policy.window_seconds):
                continue
            key = (row["batch_id"], row["task_id"])
            value = (at, status, cause)
            previous = latest.get(key)
            if previous and previous[0] == at and previous != value:
                raise ValueError("conflicting duplicate")
            if previous is None or at > previous[0]:
                latest[key] = value
        except (ValueError, TypeError, OverflowError):
            return dict(result, hold=True, reason="invalid_or_future_observation")
    tasks = {key[1] for key in latest}
    counts = Counter(value[2] for value in latest.values() if value[1] == "failure")
    cause_tasks = {
        cause: {
            key[1]
            for key, value in latest.items()
            if value[1] == "failure" and value[2] == cause
        }
        for cause in counts
    }
    result.update(
        cause_counts=dict(sorted(counts.items())),
        cause_task_counts={
            cause: len(ids) for cause, ids in sorted(cause_tasks.items())
        },
        observations=len(latest),
        distinct_tasks=len(tasks),
    )
    if any(value[1] == "unknown" for value in latest.values()):
        return dict(result, hold=True, reason="unknown_observation")
    for cause in sorted(counts):
        if (
            counts[cause] >= policy.minimum_failures
            and len(cause_tasks[cause]) >= policy.minimum_tasks
            and len(cause_tasks[cause]) / len(tasks) >= policy.minimum_share
        ):
            return dict(result, hold=True, reason="shared_failure_cause")
    return dict(
        result, reason="below_failure_threshold" if latest else "no_recent_observations"
    )


def scaling_advice(stats, *, maximum_workers):
    """Recommend at most one additional worker, with no launch/budget side effects.

    Statistics must be matched, explicitly measured windows; queue pressure or
    any recent failure holds growth. Heartbeats/VM counts are not agent-hours.
    """
    if not _integer(maximum_workers, 1, 100):
        raise ValueError("maximum_workers must be an integer from1 to100")
    result = {
        "hold": True,
        "target_workers": None,
        "reason": "unknown_observations",
        "advisory_only": True,
        "authorizes_spend": False,
        "merges_per_agent_hour": None,
    }
    if not isinstance(stats, dict):
        return result
    current = stats.get("current_workers")
    if not _integer(current, 1, maximum_workers):
        return result
    result["target_workers"] = current
    hours, merges = stats.get("agent_hours"), stats.get("merged_prs")
    previous = stats.get("previous_merges_per_agent_hour")
    if not (
        _finite(hours)
        and hours > 0
        and _integer(merges, 0, 1000000000)
        and _finite(previous)
        and previous > 0
        and stats.get("previous_rate_measured") is True
        and stats.get("comparison_matched") is True
        and type(stats.get("queue_pressure")) is bool
        and _finite(stats.get("failure_share"), 0, 1)
    ):
        return result
    rate = merges / hours
    if not math.isfinite(rate):
        return result
    result["merges_per_agent_hour"] = rate
    if stats["queue_pressure"]:
        return dict(result, reason="queue_pressure")
    if stats["failure_share"] > 0:
        return dict(result, reason="recent_failures")
    if merges == 0:
        return dict(result, reason="insufficient_merges")
    if rate < previous:
        return dict(result, reason="efficiency_below_matched_previous")
    if current == maximum_workers:
        return dict(result, reason="maximum_workers_reached")
    return dict(
        result, hold=False, target_workers=current + 1, reason="measured_growth_advice"
    )
