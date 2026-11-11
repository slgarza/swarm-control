"""Bounded Modal workers with recoverable patches; GitHub writes stay with the integrator."""

import argparse
import fcntl
import hashlib
import json
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

from .modal_worker import benchmark_bundle_summary, valid_path
from .pilot_state import batch_summary, common_state, verify_github_tasks
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
)


def now():
    return datetime.now(UTC).isoformat()


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def load_task(task_id):
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
    if report["status"] in {"starting", "preparing", "ready", "working", "checking"}:
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
        agent_exit == 0
        and check_exit == 0
        and changes.get("patch_bytes", 0) > 0
        and changes.get("head_unchanged") is True
        and changes.get("ownership_violations") == []
        and changes.get("prohibited_files") == []
    )


def prompt_for(task):
    return (
        ("Read " + ", ".join(PROJECT.instructions if PROJECT else ("README.md", "AGENTS.md")) + " before editing.\n")
        +
        "Read the relevant architecture/product docs. You own ONE ticket.\n"
        "Implement the acceptance criteria and run relevant checks.\n"
        "Do not commit, push, open PRs, change branches, read credentials, or spawn other agents.\n"
        "Do not edit outside the listed ownership. If a dependency or specification is missing, "
        "report it rather than inventing requirements. Preserve original IP.\n"
        "Return a concise handoff: changes, validation, unresolved limitations.\n\n"
        + json.dumps(task, indent=2)
    )


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


def consume(process, directory, name):
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
    directory = batch / task["id"]
    directory.mkdir()
    manifest = {
        "repository": REPOSITORY,
        "base_sha": base_sha,
        "task": task,
        "branch": f"swarm/{task['id'].lower()}-{batch.name[-12:]}",
        "allow_overlap": bool(getattr(args, "allow_overlap", False)),
        "overlap_pairs": [
            pair for pair in getattr(args, "overlap_pairs", []) if task["id"] in pair
        ],
        **(PROJECT.worker_manifest() if PROJECT else {}),
    }
    report = {
        "task_id": task["id"],
        "base_sha": base_sha,
        "branch": manifest["branch"],
        "model": args.model if args.command == "run" else None,
        "started_at": now(),
        "status": "starting",
        "sandbox_id": None,
        "agent_exit": None,
        "check_exit": None,
        "merged": False,
        "infra_cost_usd": None,
        "model_cost_usd": None,
    }
    write_json(directory / "manifest.json", manifest)
    write_json(directory / "report.json", report)
    sandbox = None
    try:
        sandbox = modal.Sandbox.create(
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
        helper = ["python3", "/opt/swarm/modal_worker.py"]
        process = sandbox.exec(*helper, "prepare", "/tmp/task.json", timeout=240)
        if consume(process, directory, "prepare") != 0:
            raise RuntimeError("repository setup failed; see install/prepare logs")
        if args.command == "run":
            if barrier is not None:
                report.update(status="ready", ready_at=now())
                write_json(directory / "report.json", report)
                barrier.wait(timeout=300)
            report.update(status="working", agent_started_at=now())
            write_json(directory / "report.json", report)
            process = sandbox.exec(
                *codex_command(args.model, args.gateway_url),
                secrets=[secret],
                timeout=args.timeout,
            )
            process.stdin.write(prompt_for(task) + "\n")
            process.stdin.write_eof()
            process.stdin.drain()
            report["agent_exit"] = consume(process, directory, "agent")
            report["agent_finished_at"] = now()
        process = sandbox.exec(*helper, "collect", "/tmp/task.json", timeout=30)
        if consume(process, directory, "collect") != 0:
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
        else:
            report["status"] = "checking"
            write_json(directory / "report.json", report)
            process = sandbox.exec(*helper, "check", "/tmp/task.json", timeout=240)
            report["check_exit"] = consume(process, directory, "check")
            if args.command == "smoke":
                report["status"] = (
                    "smoke_passed" if report["check_exit"] == 0 else "failed"
                )
            else:
                report["status"] = (
                    "validated"
                    if eligible_result(
                        report["agent_exit"], report["check_exit"], changes
                    )
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
                process = sandbox.exec(
                    "python3",
                    "/opt/swarm/modal_worker.py",
                    "collect",
                    "/tmp/task.json",
                    timeout=30,
                )
                if consume(process, directory, "recovery") != 0:
                    recovery_errors.append("collection_failed")
            except Exception as error:  # noqa: BLE001 - best-effort recovery before termination
                recovery_errors.append(type(error).__name__)
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
                sandbox.terminate()
                report["sandbox_terminated"] = True
            except Exception as error:  # noqa: BLE001 - preserve evidence of failed cleanup
                report["sandbox_terminated"] = False
                report["termination_error"] = type(error).__name__
        report.update(
            finished_at=now(), wall_seconds=round(time.monotonic() - started, 3)
        )
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
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--timeout", type=int, default=300, help="agent time limit in seconds, 30–1800; default 300"
    )
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
    project = load_project(args.project)
    configure(project)
    args.environment = args.environment or project.environment
    args.base = args.base or project.base_ref
    if args.command != "plan":
        project.require_live()
        project.verify_origin()
    if not 30 <= args.timeout <= 1800:
        parser.error("pilot timeout must be between 30 and 1800 seconds")
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
    plan = {
        "repository": project.repository,
        "base_sha": base_sha,
        "tasks": [task["id"] for task in tasks],
        "task_states": {task["id"]: task.get("status", "ready") for task in tasks},
        "live_execution_enabled": project.live_enabled,
        "gateway_concurrency": project.gateway_concurrency,
        "gateway_capacity_sufficient": min(args.workers, len(tasks)) <= project.gateway_concurrency,
        "allow_overlap": args.allow_overlap,
        "overlap_pairs": args.overlap_pairs,
        "requested_workers": args.workers,
        "maximum_active_agents": min(args.workers, len(tasks)),
        "enough_tasks_for_100": len(tasks) >= 100 and args.workers >= 100,
        "agent_timeout_seconds": args.timeout,
        "sandbox_lifecycle_timeout_seconds": args.timeout + 900,
        "gateway_run_ttl_seconds": run_ttl_seconds(args.timeout),
        "configured_provider_pacing": {
            "tokens_per_minute": project.gateway_tokens_per_minute,
            "requests_per_minute": project.gateway_requests_per_minute,
            "provider_quota_verified": False,
        },
        "mode": args.command,
    }
    if args.command == "plan":
        plan["local_admission"] = local_admission_summary(tasks, args.workers, project)
        print(json.dumps(plan, indent=2))
        return 0
    from .operations import OperationsJournal

    operations = OperationsJournal(common_state(ROOT) / "operations.sqlite3")
    operations.require_admission(project.failure_policy)
    import modal

    state = common_state(ROOT)
    with controller_lock(state):
        operations.require_admission(project.failure_policy)
        ledger = project.ledger(state / "control.sqlite3")
        if args.command == "run":
            verify_github_tasks(tasks, ROOT, base_sha, repository=project.repository, tasks_dir=project.tasks_dir)
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
