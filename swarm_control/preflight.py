"""Inspect a proposed cohort without admission, deployment or paid provider calls.

Offline checks read Git, the existing admission ledger and cached billing evidence.
Only --online permits read-only GitHub API queries. This command never attests
provider quota, a deployed gateway or owner authorization and cannot grant launch.
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from . import budget_policy, budget_reporting, modal_runner
from .pilot_integrate import green_checks
from .pilot_state import verify_github_tasks
from .project import load_project


def model_cents(value):
    """An inspection amount is a proposal, never spend authorization."""
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value() or not 0 < amount <= 18000:
            raise ValueError
        return int(amount)
    except (InvalidOperation, ValueError):
        raise ValueError("model budget must be positive whole USD cents, at most USD180") from None


def _git(project, *args):
    return subprocess.check_output(
        ["git", *args], cwd=project.root, text=True, stderr=subprocess.DEVNULL,
    ).strip()


def _github(project, path):
    result = subprocess.run(
        ["gh", "api", "--method", "GET", path], cwd=project.root,
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("GitHub read unavailable")
    return json.loads(result.stdout)


def _checks(query, prefix, base_sha):
    """Require complete, bounded exact-commit check pagination."""
    checks, identifiers, total = [], set(), None
    for page in range(1, 101):
        payload = query(f"{prefix}/commits/{base_sha}/check-runs?per_page=100&page={page}")
        entries, expected = payload.get("check_runs"), payload.get("total_count")
        if (not isinstance(entries, list) or len(entries) > 100
                or type(expected) is not int or expected < 0
                or (total is not None and total != expected)):
            raise ValueError("invalid check pagination")
        total = expected
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError("invalid check entry")
            identifier = entry.get("id")
            if type(identifier) is not int or identifier in identifiers:
                raise ValueError("invalid or duplicate check identifier")
            identifiers.add(identifier)
        checks.extend(entries)
        if len(checks) == total:
            return {"total_count": total, "check_runs": checks}
        if len(checks) > total or len(entries) != 100:
            raise ValueError("incomplete check pagination")
    raise ValueError("check pagination limit exceeded")


def _online(project, tasks, base_sha, query):
    prefix = f"repos/{project.repository}"
    result = {"checked": True, "issues_and_dependencies_verified": False,
              "base_reachable_from_remote_branch": False, "exact_base_ci_green": False,
              "blockers": [], "github_get_requests": 0}

    def counted(path):
        result["github_get_requests"] += 1
        return query(path)

    for name, check in (
        ("issues_and_dependencies_verified", lambda: verify_github_tasks(
            tasks, project.root, base_sha, repository=project.repository,
            tasks_dir=project.tasks_dir, query=counted)),
        ("base_reachable_from_remote_branch", lambda: _remote_base(
            counted, prefix, base_sha, project.base_branch)),
        ("exact_base_ci_green", lambda: green_checks(
            _checks(counted, prefix, base_sha), base_sha, project.required_checks)),
    ):
        try:
            check()
        except (ValueError, KeyError, TypeError, AttributeError, OSError,
                subprocess.SubprocessError):
            # Never echo API bodies, CLI stderr or credentials from exceptions.
            result["blockers"].append(f"github_{name}_failed")
        else:
            result[name] = True
    return result


def _remote_base(query, prefix, base_sha, branch):
    payload = query(f"{prefix}/compare/{base_sha}...{quote(branch, safe='')}")
    if (payload.get("status") not in {"identical", "ahead"}
            or payload.get("merge_base_commit", {}).get("sha") != base_sha):
        raise ValueError("selected base is not reachable from the remote base branch")


def build_preflight(project, task_ids, *, workers=1, synchronized=False, base=None,
                    online=False, model_budget=None, allow_overlap=False, query=None, timeout=300,
                    context_mode="full", require_parallel=None):
    """Return stage-specific evidence; no result authorizes a live operation."""
    modal_runner.configure(project)
    tasks = [modal_runner.load_task(task_id) for task_id in task_ids]
    modal_runner.validate_batch(tasks, workers, allow_overlap=allow_overlap)
    if synchronized and len(tasks) != workers:
        raise ValueError("a synchronized cohort needs exactly one task per worker")
    ttl = modal_runner.run_ttl_seconds(timeout)
    base = base or project.base_ref
    if not isinstance(base, str) or base.startswith("-"):
        raise ValueError("invalid base ref")
    base_sha = _git(project, "rev-parse", "--verify", f"{base}^{{commit}}")
    from .context_packet import build_context_readiness
    from .workload import analyze_project, effective_tasks, preparation_blockers

    if require_parallel is None and workers == 100:
        require_parallel = 100
    if require_parallel is not None and (type(require_parallel) is not int
            or not 1 <= require_parallel <= workers):
        raise ValueError("Required parallelism must be within the selected worker count")
    dependency_analysis = analyze_project(project, task_ids, workers=workers,
                                         base=base_sha, allow_overlap=allow_overlap)
    context_packets = modal_runner.build_context_packets(tasks, base_sha, context_mode, timeout=timeout)
    context_readiness = build_context_readiness(tasks, project, base_sha, context_mode,
                                               packets=context_packets)
    local = modal_runner.local_admission_summary(tasks, workers, project)
    blockers = preparation_blockers(dependency_analysis, require_parallel)
    try:
        project.verify_origin()
    except (ValueError, subprocess.SubprocessError):
        blockers.append("target_origin_mismatch_or_unavailable")
    if _git(project, "rev-parse", "HEAD") != base_sha:
        blockers.append("checkout_head_differs_from_selected_base")
    if _git(project, "status", "--porcelain", "--untracked-files=normal"):
        blockers.append("target_checkout_has_uncommitted_changes")
    if local["blocked_tasks"]:
        blockers.append("task_metadata_not_ready")
    if not local["project_capacity_sufficient"]:
        blockers.append("configured_worker_capacity_insufficient")
    gateway_sufficient = min(workers, len(tasks)) <= project.gateway_concurrency
    if not gateway_sufficient:
        blockers.append("configured_gateway_capacity_insufficient")

    # Both readers use existing SQLite mode=ro and do not create a Ledger.
    state = budget_policy.inspect(project)
    budget = budget_reporting.build_status(project)
    selected_claims = {}
    if state["ledger_exists"]:
        with budget_policy.connection(budget_policy.ledger_path(project), "ro") as db:
            selected_claims = {task_id: status for task_id, status in db.execute(
                "SELECT task_id,state FROM tasks") if task_id in task_ids}
    if selected_claims:
        blockers.append("selected_tasks_already_have_durable_claims")

    local_budget = budget["local_admission_policy"]
    proposed = model_cents(model_budget) if model_budget is not None else None
    unreserved = local_budget["unreserved_cents"]
    budget_blockers = []
    if local_budget["profile_matches_policy"] is False:
        budget_blockers.append("budget_profile_policy_mismatch")
    if state.get("active_task_count", 0):
        budget_blockers.append("existing_active_or_awaiting_tasks")
    if unreserved is None:
        budget_blockers.append("durable_unreserved_allowance_not_observed")
    else:
        if unreserved["infrastructure"] < local["minimum_infrastructure_reserve_cents"]:
            budget_blockers.append("local_infrastructure_reserve_insufficient")
        if proposed is not None and unreserved["model"] < proposed:
            budget_blockers.append("local_model_reserve_insufficient")
    if proposed is None:
        budget_blockers.append("cohort_model_reserve_not_selected")
    github = (_online(project, effective_tasks(tasks, dependency_analysis), base_sha,
                      query or (lambda path: _github(project, path)))
              if online else {"checked": False, "issues_and_dependencies_verified": None,
                              "base_reachable_from_remote_branch": None, "exact_base_ci_green": None,
                              "blockers": ["github_readiness_not_checked"], "github_get_requests": 0})
    launch_blockers = [*blockers, *budget_blockers, *github["blockers"],
                       "provider_quota_not_verified", "deployed_gateway_capacity_not_verified",
                       "owner_launch_authorization_not_verified"]
    if not project.live_enabled:
        launch_blockers.append("live_execution_disabled")
    active_workers = min(workers, len(tasks))
    return {
        "version": 1, "kind": "cohort_preflight", "repository": project.repository,
        "recorded_at_utc": datetime.now(UTC).isoformat(), "base_sha": base_sha,
        "task_ids": task_ids, "requested_workers": workers, "synchronized": synchronized,
        "dependency_analysis": dependency_analysis, "context_readiness": context_readiness,
        "required_parallelism": require_parallel,
        "read_only": True, "cloud_resources_created": False, "local_allocations_changed": False,
        "authorizes_spend": False, "launch_ready": False,
        "preparation_checks_passed": not blockers and (not online or not github["blockers"]),
        "exit_status_scope": "checked_source_preparation_only_not_budget_or_launch_readiness",
        "local_workload_ready": not blockers,
        "github_source_ready": not github["blockers"] if online else None,
        "local_admission_reserve_sufficient": not budget_blockers,
        "local_workload_blockers": blockers, "local_admission_blockers": budget_blockers,
        "launch_blockers": launch_blockers, "local_admission": local,
        "configured_gateway_capacity_sufficient": gateway_sufficient,
        "configured_provider_pacing": {
            "tokens_per_minute": project.gateway_tokens_per_minute,
            "requests_per_minute": project.gateway_requests_per_minute,
            "enabled": project.gateway_tokens_per_minute is not None,
            "queue_seconds": 300 if project.gateway_tokens_per_minute is not None else 0,
            "deployed_match_verified": False,
        },
        "selected_existing_claims": selected_claims,
        "proposed_model_reserve_cents": proposed,
        "budget_state_sha256": state.get("state_sha256"), "budget_status": budget,
        "github": github,
        "credentials": {"github_cli_available": shutil.which("gh") is not None,
                        "modal_sdk_available": importlib.util.find_spec("modal") is not None,
                        "provider_authentication_verified": False,
                        "billing_admin_key_configured": bool(os.environ.get("OPENAI_ADMIN_KEY")),
                        "billing_admin_key_required_for_launch": False},
        "provider_quota_verified": False, "deployed_gateway_capacity_verified": False,
        "resource_plan": {
            "source": {"workers": "swarm_control/modal_runner.py: execute_task Sandbox.create",
                       "gateway": "swarm_control/pilot_gateway.py: gateway function"},
            "worker_requests": {"cpu_per_worker": 0.5, "memory_mib_per_worker": 512,
                                "total_cpu": 0.5 * active_workers,
                                "total_memory_mib": 512 * active_workers},
            "worker_caps": {"cpu_per_worker": 2, "memory_mib_per_worker": 2048,
                            "total_cpu": 2 * active_workers,
                            "total_memory_mib": 2048 * active_workers},
            "gateway_request": {"cpu": 0.25, "memory_mib": 256},
            "logical_container_inventory": {
                "workers": active_workers, "gateway": 1, "minimum_count": active_workers + 1,
                "recommended_with_one_admin_helper": active_workers + 2,
                "additional_headroom_requires_provider_verification": True,
            },
            "provider_quota_pools_verified": False, "effective_capacity_verified": False,
            "interpretation": "Requests and upper caps describe configuration, not verified provider reservations or a shared quota pool.",
        },
        "runtime_constraints": {"agent_timeout_seconds_default": 300,
                                "agent_timeout_seconds": timeout,
                                "agent_timeout_seconds_max": 1800,
                                "sandbox_lifecycle_timeout_seconds": timeout + 900,
                                "gateway_run_ttl_seconds": ttl,
                                "synchronized_preparation_barrier_seconds": 300,
                                "vm_cpu_limit": 2, "vm_memory_mib_limit": 2048,
                                "automatic_worker_replacement": False},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", nargs="+")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--synchronized", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--allow-overlap", action="store_true")
    parser.add_argument("--context-mode", choices=("full", "task"), default="full")
    parser.add_argument("--require-parallel", type=int,
                        help="Require a conservative independent ready frontier of this size")
    parser.add_argument("--model-budget", help="proposed cohort model reserve in USD; not authorization")
    parser.add_argument("--online", action="store_true", help="permit read-only GitHub GET requests")
    args = parser.parse_args()
    result = build_preflight(
        load_project(args.project), args.tasks, workers=args.workers,
        synchronized=args.synchronized, base=args.base, online=args.online,
        model_budget=args.model_budget, allow_overlap=args.allow_overlap,
        timeout=args.timeout,
        context_mode=args.context_mode, require_parallel=args.require_parallel,
    )
    print(json.dumps(result, indent=2))
    # Exit describes checked source preparation, never financial or launch approval.
    return 0 if result["preparation_checks_passed"] else 1
