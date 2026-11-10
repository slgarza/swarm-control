"""Offline, allowlisted evidence export and paired efficiency comparison.

This summarizes local receipts; it neither queries services nor certifies that
an agent was continuously productive or that a benchmark was independently run.
"""

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

SHA = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
TASK_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,39}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
STATUSES = {"starting", "preparing", "ready", "working", "checking", "validated",
            "failed", "rejected", "needs_repair", "needs_recovery", "interrupted",
            "launch_failed", "smoke_passed"}
ARTIFACTS = ("manifest.json", "report.json", "agent.jsonl", "changes.patch", "integration.patch",
             "changes.json", "integration.json", "checks.log", "versions.json")
PROTOCOL_FIELDS = {"comparison_id", "baseline_configuration_sha256", "acceptance_policy_sha256",
                   "cost_policy_sha256", "clock_definition", "observation_window_seconds"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_json(path):
    return json.loads(Path(path).read_text())


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("an explicit timezone-aware timestamp is required")  # noqa: TRY004 - malformed evidence
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid timestamp") from None
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return result.astimezone(UTC)


def overlap(intervals, threshold=100):
    """Half-open intervals: coincident starts/ends never inflate the peak."""
    changes = Counter()
    for start, end in intervals:
        if start < end:
            changes[start] += 1
            changes[end] -= 1
    peak = current = 0
    duration = 0.0
    previous = None
    for at, delta in sorted(changes.items()):
        if previous is not None and current >= threshold:
            duration += (at - previous).total_seconds()
        current += delta
        peak = max(peak, current)
        previous = at
    return {"peak": peak, "seconds_at_or_above_100": duration}


def cost_summary(value=None):
    """Invoices and exemption attestations are explicit operator inputs."""
    if value is None:
        return {"reconciliation_status": "unmeasured", "gross_total": None,
                "confirmed_internal_token_exemption": None, "contest_eligible_total": None,
                "exemption_evidence_sha256": None}
    if not isinstance(value, dict) or value.get("reconciliation_status") != "reconciled":
        raise ValueError("cost receipt must be reconciled")
    fields = ("gross_total", "confirmed_internal_token_exemption", "contest_eligible_total")
    amounts = [value.get(key) for key in fields]
    if any(type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0
           for amount in amounts):
        raise ValueError("cost receipt requires finite nonnegative USD amounts")
    if not math.isclose(amounts[0] - amounts[1], amounts[2], rel_tol=0, abs_tol=0.000001):
        raise ValueError("eligible cost must equal gross cost minus confirmed exemption")
    attestation = value.get("exemption_evidence_sha256")
    if amounts[1] > 0 and (not isinstance(attestation, str) or not DIGEST.fullmatch(attestation)):
        raise ValueError("a positive exemption requires a confirmation evidence SHA256")
    return {"reconciliation_status": "reconciled", **dict(zip(fields, amounts, strict=True)),
            "exemption_evidence_sha256": attestation if amounts[1] > 0 else None}


def rates(deliveries, elapsed, costs):
    cost = costs.get("contest_eligible_total")
    reconciled = costs.get("reconciliation_status") == "reconciled"
    valid_count = type(deliveries) is int and deliveries >= 0
    return {
        "merged_green_prs_per_hour": deliveries * 3600 / elapsed
        if valid_count and type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed > 0 else None,
        "merged_green_prs_per_eligible_dollar": deliveries / cost
        if valid_count and reconciled and type(cost) in (int, float) and math.isfinite(cost) and cost > 0 else None,
        "eligible_cost_per_delivery_usd": cost / deliveries
        if valid_count and reconciled and type(cost) in (int, float) and math.isfinite(cost) and cost >= 0 and deliveries > 0 else None,
    }


def protocol_digest(protocol):
    if protocol is None:
        return None
    if not isinstance(protocol, dict) or not PROTOCOL_FIELDS <= protocol.keys():
        raise ValueError("comparison protocol is missing required fields")
    for key in PROTOCOL_FIELDS - {"observation_window_seconds"}:
        value = protocol[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("comparison protocol fields must be nonempty strings")
        if key.endswith("_sha256") and not DIGEST.fullmatch(value):
            raise ValueError("comparison protocol requires SHA256 digests")
    window = protocol["observation_window_seconds"]
    if type(window) is not int or window < 0:
        raise ValueError("observation window must be nonnegative whole seconds")
    return digest(canonical(protocol))


def collect(batch, *, pipeline="swarm", started_at=None, ended_at=None, costs=None,
            protocol=None, acceptance=None):
    batch = Path(batch)
    if batch.is_symlink() or (batch / "plan.json").is_symlink():
        raise ValueError("symlink evidence is prohibited")
    plan = read_json(batch / "plan.json")
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")  # noqa: TRY004 - malformed evidence
    repository, base = plan.get("repository"), plan.get("base_sha")
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValueError("invalid repository in plan")
    if not isinstance(base, str) or not SHA.fullmatch(base):
        raise ValueError("invalid base SHA in plan")
    task_ids = plan.get("tasks")
    if not isinstance(task_ids, list) or not task_ids or any(
        not isinstance(task, str) or not TASK_ID.fullmatch(task) for task in task_ids
    ) or len(set(task_ids)) != len(task_ids):
        raise ValueError("invalid or duplicate tasks in plan")
    if pipeline not in {"swarm", "afk"}:
        raise ValueError("pipeline must be swarm or afk")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", batch.name):
        raise ValueError("invalid batch identity")
    protocol_sha = protocol_digest(protocol)
    if (started_at is None) != (ended_at is None):
        raise ValueError("supply both delivery clock timestamps")
    clock_start = timestamp(started_at) if started_at else None
    clock_end = timestamp(ended_at) if ended_at else None
    if clock_start and clock_end <= clock_start:
        raise ValueError("delivery clock must have positive duration")
    elapsed = (clock_end - clock_start).total_seconds() if clock_start else None
    accepted = None
    if acceptance is not None:
        if not isinstance(acceptance, list) or any(not isinstance(item, dict) for item in acceptance):
            raise ValueError("acceptance receipts must be a list of objects")
        accepted = {item.get("task_id"): item for item in acceptance}
        if len(accepted) != len(acceptance) or not set(accepted) <= set(task_ids):
            raise ValueError("duplicate or unknown acceptance receipt")
    gaps, tasks, manifests, source_hashes = [], [], [], []
    sessions, sampled = [], []
    identities = []
    for task_id in sorted(task_ids):
        directory = batch / task_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(batch.resolve()):
            raise ValueError("task evidence must stay inside its batch")
        for name in ARTIFACTS:
            path = directory / name
            if path.is_symlink():
                raise ValueError("symlink evidence is prohibited")
            if path.is_file():
                source_hashes.append({"task_id": task_id, "artifact": name, "sha256": digest(path.read_bytes())})
        try:
            manifest, report = read_json(directory / "manifest.json"), read_json(directory / "report.json")
            if manifest.get("repository") != f"https://github.com/{repository}.git" or manifest.get("base_sha") != base:
                raise ValueError("worker source mismatch")
            if manifest.get("task", {}).get("id") != task_id or report.get("task_id") != task_id or report.get("base_sha") != base:
                raise ValueError("worker identity mismatch")
            manifests.append(manifest["task"])
        except (FileNotFoundError, ValueError, TypeError, AttributeError):
            gaps.append({"task_id": task_id, "code": "missing_or_inconsistent_worker"})
            continue
        status = report.get("status")
        exported = {"task_id": task_id, "status": status if isinstance(status, str) and status in STATUSES else "unknown",
                    "commands_completed": 0, "model_turns_completed": 0,
                    "patch_sha256": None, "merged_receipt": False, "accepted_delivery": None}
        patch_path = directory / "changes.patch"
        patch_hash = digest(patch_path.read_bytes()) if patch_path.is_file() else None
        changes = report.get("changes", {})
        valid_patch = (patch_path.is_file() and patch_path.stat().st_size > 0
                       and type(report.get("agent_exit")) is int and report["agent_exit"] == 0
                       and type(report.get("check_exit")) is int and report["check_exit"] == 0
                       and isinstance(changes, dict) and changes.get("head_unchanged") is True
                       and changes.get("ownership_violations") == [] and changes.get("prohibited_files") == []
                       and report.get("recovery_errors") == [] and not report.get("patch_redacted")
                       and patch_hash == report.get("patch_sha256") == report.get("checked_patch_sha256"))
        exported["patch_sha256"] = patch_hash
        identity = report.get("sandbox_id")
        identity = identity if isinstance(identity, str) and identity else None
        identities.append(identity)
        interval, activity = None, []
        try:
            start, end = timestamp(report.get("agent_started_at")), timestamp(report.get("agent_finished_at"))
            if end <= start or (clock_start and (start < clock_start or end > clock_end)):
                raise ValueError("worker outside delivery clock")
            interval = (start, end)
        except ValueError:
            gaps.append({"task_id": task_id, "code": "invalid_or_missing_session_interval"})
        if clock_start:
            try:
                lifecycle_start = timestamp(report.get("started_at"))
                lifecycle_end = timestamp(report.get("finished_at"))
                if not clock_start <= lifecycle_start <= lifecycle_end <= clock_end:
                    raise ValueError("worker lifecycle outside delivery clock")
            except ValueError:
                gaps.append({"task_id": task_id, "code": "invalid_or_missing_worker_lifecycle"})
        events = directory / "agent.jsonl"
        if events.is_file():
            for line in events.read_text().splitlines():
                try:
                    event = json.loads(line)
                    at = timestamp(event.get("swarm_received_at"))
                    if interval is None or not interval[0] <= at <= interval[1]:
                        raise ValueError("event outside session")
                    item = event.get("item", {})
                    if event.get("type") == "item.completed" and item.get("type") == "command_execution":
                        exported["commands_completed"] += 1
                        activity.append(at)
                    if event.get("type") == "turn.completed":
                        usage = event.get("usage", {})
                        if any(type(usage.get(key)) is int and usage[key] > 0 for key in ("input_tokens", "output_tokens")):
                            exported["model_turns_completed"] += 1
                            activity.append(at)
                except (ValueError, TypeError, AttributeError):
                    gaps.append({"task_id": task_id, "code": "invalid_activity_event"})
        duplicate_identity = not identity or identity in identities[:-1]
        if duplicate_identity:
            gaps.append({"task_id": task_id, "code": "missing_or_duplicate_session_identity"})
        if interval and not duplicate_identity:
            sessions.append((task_id, identity, interval))
            if valid_patch and status == "validated" and exported["commands_completed"] and exported["model_turns_completed"] and len(activity) >= 2:
                sampled.append((task_id, identity, (min(activity), max(activity))))
        integration_path = directory / "integration.json"
        if integration_path.is_file():
            integration = read_json(integration_path)
            head, merged_sha = integration.get("head_sha"), integration.get("merge_commit_sha")
            try:
                merged_at = timestamp(integration.get("merged_at"))
                if clock_end and not clock_start <= merged_at <= clock_end:
                    raise ValueError("merge outside delivery clock")
            except ValueError:
                merged_at = None
            merged = (valid_patch and status == "validated" and integration.get("task_id") == task_id
                      and integration.get("batch_id") == batch.name and integration.get("patch_sha256") == patch_hash
                      and integration.get("status") == "merged" and isinstance(head, str) and SHA.fullmatch(head)
                      and integration.get("green_head_sha") == head and isinstance(merged_sha, str)
                      and SHA.fullmatch(merged_sha) and merged_at is not None)
            exported["merged_receipt"] = bool(merged)
            if merged:
                exported.update(reviewed_head_sha=head, merge_commit_sha=merged_sha, merged_at_utc=merged_at.isoformat())
                if clock_start:
                    try:
                        checks_start = timestamp(integration.get("checks_started_at"))
                        checks_end = timestamp(integration.get("checks_finished_at"))
                        if not clock_start <= checks_start <= checks_end <= merged_at:
                            raise ValueError("checks outside delivery clock")
                        if integration.get("preparation_started_at") is not None:
                            prepared = timestamp(integration["preparation_started_at"])
                            if not clock_start <= prepared <= checks_start:
                                raise ValueError("preparation outside delivery clock")
                    except ValueError:
                        gaps.append({"task_id": task_id, "code": "invalid_or_missing_integration_interval"})
            receipt = accepted.get(task_id) if accepted is not None else None
            if receipt is not None:
                try:
                    observation = timestamp(receipt.get("observation_ended_at"))
                    long_enough = merged_at is not None and protocol is not None and (
                        observation - merged_at).total_seconds() >= protocol["observation_window_seconds"]
                except ValueError:
                    long_enough = False
                exported["accepted_delivery"] = bool(
                    merged and receipt.get("patch_sha256") == patch_hash and receipt.get("reviewed_head_sha") == head
                    and receipt.get("merge_commit_sha") == merged_sha and receipt.get("accepted") is True
                    and receipt.get("post_merge_checks") == "success" and receipt.get("reverted") is False
                    and isinstance(receipt.get("evidence_sha256"), str) and DIGEST.fullmatch(receipt["evidence_sha256"])
                    and long_enough)
        tasks.append(exported)
    # Exclude every occurrence of a duplicated session, including the first.
    counts = Counter(identity for identity in identities if identity)
    session_stats = overlap([interval for _, identity, interval in sessions if counts[identity] == 1])
    sampled_stats = overlap([interval for _, identity, interval in sampled if counts[identity] == 1])
    if accepted is not None:
        missing = [item["task_id"] for item in tasks if item["merged_receipt"] and item["accepted_delivery"] is None]
        gaps.extend({"task_id": task_id, "code": "missing_acceptance_receipt"} for task_id in missing)
        delivery_count = None if missing or len(tasks) != len(task_ids) else sum(item["accepted_delivery"] is True for item in tasks)
    else:
        delivery_count = None
    original_cost_hash = digest(canonical(costs)) if costs is not None else None
    costs = cost_summary(costs)
    coverage_valid = clock_start is not None and not any(item["code"] in {
        "missing_or_inconsistent_worker", "invalid_or_missing_session_interval", "invalid_or_missing_worker_lifecycle",
        "invalid_or_missing_integration_interval", "invalid_activity_event"
    } for item in gaps)
    return {
        "report_format_version": 1, "evidence_format": "swarm-control/v1", "is_template": False,
        "run_id": batch.name, "pipeline": pipeline, "target_repository": repository, "base_sha": base,
        "task_manifest_sha256": digest(canonical(manifests)) if len(manifests) == len(task_ids) else None,
        "comparison_protocol_sha256": protocol_sha,
        "clock": {"source": "operator_supplied_delivery_clock", "started_at_utc": clock_start.isoformat() if clock_start else None,
                  "ended_at_utc": clock_end.isoformat() if clock_end else None, "elapsed_seconds": elapsed,
                  "coverage_validated": coverage_valid},
        "work": {"backlog_items": len(task_ids), "observed_merged_prs": sum(item["merged_receipt"] for item in tasks),
                 "merged_green_prs": delivery_count},
        "concurrency": {"observed_peak_sessions": session_stats["peak"],
                        "session_overlap_at_or_above_100_seconds": session_stats["seconds_at_or_above_100"],
                        "sampled_contribution_peak": sampled_stats["peak"],
                        "sampled_contribution_overlap_at_or_above_100_seconds": sampled_stats["seconds_at_or_above_100"],
                        "useful_activity_measurement": "first to last observed command/model event, with a validated nonempty patch; spans may contain idle gaps",
                        "scale_requirement_verified": False, "evidence_gaps": gaps},
        "cost_usd": costs, "rates": rates(delivery_count, elapsed if coverage_valid else None, costs), "deliveries": tasks,
        "source_artifacts": source_hashes,
        "input_receipts_sha256": {"plan": digest((batch / "plan.json").read_bytes()),
                                  "cost": original_cost_hash,
                                  "acceptance": digest(canonical(acceptance)) if acceptance is not None else None},
        "limitations": ["Local receipts require independent audit; overlap spans do not certify continuous useful work.",
                        "This export does not establish a qualifying live cohort or organizer-approved baseline."],
    }


def compare(swarm, afk):
    reasons = []
    for report, pipeline in ((swarm, "swarm"), (afk, "afk")):
        if report.get("evidence_format") != "swarm-control/v1" or report.get("is_template") is not False or report.get("pipeline") != pipeline:
            raise ValueError("comparison requires measured swarm and afk evidence reports")
    if swarm.get("run_id") == afk.get("run_id"):
        reasons.append("run_identities_are_not_distinct")
    for key in ("target_repository", "base_sha", "task_manifest_sha256", "comparison_protocol_sha256"):
        if not swarm.get(key) or swarm.get(key) != afk.get(key):
            reasons.append("missing_or_mismatched_" + key)
    computed = {}
    for report, name in ((swarm, "swarm"), (afk, "afk")):
        clock = report.get("clock", {})
        try:
            elapsed = (timestamp(clock.get("ended_at_utc")) - timestamp(clock.get("started_at_utc"))).total_seconds()
            recorded = clock.get("elapsed_seconds")
            if (clock.get("coverage_validated") is not True or elapsed <= 0
                    or type(recorded) not in (int, float) or not math.isfinite(recorded)
                    or not math.isclose(elapsed, recorded, rel_tol=0, abs_tol=0.000001)):
                raise ValueError("inconsistent clock")
        except ValueError:
            elapsed = None
            reasons.append(name + "_has_invalid_delivery_clock")
        work, deliveries = report.get("work", {}), report.get("deliveries")
        count, backlog = work.get("merged_green_prs"), work.get("backlog_items")
        valid_deliveries = isinstance(deliveries, list) and all(isinstance(item, dict) for item in deliveries)
        if valid_deliveries:
            ids = [item.get("task_id") for item in deliveries]
            valid_deliveries = (all(isinstance(task_id, str) and TASK_ID.fullmatch(task_id) for task_id in ids)
                                and len(set(ids)) == len(ids) and type(backlog) is int and len(ids) == backlog)
        if valid_deliveries:
            for item in deliveries:
                if item.get("accepted_delivery") is True:
                    valid_deliveries = valid_deliveries and item.get("merged_receipt") is True and all(
                        isinstance(item.get(key), str) and pattern.fullmatch(item[key]) for key, pattern in (
                            ("patch_sha256", DIGEST), ("reviewed_head_sha", SHA), ("merge_commit_sha", SHA)))
            if count is not None:
                valid_deliveries = (valid_deliveries and type(count) is int and count >= 0
                                    and count == sum(item.get("accepted_delivery") is True for item in deliveries))
            valid_deliveries = valid_deliveries and work.get("observed_merged_prs") == sum(
                item.get("merged_receipt") is True for item in deliveries)
        if not valid_deliveries:
            count = None
            reasons.append(name + "_has_inconsistent_delivery_receipts")
        costs = report.get("cost_usd", {})
        if costs.get("reconciliation_status") == "reconciled":
            costs = cost_summary(costs)
        computed[name] = rates(count, elapsed, costs)
        if any(computed[name][metric] is None for metric in (
            "merged_green_prs_per_hour", "merged_green_prs_per_eligible_dollar"
        )):
            reasons.append(name + "_has_unknown_or_zero_denominators")
    ratio = {}
    for metric in ("merged_green_prs_per_hour", "merged_green_prs_per_eligible_dollar"):
        numerator, denominator = computed["swarm"][metric], computed["afk"][metric]
        ratio[metric] = numerator / denominator if numerator is not None and denominator is not None and denominator > 0 else None
    comparable = not reasons and all(value is not None for value in ratio.values())
    if not comparable and not reasons:
        reasons.append("baseline_has_zero_delivery_rate")
    return {"comparable": comparable, "reasons": reasons, "rates": computed, "rate_ratios": ratio,
            "efficiency_win": all(value > 1 for value in ratio.values()) if comparable else None,
            "contest_win_proven": False,
            "limitation": "Numerical efficiency requires independent acceptance, scale and baseline review; local exports are not certificates."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("collect")
    export.add_argument("batch", type=Path)
    export.add_argument("--pipeline", choices=("swarm", "afk"), default="swarm")
    export.add_argument("--started-at")
    export.add_argument("--ended-at")
    export.add_argument("--cost-receipt", type=Path)
    export.add_argument("--protocol", type=Path)
    export.add_argument("--acceptance-receipts", type=Path)
    paired = commands.add_parser("compare")
    paired.add_argument("swarm", type=Path)
    paired.add_argument("afk", type=Path)
    args = parser.parse_args()
    if args.command == "collect":
        result = collect(args.batch, pipeline=args.pipeline, started_at=args.started_at, ended_at=args.ended_at,
                         costs=read_json(args.cost_receipt) if args.cost_receipt else None,
                         protocol=read_json(args.protocol) if args.protocol else None,
                         acceptance=read_json(args.acceptance_receipts) if args.acceptance_receipts else None)
    else:
        result = compare(read_json(args.swarm), read_json(args.afk))
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0
