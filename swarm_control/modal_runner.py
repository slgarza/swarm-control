"""Bounded Modal workers with recoverable patches; GitHub writes stay with the integrator."""

import argparse
import fcntl
import hashlib
import json
import math
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from urllib.parse import urlparse

from .context_packet import build_context_readiness, task_packet
from .modal_worker import (
    acceptance_commands,
    acceptance_files,
    benchmark_bundle_summary,
    valid_path,
)
from .pilot_state import batch_summary, common_state, verify_github_tasks
from .progress import WorkerProgress
from .project import load_project

ROOT = Path.cwd()
REPOSITORY = None
APP = None
PROJECT = None


def configure(project):
    global ROOT, REPOSITORY, APP, PROJECT
    ROOT, REPOSITORY, APP, PROJECT = project.root, project.clone_url, project.worker_app, project


def task_directory():
    return PROJECT.tasks_dir if PROJECT else ROOT / "swarm/tasks"


ARTIFACTS = (
    "benchmarks.json",
    "changes.patch",
    "changes.json",
    "checks.log",
    "install.log",
    "versions.json",
    "last-message.txt",
    "supervision.json",
    "acceptance.json",
    "acceptance.log",
)


def now():
    return datetime.now(UTC).isoformat()


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def load_task(task_id):
    from .dependencies import validate_task_resources

    if not re.fullmatch(r"[A-Z][A-Z0-9_-]{1,39}", task_id):
        raise ValueError("invalid task ID")
    path = task_directory() / f"{task_id}.json"
    if path.is_symlink() or not path.resolve().is_relative_to(task_directory().resolve()):
        raise ValueError("task file must stay inside the configured task directory")
    task = json.loads(path.read_text())
    if task.get("id") != task_id:
        raise ValueError("task ID differs from its filename")
    for key in ("title", "prompt"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise ValueError(f"{task_id}: missing {key}")
    if (
        not isinstance(task.get("ownership"), list)
        or not task["ownership"]
        or not all(valid_path(path) for path in task["ownership"])
    ):
        raise ValueError(f"{task_id}: ownership must contain repository-relative paths")
    if (
        not isinstance(task.get("acceptance"), list)
        or not task["acceptance"]
        or not all(
            isinstance(value, str) and value.strip() for value in task["acceptance"]
        )
    ):
        raise ValueError(f"{task_id}: missing acceptance criteria")
    dependencies = task.get("dependsOn", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(dep, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_-]{1,39}", dep)
        or not (task_directory() / f"{dep}.json").is_file()
        for dep in dependencies
    ):
        raise ValueError(f"{task_id}: missing or invalid dependency metadata")
    acceptance_commands(task)
    acceptance_files(task)
    validate_task_resources(task)
    if PROJECT is not None and "issueUrl" in task:
        issue = task.get("issue")
        if (type(issue) is not int or issue <= 0
                or task["issueUrl"] != f"https://github.com/{PROJECT.repository}/issues/{issue}"):
            raise ValueError(f"{task_id}: issueUrl must name this project's exact issue; rebind foreign fork tasks explicitly")
    return task


def overlaps(left, right):
    return any(
        a == b or a.startswith(b + "/") or b.startswith(a + "/")
        for a in left
        for b in right
    )


def overlap_pairs(tasks):
    return [
        [previous["id"], task["id"]]
        for index, task in enumerate(tasks)
        for previous in tasks[:index]
        if overlaps(task["ownership"], previous["ownership"])
    ]


def validate_batch(tasks, workers, *, allow_overlap=False):
    if type(workers) is not int or not 1 <= workers <= 100:
        raise ValueError("workers must be between 1 and 100")
    if not tasks:
        raise ValueError("a batch requires at least one task")
    for index, task in enumerate(tasks):
        for previous in tasks[:index]:
            if task["id"] == previous["id"]:
                raise ValueError(f"duplicate task: {task['id']}")
            if not allow_overlap and overlaps(
                task["ownership"], previous["ownership"]
            ):
                raise ValueError(f"overlapping tasks: {previous['id']} / {task['id']}")


def infrastructure_reserve_cents(task_count):
    return 100 + 30 * task_count


def local_admission_summary(tasks, workers, project):
    """Inspect metadata without contacting services or opening a budget ledger.

    A positive result is preparation evidence only. Live issue/dependency checks,
    remaining durable allowances and deployed provider capacity are not observed.
    """
    issues = {}
    for task in tasks:
        number = task.get("issue")
        if type(number) is int and number > 0:
            issues.setdefault(number, []).append(task["id"])
    blockers = {}
    for task in tasks:
        reasons = []
        if task.get("status", "ready") != "ready":
            reasons.append("task_not_ready")
        if task.get("blockedBy"):
            reasons.append("declared_blockers")
        number = task.get("issue")
        if type(number) is not int or number <= 0:
            reasons.append("missing_issue")
        elif len(issues[number]) != 1:
            reasons.append("duplicate_issue")
        for dependency in task.get("dependsOn", []):
            try:
                path = project.tasks_dir / f"{dependency}.json"
                if path.is_symlink() or not path.resolve().is_relative_to(project.tasks_dir.resolve()):
                    raise ValueError("dependency metadata is outside the task directory")
                metadata = json.loads(path.read_text())
                pull_request = metadata.get("pullRequest")
                if metadata.get("status") != "merged" or type(pull_request) is not int or pull_request <= 0:
                    reasons.append(f"dependency_not_locally_merged:{dependency}")
            except (OSError, ValueError, AttributeError):
                reasons.append(f"dependency_metadata_invalid:{dependency}")
        if reasons:
            blockers[task["id"]] = reasons
    ready = [task["id"] for task in tasks if task["id"] not in blockers]
    reserve = infrastructure_reserve_cents(len(tasks))
    return {
        "scope": "local_metadata_only",
        "budget_accounting_kind": "local_admission_reservations_not_provider_spend",
        "ready_task_ids": ready,
        "ready_task_count": len(ready),
        "blocked_tasks": blockers,
        "maximum_metadata_ready_agents": min(workers, len(ready)),
        "project_capacity_sufficient": max(workers, len(tasks)) <= project.max_live_agents,
        "minimum_infrastructure_reserve_cents": reserve,
        "infrastructure_policy_covers_minimum_reserve": reserve <= project.infra_limit_cents,
        "remaining_model_allowance_cents": None,
        "remaining_infrastructure_allowance_cents": None,
        "provider_billing_queried": False,
        "provider_reported_spend_usd": None,
        "provider_credit_balance_usd": None,
        "provider_funds_exhausted": None,
        "budget_ledger_checked": False,
        "github_readiness_checked": False,
        "provider_capacity_verified": False,
        "launch_readiness_verified": False,
    }


def verify_gateway_capacity(admission, workers, project=None):
    # Older gateways had twenty upstream slots. Their missing field supports
    # small cohorts only; a larger cohort must attest its deployed capacity.
    capacity = admission.get("gateway_concurrency", 20)
    if type(capacity) is not int or not workers <= capacity <= 100:
        raise ValueError(
            "deployed gateway capacity is missing or insufficient for this synchronized cohort; "
            "redeploy the configured gateway before another explicitly budgeted attempt"
        )
    if project is not None and project.gateway_tokens_per_minute is not None:
        expected = {"provider_rate_tpm": project.gateway_tokens_per_minute,
                    "provider_rate_rpm": project.gateway_requests_per_minute,
                    "provider_rate_queue_seconds": 300}
        if any(type(admission.get(key)) is not int or admission[key] != value
               for key, value in expected.items()):
            raise ValueError(
                "deployed gateway provider pacing is missing or differs from the reviewed profile; "
                "redeploy before another explicitly budgeted attempt"
            )


def run_ttl_seconds(agent_timeout):
    """Keep default expiry while covering opt-in longer coding and recovery."""
    if type(agent_timeout) is not int or not 30 <= agent_timeout <= 1800:
        raise ValueError("agent timeout must be between 30 and 1800 seconds")
    return max(1800, agent_timeout + 900)


def dispatch_tasks(tasks, workers, execute, on_complete, *, admit=None):
    """Bound submitted work and checkpoint each completion before admitting more.

    Provider failures produce a failed attempt, never an automatic replacement.
    A checkpoint failure stops new submissions; already submitted workers still
    finish and are recovered by the caller's final reconciliation.
    """
    if not tasks:
        return []
    if type(workers) is not int or not 1 <= workers <= 100:
        raise ValueError("workers must be between 1 and 100")
    remaining = iter(tasks)
    reports, skipped = [], []
    held = False
    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
        pending = {}

        def submit_next():
            nonlocal held
            if held:
                return
            task = next(remaining, None)
            if task is not None:
                if admit is not None and not admit():
                    held = True
                    skipped.extend([task, *remaining])
                    return
                pending[pool.submit(execute, task)] = task

        for _ in range(min(workers, len(tasks))):
            submit_next()
        while pending:
            future = next(as_completed(pending))
            task = pending.pop(future)
            try:
                report = future.result()
            except Exception as error:  # noqa: BLE001 - preserve a launch failure without retrying
                report = {
                    "task_id": task["id"], "status": "launch_failed",
                    "error_type": type(error).__name__, "finished_at": now(),
                }
            on_complete(task, report)
            reports.append(report)
            submit_next()
    for task in skipped:
        report = {"task_id": task["id"], "status": "not_dispatched",
                  "reason": "operations_hold", "finished_at": now()}
        on_complete(task, report)
        reports.append(report)
    return reports


def record_task_result(ledger, batch, task, report=None):
    directory = batch / task["id"]
    directory.mkdir(exist_ok=True)
    path = directory / "report.json"
    if report is None:
        report = json.loads(path.read_text()) if path.exists() else {
            "task_id": task["id"], "status": "launch_failed", "finished_at": now(),
        }
    if report["status"] in {"starting", "preparing", "ready", "working", "checking", "recovering"}:
        report["status"] = "interrupted"
    write_json(path, report)
    ledger.finish(
        task["id"], batch.name,
        "awaiting_integration" if report["status"] == "validated" else report["status"],
        path,
    )


def redact(text):
    return re.sub(r"\bsk-[A-Za-z0-9_-]{16,}", "[REDACTED_OPENAI_KEY]", text)


def eligible_result(agent_exit, check_exit, changes):
    return (
        type(agent_exit) is int and agent_exit == 0
        and type(check_exit) is int and check_exit == 0
        and changes.get("patch_bytes", 0) > 0
        and changes.get("head_unchanged") is True
        and changes.get("ownership_violations") == []
        and changes.get("prohibited_files") == []
    )


def supervision_summary(value, task, base_sha, run_id, timeout, no_progress_timeout=None):
    """Project a bounded worker observation without claiming a provider cause."""
    reasons = {"child_exited", "coding_deadline_reached", "configured_no_source_progress",
               "supervisor_interrupted", "spawn_failed"}
    warning_codes = {"source_observation_unavailable", "no_source_change_observed", "closure_window"}
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("source") != "worker_supervisor_observed"
            or value.get("task_id") != task["id"] or value.get("base_sha") != base_sha
            or value.get("run_id") != run_id or value.get("coding_limit_seconds") != timeout
            or value.get("no_progress_timeout_seconds") != no_progress_timeout
            or value.get("terminal_reason") not in reasons or not value.get("finished_at_utc")
            or type(value.get("automatic_retries")) is not int or value["automatic_retries"] != 0):
        raise ValueError("Invalid or incomplete worker supervision receipt")
    for field in ("started_at_utc", "finished_at_utc"):
        if not isinstance(value.get(field), str) or len(value[field]) > 64:
            raise ValueError("Invalid supervision timestamp")
        if datetime.fromisoformat(value[field]).utcoffset() is None:
            raise ValueError("Supervision timestamps must be timezone aware")
    result = {key: value[key] for key in ("schema_version", "source", "terminal_reason",
        "started_at_utc", "finished_at_utc", "coding_limit_seconds", "automatic_retries")}
    for field in ("child_exit_code", "wrapper_exit_code"):
        code = value.get(field)
        if code is not None and (type(code) is not int or not -255 <= code <= 255):
            raise ValueError("Invalid supervision process exit")
        result[field] = code
    if value["terminal_reason"] == "child_exited":
        child = result["child_exit_code"]
        if child is None or result["wrapper_exit_code"] != (child if child >= 0 else 128 - child):
            raise ValueError("Inconsistent supervision child outcome")
    for field in ("source_change_count", "source_observation_failures", "checkpoint_failures"):
        number = value.get(field, 0)
        if type(number) is not int or not 0 <= number <= 1000000:
            raise ValueError("Invalid supervision observation count")
        result[field] = number
    for field in ("elapsed_seconds", "first_source_change_seconds", "last_source_change_seconds"):
        number = value.get(field)
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number)
                                   or not 0 <= number <= timeout + 60):
            raise ValueError("Invalid supervision duration")
        result[field] = number
    warnings, signals = value.get("warnings", []), value.get("signals", [])
    if (not isinstance(warnings, list) or len(warnings) > 8
            or any(not isinstance(item, str) or item not in warning_codes for item in warnings)
            or not isinstance(signals, list) or len(signals) > 2
            or any(not isinstance(item, str) or item not in {"SIGTERM", "SIGKILL"} for item in signals)):
        raise ValueError("Invalid supervision categories")
    result.update(warnings=warnings, signals=signals, remote_provider_cause=None,
                  useful_progress_verified=False, no_progress_timeout_seconds=no_progress_timeout)
    return result


def recovered_candidate(report):
    """A failed original may yield reviewable bytes, never an accepted delivery."""
    changes = report.get("changes", {})
    if (report.get("status") not in {"needs_repair", "failed", "interrupted"}
            or not isinstance(changes, dict) or type(changes.get("patch_bytes")) is not int
            or changes["patch_bytes"] <= 0 or changes.get("head_unchanged") is not True
            or changes.get("ownership_violations") != [] or changes.get("prohibited_files") != []
            or report.get("recovery_errors") or report.get("patch_redacted")
            or report.get("acceptance_inputs_unchanged") is False
            or not isinstance(report.get("patch_sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", report["patch_sha256"]) is None):
        return None
    return {"schema_version": 1, "status": "candidate",
            "original_worker_status": report["status"], "original_agent_exit": report.get("agent_exit"),
            "patch_sha256": report["patch_sha256"], "base_sha": report["base_sha"],
            "head_unchanged": True, "scope_clean": True,
            "baseline_checks_passed": type(report.get("check_exit")) is int and report["check_exit"] == 0,
            "configured_acceptance_passed": (type(report.get("acceptance_exit")) is int and report["acceptance_exit"] == 0
                if report.get("acceptance_configured") else None),
            "requires_independent_validation": True, "original_outcome_preserved": True,
            "authorizes_publish": False}


def prompt_for(task, *, context_mode="full", context_excerpts=(), timeout=None):
    if context_mode not in {"full", "task"}:
        raise ValueError("context mode must be full or task")
    context = "Read the relevant architecture/product docs. You own ONE ticket.\n"
    if context_mode == "task":
        context = (
            "Use the frozen task contract excerpts below for this ticket. "
            "Required project instructions above still apply in full. "
            "Consult their full source documents when a rule or dependency is unclear. "
            "Do not reread unrelated product material without a task-specific need. "
            "You own ONE ticket.\n"
        )
    closure = ""
    if timeout is not None:
        margin = max(10, min(120, timeout // 5))
        closure = (
            f"The coding limit is {timeout} seconds. Finish edits with about {margin} seconds "
            "remaining for targeted checks and a concise handoff. This planning hint does "
            "not extend the hard deadline. Preserve unfinished changes and report limitations.\n"
        )
    return (
        ("Read " + ", ".join(PROJECT.instructions if PROJECT else ("README.md", "AGENTS.md")) + " before editing.\n")
        + context + closure +
        "Implement the acceptance criteria and run relevant checks.\n"
        "Do not commit, push, open PRs, change branches, read credentials, or spawn other agents.\n"
        "Do not edit outside the listed ownership. If a dependency or specification is missing, "
        "report it rather than inventing requirements. Preserve original IP.\n"
        "Return a concise handoff: changes, validation, unresolved limitations.\n\n"
        + json.dumps(task, indent=2)
        + ("\n\nFrozen contract excerpts:\n" + json.dumps(list(context_excerpts), indent=2)
           if context_excerpts else "")
    )


def build_context_packets(tasks, base_sha, context_mode="full", *, timeout=None):
    result = {}
    for task in tasks:
        excerpts, metadata = task_packet(task, PROJECT, base_sha, context_mode)
        prompt = prompt_for(task, context_mode=context_mode, context_excerpts=excerpts, timeout=timeout)
        result[task["id"]] = {
            "prompt": prompt,
            "metadata": {**metadata, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                         "prompt_bytes": len(prompt.encode())},
        }
    return result


def validated_run_id(value):
    if value is None:
        return None
    if (not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None
            or re.match(r"(?:sk-|gh[pousr]_|github_pat_)", value, re.IGNORECASE)):
        raise ValueError("run ID must be a bounded non-secret identity")
    return value


def bind_report_identity(report, task, run_id, base_sha):
    """Bind all trusted dispatcher outcomes, including work never dispatched."""
    for field, expected in (("task_id", task["id"]), ("run_id", run_id), ("base_sha", base_sha)):
        if field in report and report[field] is not None and report[field] != expected:
            raise ValueError("worker report identity differs from its admitted task")
        report[field] = expected
    return report


def codex_command(model, gateway_url=None):
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--model",
        model,
        "--sandbox",
        "danger-full-access",
        "-c",
        'approval_policy="never"',
        "-c",
        "features.multi_agent=false",
        "-c",
        ('shell_environment_policy.exclude=["CODEX_API_KEY","OPENAI_API_KEY",'
         '"OPENAI_ADMIN_KEY","OPENAI_ADMIN_API_KEY"]'),
        "--cd",
        "/workspace/repo",
        "--output-last-message",
        "/artifacts/last-message.txt",
        "-",
    ]
    if gateway_url:
        settings = {
            "model_provider": "swarm",
            "model_providers.swarm.name": "Swarm Control",
            "model_providers.swarm.base_url": gateway_url.rstrip("/") + "/v1",
            "model_providers.swarm.env_key": "CODEX_API_KEY",
            "model_providers.swarm.wire_api": "responses",
            "model_providers.swarm.supports_websockets": False,
            "model_providers.swarm.requires_openai_auth": False,
            "model_providers.swarm.capabilities.remote_compaction": "unsupported",
            "model_providers.swarm.request_max_retries": 2,
            "model_providers.swarm.stream_max_retries": 1,
            "model_providers.swarm.stream_idle_timeout_ms": 900000,
            "model_context_window": 32768,
            "features.enable_request_compression": False,
            "web_search": "disabled",
        }
        for key, value in settings.items():
            command[-1:-1] = ["-c", f"{key}={json.dumps(value)}"]
    return command


@contextmanager
def controller_lock(state):
    """A single dispatcher across all worktrees sharing this repository."""
    state.mkdir(parents=True, exist_ok=True)
    with (state / "modal-controller.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "another Modal dispatcher is active for this repository"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def consume(process, directory, name, *, progress=None):
    """Drain both streams to avoid pipe stalls. Persist events without printing agent text."""

    def drain(stream, path):
        with path.open("w") as output:

            def persist(line):
                sanitized = redact(line)
                if path.suffix == ".jsonl":
                    try:
                        event = json.loads(sanitized)
                        if isinstance(event, dict):
                            event["swarm_received_at"] = now()
                            if progress is not None:
                                progress.event(event, event["swarm_received_at"])
                            sanitized = json.dumps(event) + "\n"
                    except json.JSONDecodeError:
                        pass
                output.write(sanitized)
                output.flush()

            # Modal streams yield chunks: a chunk can contain several events,
            # or split a JSON object or credential in the middle of a line.
            pending = ""
            for chunk in stream:
                pending += chunk
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    persist(line + "\n")
            if pending:
                persist(pending)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stdout = pool.submit(drain, process.stdout, directory / f"{name}.jsonl")
        stderr = pool.submit(drain, process.stderr, directory / f"{name}.stderr.log")
        process.wait()
        stdout.result()
        stderr.result()
    return process.returncode


def execute_task(task, args, base_sha, batch, image, app, secret, barrier=None):
    import modal

    started = time.monotonic()
    run_id = validated_run_id(getattr(args, "run_id", None))
    context_mode = getattr(args, "context_mode", "full")
    packet = getattr(args, "context_packets", {}).get(task["id"])
    if packet is None:
        packet = build_context_packets([task], base_sha, context_mode, timeout=args.timeout)[task["id"]]
    directory = batch / task["id"]
    directory.mkdir()
    manifest = {
        "repository": REPOSITORY,
        "base_sha": base_sha,
        "run_id": run_id,
        "context_mode": context_mode,
        "context_packet": packet["metadata"],
        "task": task,
        "branch": f"swarm/{task['id'].lower()}-{batch.name[-12:]}",
        "allow_overlap": bool(getattr(args, "allow_overlap", False)),
        "overlap_pairs": [
            pair for pair in getattr(args, "overlap_pairs", []) if task["id"] in pair
        ],
        **(PROJECT.worker_manifest() if PROJECT else {}),
    }
    source_bundle = getattr(args, "source_bundle", None)
    if source_bundle is not None:
        manifest["source_bundle"] = source_bundle.metadata()
    if args.command == "run":
        manifest.update(agent_command=codex_command(args.model, args.gateway_url), supervision={
            "coding_limit_seconds": args.timeout,
            "no_progress_timeout_seconds": getattr(args, "no_progress_timeout", None),
            "poll_seconds": 1.0, "termination_grace_seconds": 5.0})
    report = {
        "task_id": task["id"],
        "run_id": run_id,
        "context_mode": context_mode,
        "base_sha": base_sha,
        "branch": manifest["branch"],
        "model": args.model if args.command == "run" else None,
        "started_at": now(),
        "status": "starting",
        "sandbox_id": None,
        "agent_exit": None,
        "check_exit": None,
        "acceptance_configured": acceptance_commands(task) is not None,
        "acceptance_exit": None,
        "acceptance_inputs_unchanged": None,
        "independent_product_acceptance": False,
        "merged": False,
        "infra_cost_usd": None,
        "model_cost_usd": None,
    }
    write_json(directory / "manifest.json", manifest)
    write_json(directory / "report.json", report)

    def checkpoint_progress(value):
        report["progress"] = value
        saved = dict(report)
        if saved["status"] in {"validated", "smoke_passed"} and not saved.get("finished_at"):
            saved["status"] = "recovering"
        write_json(directory / "report.json", saved)

    progress = WorkerProgress(checkpoint_progress, utc_now=now, monotonic=time.monotonic)

    def measured(phase, operation, *arguments, **keywords):
        with progress.phase(phase):
            return operation(*arguments, **keywords)

    def helper_step(action, phase, timeout):
        with progress.phase(phase) as measurement:
            process = sandbox.exec("python3", "/opt/swarm/modal_worker.py", action,
                                   "/tmp/task.json", timeout=timeout)
            code = consume(process, directory, action)
            measurement["process_exit"] = code if type(code) is int else None
            return code

    sandbox = None
    try:
        sandbox = measured("provision", modal.Sandbox.create,
            app=app,
            image=image,
            timeout=args.timeout + 900,
            runtime="vm",
            cpu=(0.5, 2.0),
            memory=(512, 2048),
            outbound_domain_allowlist=[
                *(PROJECT.network_domains if PROJECT else ("github.com",)),
                *(
                    [urlparse(args.gateway_url).hostname]
                    if getattr(args, "gateway_url", None)
                    else []
                ),
            ],
            tags={"batch": batch.name, "ticket": task["id"], "mode": args.command},
        )
        report.update(sandbox_id=sandbox.object_id, status="preparing")
        write_json(directory / "report.json", report)
        sandbox.filesystem.write_text(json.dumps(manifest), "/tmp/task.json")
        preparation_started = time.monotonic()
        if source_bundle is not None:
            measured("prepare", source_bundle.upload, sandbox)
        remaining = max(0, int(240 - (time.monotonic() - preparation_started))) if source_bundle is not None else 240
        if remaining < 1 or helper_step("prepare", "prepare", remaining) != 0:
            raise RuntimeError("repository setup failed; see install/prepare logs")
        if args.command == "run":
            if barrier is not None:
                report.update(status="ready", ready_at=now())
                write_json(directory / "report.json", report)
                measured("barrier", barrier.wait, timeout=300)
            report.update(status="working", agent_started_at=now())
            write_json(directory / "report.json", report)
            with progress.phase("agent") as measurement:
                process = sandbox.exec(
                    "python3", "/opt/swarm/modal_worker.py", "agent", "/tmp/task.json",
                    secrets=[secret],
                    timeout=args.timeout + 12,
                )
                process.stdin.write(packet["prompt"] + "\n")
                process.stdin.write_eof()
                process.stdin.drain()
                report["agent_exit"] = consume(process, directory, "agent", progress=progress)
                measurement["process_exit"] = report["agent_exit"] if type(report["agent_exit"]) is int else None
                progress.agent_exit(report["agent_exit"])
            report["agent_finished_at"] = now()
        if helper_step("collect", "collect", 30) != 0:
            raise RuntimeError("could not capture changes")
        changes = json.loads(sandbox.filesystem.read_text("/artifacts/changes.json"))
        report["changes"] = changes
        report["checked_patch_sha256"] = hashlib.sha256(
            sandbox.filesystem.read_bytes("/artifacts/changes.patch")
        ).hexdigest()
        if (
            changes["ownership_violations"]
            or changes["prohibited_files"]
            or not changes["head_unchanged"]
        ):
            report["status"] = "rejected"
        elif args.command == "run" and changes.get("patch_bytes") == 0:
            report.update(status="needs_repair", validation_skipped="empty_patch")
        else:
            report["status"] = "checking"
            write_json(directory / "report.json", report)
            validation_started = time.monotonic()
            report["check_exit"] = helper_step("check", "check", 240)
            if args.command == "run" and report["check_exit"] == 0 and report["acceptance_configured"]:
                remaining = int(240 - (time.monotonic() - validation_started))
                if remaining < 1:
                    report["acceptance_not_run"] = "validation_budget_exhausted"
                else:
                    report["acceptance_exit"] = helper_step("acceptance", "acceptance", remaining)
            if args.command == "smoke":
                report["status"] = (
                    "smoke_passed" if report["check_exit"] == 0 else "failed"
                )
            else:
                report["status"] = (
                    "validated"
                    if eligible_result(
                        report["agent_exit"], report["check_exit"], changes
                    ) and (not report["acceptance_configured"] or
                           (type(report["acceptance_exit"]) is int and report["acceptance_exit"] == 0))
                    else "needs_repair"
                )
    except Exception as error:  # noqa: BLE001 - provider failures must still recover and terminate
        report.update(status="failed", error=redact(f"{type(error).__name__}: {error}"))
        if barrier is not None:
            barrier.abort()
    finally:
        if report.get("agent_started_at") and not report.get("agent_finished_at"):
            report["agent_finished_at"] = now()
        if sandbox is not None:
            # Also recover partially completed work after errors/timeouts, before termination.
            recovery_errors = []
            try:
                with progress.phase("recover") as measurement:
                    process = sandbox.exec(
                        "python3", "/opt/swarm/modal_worker.py", "collect",
                        "/tmp/task.json", timeout=30,
                    )
                    recovery_exit = consume(process, directory, "recovery")
                    measurement["process_exit"] = recovery_exit if type(recovery_exit) is int else None
                if recovery_exit != 0:
                    recovery_errors.append("collection_failed")
            except Exception as error:  # noqa: BLE001 - best-effort recovery before termination
                recovery_errors.append(type(error).__name__)
            with progress.phase("artifacts"):
                for name in ARTIFACTS:
                    try:
                        content = sandbox.filesystem.read_bytes(f"/artifacts/{name}")
                        sanitized = redact(content.decode("utf-8", errors="replace"))
                        (directory / name).write_text(sanitized)
                        if name == "changes.patch" and sanitized.encode() != content:
                            report["status"] = "rejected"
                            report["patch_redacted"] = True
                        if name == "benchmarks.json":
                            report["benchmark_receipts"] = {
                                **benchmark_bundle_summary(json.loads(sanitized)),
                                "bundle_sha256": hashlib.sha256(sanitized.encode()).hexdigest(),
                            }
                            if "changes" in report:
                                report["changes"]["benchmark_receipts"] = {
                                    key: value for key, value in report["benchmark_receipts"].items()
                                    if key != "bundle_sha256"
                                }
                    except (
                        FileNotFoundError,
                        modal.exception.SandboxFilesystemNotFoundError,
                    ):
                        continue
                    except Exception as error:  # noqa: BLE001 - record provider retrieval failures
                        recovery_errors.append(f"{name}: {type(error).__name__}")
            patch = directory / "changes.patch"
            recovered_changes = directory / "changes.json"
            if recovered_changes.is_file():
                try:
                    final_changes = json.loads(recovered_changes.read_text())
                    if (not isinstance(final_changes, dict) or type(final_changes.get("patch_bytes")) is not int
                            or final_changes["patch_bytes"] < 0):
                        raise ValueError("invalid collected change metadata")
                    report["changes"] = final_changes
                    if (report["status"] in {"validated", "smoke_passed", "needs_repair"}
                            and (final_changes.get("head_unchanged") is not True
                                 or final_changes.get("ownership_violations") != []
                                 or final_changes.get("prohibited_files") != [])):
                        report["status"] = "rejected"
                except (OSError, ValueError):
                    recovery_errors.append("changes_metadata_unavailable")
            if patch.exists():
                report["patch_sha256"] = hashlib.sha256(patch.read_bytes()).hexdigest()
                if report["status"] in {"validated", "smoke_passed"} and (
                    report["patch_sha256"] != report.get("checked_patch_sha256")
                ):
                    report["status"] = "needs_repair"
                    report["error"] = (
                        "checks modified the candidate patch; review and rerun"
                    )
            else:
                report["status"] = "failed"
                recovery_errors.append("patch_missing")
            if args.command == "run":
                try:
                    raw_supervision = json.loads((directory / "supervision.json").read_text())
                    report["supervision"] = supervision_summary(raw_supervision, task, base_sha, run_id,
                        args.timeout, getattr(args, "no_progress_timeout", None))
                    if (type(report["agent_exit"]) is int and report["agent_exit"] >= 0
                            and report["supervision"]["wrapper_exit_code"] is not None
                            and report["agent_exit"] != report["supervision"]["wrapper_exit_code"]):
                        raise ValueError("Agent exit differs from the supervision receipt")
                    progress.supervision(report["supervision"])
                    if report["supervision"]["checkpoint_failures"]:
                        report["supervision_error"] = "checkpoint_failed"
                    if (report["status"] == "validated" and
                            report["supervision"]["terminal_reason"] != "child_exited"):
                        report["status"] = "needs_repair"
                except (OSError, ValueError, TypeError, RecursionError):
                    report["supervision_error"] = "unavailable_or_invalid"
                if report.get("supervision_error") and report["status"] == "validated":
                    report["status"] = "needs_recovery"
            if report["acceptance_configured"] and args.command == "run" and report["acceptance_exit"] is not None:
                try:
                    accepted = json.loads((directory / "acceptance.json").read_text())
                    if (not isinstance(accepted, dict) or accepted.get("configured") is not True
                            or accepted.get("source") != "frozen_task_commands"
                            or accepted.get("checks_sha256") != hashlib.sha256(
                                json.dumps(acceptance_commands(task), sort_keys=True).encode()).hexdigest()
                            or type(accepted.get("exit_code")) is not int
                            or accepted.get("exit_code") != report["acceptance_exit"]
                            or type(accepted.get("inputs_unchanged")) is not bool):
                        raise ValueError("invalid task acceptance receipt")
                    report["acceptance_inputs_unchanged"] = accepted["inputs_unchanged"]
                    if not accepted["inputs_unchanged"]:
                        report["status"] = "rejected"
                except (OSError, ValueError, TypeError, RecursionError):
                    recovery_errors.append("acceptance_evidence_unavailable")
            if report["status"] in {"validated", "smoke_passed"}:
                for required in (
                    "changes.json",
                    "checks.log",
                    "install.log",
                    "versions.json",
                    "benchmarks.json",
                ):
                    if not (directory / required).is_file():
                        recovery_errors.append(f"{required}: missing")
            report["recovery_errors"] = recovery_errors
            if recovery_errors and report["status"] in {"validated", "smoke_passed"}:
                report["status"] = "needs_recovery"
            try:
                measured("terminate", sandbox.terminate)
                report["sandbox_terminated"] = True
            except Exception as error:  # noqa: BLE001 - preserve evidence of failed cleanup
                report["sandbox_terminated"] = False
                report["termination_error"] = type(error).__name__
        report.update(
            finished_at=now(), wall_seconds=round(time.monotonic() - started, 3),
            progress=progress.snapshot(),
        )
        if report["progress"]["checkpoint_failures"]:
            report.setdefault("recovery_errors", []).append("progress_checkpoint_failed")
            if report["status"] in {"validated", "smoke_passed"}:
                report["status"] = "needs_recovery"
        report["recovered_candidate"] = recovered_candidate(report)
        usage = []
        events = directory / "agent.jsonl"
        if events.exists():
            for line in events.read_text().splitlines():
                try:
                    event = json.loads(line)
                    if event.get("type") == "turn.completed":
                        usage.append(event.get("usage", {}))
                except json.JSONDecodeError:
                    continue
        report["token_usage"] = usage
        write_json(directory / "report.json", report)
        print(
            json.dumps(
                {
                    "task": task["id"],
                    "status": report["status"],
                    "report": str(directory / "report.json"),
                }
            ),
            flush=True,
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "smoke", "run"])
    parser.add_argument("tasks", nargs="+")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--workers", "--agents", dest="workers", type=int, default=1)
    parser.add_argument("--run-id", help="optional local workspace run identity; not an authorization")
    parser.add_argument("--context-mode", choices=("full", "task"), default="full",
                        help="task mode uses explicit frozen context_files contract excerpts; global rules remain required")
    parser.add_argument("--source-transport", choices=("github", "bundle"),
                        help="bundle uploads one exact-base local Git bundle; no GitHub credential reaches workers")
    parser.add_argument(
        "--timeout", type=int, default=300, help="agent time limit in seconds, 30–1800; default 300"
    )
    parser.add_argument("--no-progress-timeout", type=int,
                        help="Explicitly stop after this many seconds without an observed owned-source change (30–coding limit); default warns only")
    parser.add_argument("--require-parallel", type=int,
                        help="Require this many conservatively independent ready tasks before resource admission")
    parser.add_argument("--model", help="explicit OpenAI model ID for a real agent run")
    parser.add_argument("--environment")
    parser.add_argument(
        "--base", default=None, help="reviewed commit or local Git ref"
    )
    parser.add_argument(
        "--model-budget",
        type=float,
        help="explicit remaining pilot model allowance in USD",
    )
    parser.add_argument("--gateway-url", help="deployed bounded gateway HTTPS URL")
    parser.add_argument(
        "--synchronized", action="store_true", help="start one full cohort together"
    )
    parser.add_argument(
        "--allow-overlap", action="store_true",
        help="allow shared ownership in isolated workers; integration must reconcile collisions",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help="explicitly retry a reviewed failed attempt",
    )
    args = parser.parse_args()
    if args.require_parallel is None and args.workers == 100:
        args.require_parallel = 100
    args.run_id = validated_run_id(args.run_id)
    project = load_project(args.project)
    configure(project)
    args.environment = args.environment or project.environment
    args.base = args.base or project.base_ref
    args.source_transport = args.source_transport or getattr(project, "source_transport", "github")
    if args.command != "plan":
        project.require_live()
        project.verify_origin()
    if not 30 <= args.timeout <= 1800:
        parser.error("pilot timeout must be between 30 and 1800 seconds")
    if args.no_progress_timeout is not None and not 30 <= args.no_progress_timeout <= args.timeout:
        parser.error("--no-progress-timeout must be between 30 seconds and the coding limit")
    if args.require_parallel is not None and not 1 <= args.require_parallel <= args.workers:
        parser.error("--require-parallel must fit within the selected worker count")
    if args.command == "run" and not args.model:
        parser.error("run requires --model")
    tasks = [load_task(task) for task in args.tasks]
    if args.allow_overlap and args.command == "smoke":
        parser.error("--allow-overlap is supported by plan and run")
    validate_batch(tasks, args.workers, allow_overlap=args.allow_overlap)
    args.overlap_pairs = overlap_pairs(tasks)
    if args.synchronized and len(tasks) != args.workers:
        parser.error("a synchronized cohort needs exactly one task per worker")
    if args.command != "plan" and (len(tasks) > project.max_live_agents or args.workers > project.max_live_agents):
        parser.error(f"this project permits at most {project.max_live_agents} live agents")
    if args.command == "run" and args.synchronized and args.workers > project.gateway_concurrency:
        parser.error(
            "a synchronized run requires runtime.gateway_concurrency at least its worker count; "
            "update the profile and redeploy the gateway after verifying provider capacity"
        )
    if args.command == "run":
        if args.model_budget is None:
            parser.error(
                "run requires --model-budget within the local admission policy; "
                "provider spending and account credit are separate"
            )
        if args.model != "gpt-6.1-sol":
            parser.error("the priced pilot supports only gpt-6.1-sol")
        parsed = urlparse(args.gateway_url or "")
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".modal.run")
            or parsed.path not in ("", "/")
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.query
            or parsed.fragment
        ):
            parser.error("run requires the deployed gateway HTTPS modal.run origin")
    base_sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", f"{args.base}^{{commit}}"], cwd=ROOT, text=True
    ).strip()
    from .workload import analyze_project, effective_tasks, require_cohort

    dependency_analysis = analyze_project(project, args.tasks, workers=args.workers,
                                         base=base_sha, allow_overlap=args.allow_overlap)
    if args.command != "plan":
        require_cohort(dependency_analysis, args.require_parallel)
    args.context_packets = build_context_packets(tasks, base_sha, args.context_mode, timeout=args.timeout)
    context_readiness = build_context_readiness(tasks, project, base_sha, args.context_mode,
                                              packets=args.context_packets)
    plan = {
        "repository": project.repository,
        "base_sha": base_sha,
        "run_id": args.run_id,
        "context_mode": args.context_mode,
        "context_packets": {task_id: packet["metadata"] for task_id, packet in args.context_packets.items()},
        "context_readiness": context_readiness,
        "dependency_analysis": dependency_analysis,
        "required_parallelism": args.require_parallel,
        "supervision_policy": {"coding_limit_seconds": args.timeout,
                               "no_progress_timeout_seconds": args.no_progress_timeout,
                               "automatic_retries": 0},
        "tasks": [task["id"] for task in tasks],
        "task_states": {task["id"]: task.get("status", "ready") for task in tasks},
        "live_execution_enabled": project.live_enabled,
        "gateway_concurrency": project.gateway_concurrency,
        "gateway_capacity_sufficient": min(args.workers, len(tasks)) <= project.gateway_concurrency,
        "allow_overlap": args.allow_overlap,
        "overlap_pairs": args.overlap_pairs,
        "requested_workers": args.workers,
        "maximum_active_agents": min(args.workers, dependency_analysis["ready_count"]),
        "enough_tasks_for_100": dependency_analysis["supports_100_now"] and args.workers >= 100,
        "agent_timeout_seconds": args.timeout,
        "sandbox_lifecycle_timeout_seconds": args.timeout + 900,
        "gateway_run_ttl_seconds": run_ttl_seconds(args.timeout),
        "configured_provider_pacing": {
            "tokens_per_minute": project.gateway_tokens_per_minute,
            "requests_per_minute": project.gateway_requests_per_minute,
            "provider_quota_verified": False,
        },
        "mode": args.command,
        "source_transport": args.source_transport,
    }
    if args.command == "plan":
        plan["local_admission"] = local_admission_summary(tasks, args.workers, project)
        print(json.dumps(plan, indent=2))
        return 0
    args.source_bundle = None
    if args.source_transport == "bundle":
        from .source_bundle import build_source_bundle

        args.source_bundle = build_source_bundle(ROOT, base_sha, common_state(ROOT))
        plan["source_bundle"] = args.source_bundle.metadata()
    from .operations import OperationsJournal

    operations = OperationsJournal(common_state(ROOT) / "operations.sqlite3")
    operations.require_admission(project.failure_policy)
    import modal

    state = common_state(ROOT)
    with controller_lock(state):
        operations.require_admission(project.failure_policy)
        ledger = project.ledger(state / "control.sqlite3")
        if args.command == "run":
            verify_github_tasks(effective_tasks(tasks, dependency_analysis), ROOT, base_sha,
                                repository=project.repository, tasks_dir=project.tasks_dir)
        app = modal.App.lookup(
            APP, environment_name=args.environment, create_if_missing=True
        )
        image = (
            modal.Image.from_registry(project.image, add_python="3.12")
            .apt_install("git", "ripgrep", "ca-certificates")
            .run_commands(*project.setup_commands)
            .add_local_file(
                Path(__file__).with_name("modal_worker.py"),
                "/opt/swarm/modal_worker.py",
            )
        )
        batch = state / "modal-runs" / ("batch-" + uuid.uuid4().hex[:12])
        batch.mkdir(parents=True)
        plan.update(
            model_budget_usd=args.model_budget if args.command == "run" else 0,
            total_project_budget_usd=(project.model_limit_cents + project.infra_limit_cents) / 100,
            infrastructure_reserve_usd=project.infra_limit_cents / 100,
            synchronized=args.synchronized,
        )
        write_json(batch / "plan.json", plan)
        print(f"Evidence: {batch}", flush=True)
        gateway = None
        tokens = {}
        reports = []
        recorded = set()
        if args.command == "run":
            from decimal import Decimal

            amount = Decimal(str(args.model_budget)) * 100
            if (
                not amount.is_finite()
                or amount != amount.to_integral_value()
                or not 0 < amount <= min(project.model_limit_cents, 18000)
            ):
                raise ValueError(
                    "model budget requires positive whole cents within project policy, at most USD180"
                )
            gateway = {
                name: modal.Function.from_name(
                    project.gateway_app, name, environment_name=args.environment
                )
                for name in ("create_run", "run_status", "stop_run")
            }
            ledger.claim(
                tasks,
                batch.name,
                base_sha,
                retry=args.retry,
                budget_cents=int(amount),
                infra_cents=infrastructure_reserve_cents(len(tasks)),
            )
        else:
            ledger.allocate(batch.name, 0, infra_cents=infrastructure_reserve_cents(len(tasks)))
        try:
            if gateway:
                admission = gateway["create_run"].remote(
                    batch.name, [task["id"] for task in tasks], args.model_budget,
                    run_ttl_seconds(args.timeout)
                )
                tokens = admission.pop("tokens")
                write_json(batch / "budget-start.json", admission)
                if args.synchronized or project.gateway_tokens_per_minute is not None:
                    verify_gateway_capacity(admission, min(args.workers, len(tasks)), project)
            barrier = (
                Barrier(len(tasks))
                if args.synchronized and args.command == "run"
                else None
            )
            def launch(task):
                try:
                    if operations.status(project.failure_policy)["admission_held"]:
                        if barrier is not None:
                            barrier.abort()
                        return {"task_id": task["id"], "status": "not_dispatched",
                                "reason": "operations_hold", "finished_at": now()}
                    return execute_task(
                        task,
                        args,
                        base_sha,
                        batch,
                        image,
                        app,
                        modal.Secret.from_dict({"CODEX_API_KEY": tokens[task["id"]]})
                        if tokens
                        else None,
                        barrier,
                    )
                except BaseException:
                    if barrier is not None:
                        barrier.abort()
                    raise

            def checkpoint(task, report):
                bind_report_identity(report, task, args.run_id, base_sha)
                if args.command == "run":
                    record_task_result(ledger, batch, task, report)
                    recorded.add(task["id"])
                    if report.get("status") != "not_dispatched":
                        operations.observe(batch.name, task["id"], report, project.failure_policy)
                else:
                    directory = batch / task["id"]
                    directory.mkdir(exist_ok=True)
                    write_json(directory / "report.json", report)

            def admit():
                allowed = not operations.status(project.failure_policy)["admission_held"]
                if not allowed and barrier is not None:
                    barrier.abort()
                return allowed

            reports = dispatch_tasks(tasks, args.workers, launch, checkpoint, admit=admit)
        finally:
            tokens.clear()
            if gateway:
                try:
                    write_json(
                        batch / "budget-end.json",
                        gateway["stop_run"].remote(batch.name),
                    )
                except Exception as error:  # noqa: BLE001 - preserve provider cleanup failure evidence
                    write_json(
                        batch / "budget-stop-error.json",
                        {
                            "error_type": type(error).__name__,
                            "expires_in_at_most_seconds": run_ttl_seconds(args.timeout),
                        },
                    )
                for task in tasks:
                    if task["id"] not in recorded:
                        record_task_result(ledger, batch, task)
            write_json(batch / "summary.json", batch_summary(batch))
        return (
            0
            if len(reports) == len(tasks) and reports and all(
                report["status"] in {"validated", "smoke_passed"} for report in reports
            )
            else 1
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
