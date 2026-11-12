"""Loopback evidence, guided setup and explicitly reviewed controller operations."""

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import traceback
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 8 * 1024 * 1024
STATIC = Path(__file__).parent / "dashboard_static"
HASH = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SENSITIVE = re.compile(
    r"(?:^|[^A-Za-z0-9])(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|Bearer\s)",
    re.IGNORECASE,
)
ORIGINAL = {
    "queued",
    "preparing",
    "ready",
    "working",
    "checking",
    "needs_repair",
    "validated",
    "merged",
    "failed",
    "unknown",
}
ATTRIBUTIONS = {
    "trusted_local_unchanged_failed_worker_recovery",
    "coordinator_code_or_test_repair",
}
FINANCES = (
    "authorized_new_usd",
    "admitted_new_usd",
    "estimated_model_committed_usd",
    "model_uncertain_retained_usd",
    "model_exposure_ceiling_usd",
    "actual_billed_total_usd",
    "remaining_model_admission_usd",
    "remaining_infra_admission_usd",
)


def _launch_error(error):
    """Map controller failures to fixed operator guidance, never raw exceptions."""
    text = str(error)
    for fragment, message in (
        ("live execution is disabled", "Live execution is disabled in this project's policy. Follow the real-run setup guide and obtain an authorized budget before enabling it."),
        ("Draft source or configuration changed", "The source or configuration changed after this draft was saved. Create and review a new draft."),
        ("already been attempted", "This run has already been attempted. Inspect its history and create a new draft for a separately reviewed attempt."),
        ("already reserved", "A launch was already reserved for this run. Inspect its history before taking another action."),
        ("source preparation is blocked", "Source preparation is blocked. Check the plan and resolve its reported blockers before saving a new draft."),
        ("admission is blocked", "Budget admission is blocked. Inspect the existing allowance and retained reservations; preserve the ledger."),
        ("explicit total model reservation", "This draft needs an explicit total model reservation. Save a new draft with a budget you are authorized to use."),
        ("operations", "An operations hold blocks this run. Inspect the controller's operations report before attempting a new run."),
    ):
        if fragment.lower() in text.lower():
            return message
    return "The controller could not launch this run. Inspect the local terminal and run history before creating a new draft. No automatic retry was made."


class BrowserLaunchValidation(ValueError):
    """Fixed validation guidance which is safe to return over HTTP."""


class BrowserLaunchBusy(RuntimeError):
    pass


class BrowserLaunch:
    """One bounded background launch; the ordinary durable runner owns execution.

    The thread is deliberately non-daemon. Closing the HTTP server waits for it
    so a closed tab or Ctrl+C cannot silently abandon a running subprocess.
    """

    def __init__(self, workspace):
        self.workspace = workspace
        self.lock = threading.Lock()
        self.thread = None
        self.observations = {}

    def observation(self, project_id, run_id):
        from .dashboard_catalog import identity
        identity(project_id)
        identity(run_id)
        with self.lock:
            return dict(self.observations.get((project_id, run_id), {
                "project_id": project_id, "run_id": run_id, "status": "idle",
                "error": None, "runner_exit_code": None}))

    @staticmethod
    def _write_private(path, value):
        raw = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _audit(self, project_id, run_id, gateway_url):
        root = self.workspace.path.with_name(self.workspace.path.stem + ".launch-attempts")
        if root.is_symlink():
            raise OSError("Unsafe browser launch evidence directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not root.is_dir():
            raise OSError("Unsafe browser launch evidence directory")
        os.chmod(root, 0o700)
        attempt_id = secrets.token_hex(16)
        directory = root / attempt_id
        directory.mkdir(mode=0o700)
        self._write_private(directory / "request.json", {
            "schema_version": 1, "attempt_id": attempt_id,
            "stage": "explicit_browser_launch_request",
            "requested_at_utc": datetime.now(UTC).isoformat(),
            "project_id": project_id, "run_id": run_id, "gateway_origin": gateway_url,
            "confirmed": True, "cost_usd": None,
            "cost_reason": "Provider billing is not measured by a browser launch request",
            "authority": "The workspace runner's durable attempt and target ledger remain authoritative"})
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return directory

    def start(self, project_id, run_id, gateway_url, confirmed):
        if confirmed is not True:
            raise BrowserLaunchValidation("Confirm the reviewed run and authorized cost before launching.")
        if not isinstance(gateway_url, str) or not re.fullmatch(r"https://[A-Za-z0-9-]+\.modal\.run/?", gateway_url):
            raise BrowserLaunchValidation("Enter the verified deployed gateway HTTPS origin ending in .modal.run.")
        detail = self.workspace.run_detail(project_id, run_id)
        if detail["status"] != "draft":
            raise BrowserLaunchValidation("This run has already been attempted. Inspect its history and create a new draft.")
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise BrowserLaunchBusy("A run is already launching on this dashboard. Wait for its recorded outcome.")
            # Fail closed before dispatch if the private request cannot be kept.
            audit = self._audit(project_id, run_id, gateway_url)
            key = (project_id, run_id)
            if len(self.observations) >= 1000:
                del self.observations[next(iter(self.observations))]
            self.observations[key] = {"project_id": project_id, "run_id": run_id,
                "status": "starting", "error": None, "runner_exit_code": None}
            self.thread = threading.Thread(target=self._run, args=(key, gateway_url, audit, time.monotonic_ns()),
                                           name="swarm-browser-launch", daemon=False)
            self.thread.start()
            return dict(self.observations[key])

    def _run(self, key, gateway_url, audit, started_ns):
        with self.lock:
            self.observations[key]["status"] = "running"
        try:
            result = self.workspace.launch(*key, gateway_url=gateway_url, execute=True)
            exit_code = result.get("runner_exit_code")
            with self.lock:
                self.observations[key].update(status="finished" if exit_code == 0 else "failed",
                    runner_exit_code=exit_code,
                    error=None if exit_code == 0 else "The runner stopped with a failure. Inspect the saved run and local terminal before creating a new draft.")
        # This is the background task's terminal boundary: unexpected failures
        # must also get a private original and an explicit failed UI observation.
        except (Exception, SystemExit) as error:  # noqa: BLE001
            raw_traceback = traceback.format_exc().encode()
            try:
                with (audit / "failure.txt").open("xb") as stream:
                    os.chmod(audit / "failure.txt", 0o600)
                    stream.write(raw_traceback)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                raw_traceback = None
            with self.lock:
                self.observations[key].update(status="failed", error=_launch_error(error),
                    failure_evidence_recorded=raw_traceback is not None)
        finally:
            with self.lock:
                outcome = dict(self.observations[key])
            outcome.update(finished_at_utc=datetime.now(UTC).isoformat(),
                           elapsed_seconds=(time.monotonic_ns() - started_ns) / 1e9,
                           cost_usd=None, automatic_retries=0)
            failure_file = audit / "failure.txt"
            try:
                if failure_file.is_file():
                    outcome["failure_sha256"] = hashlib.sha256(failure_file.read_bytes()).hexdigest()
                self._write_private(audit / "outcome.json", outcome)
                if outcome.get("failure_evidence_recorded") is False:
                    raise OSError("Original launch failure evidence could not be retained")
                with self.lock:
                    self.observations[key]["evidence_status"] = "recorded"
            except OSError:
                with self.lock:
                    self.observations[key].update(evidence_status="unavailable",
                        error="The launch outcome could not be saved. Inspect run history and the local terminal before taking any further action. No automatic retry was made.")
                print("Browser launch evidence could not be saved. Preserve run history and investigate before retrying.", flush=True)

    def wait(self):
        thread = self.thread
        if thread is not None and thread.is_alive():
            print("Waiting for the active run to finish and preserve its outcome. Keep this terminal open.", flush=True)
            thread.join()


class DashboardServer(ThreadingHTTPServer):
    def server_close(self):
        super().server_close()
        launch = getattr(self, "browser_launch", None)
        if launch is not None:
            launch.wait()
        setup = getattr(self, "onboarding", None)
        if setup is not None:
            setup.wait()


def token(value, default=None):
    return (
        value
        if isinstance(value, str)
        and TOKEN.fullmatch(value)
        and not SENSITIVE.search(value)
        else default
    )


def digest(value, pattern=HASH):
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def number(value):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and value >= 0
        else None
    )


def timestamp(value):
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (
        parsed.astimezone(UTC).isoformat() if parsed.utcoffset() is not None else None
    )


def github_url(value, repository):
    if not isinstance(value, str) or len(value) > 500:
        return None
    try:
        path = urlsplit(value).path
    except ValueError:
        return None
    if any(part in {".", ".."} for part in path.split("/")):
        return None
    suffix = (
        re.escape(repository)
        + r"/(?:pull/\d+|issues/\d+(?:#issuecomment-\d+)?|commit/[a-f0-9]{40}/checks|actions/runs/\d+(?:/job/\d+)?|blob/[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)"
    )
    return value if re.fullmatch(r"https://github\.com/" + suffix, value) else None


def dictionary(value):
    return value if isinstance(value, dict) else {}


def title(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 240
        or any(ord(char) < 32 for char in value)
        or SENSITIVE.search(value)
        or re.search(r"/(?:home|Users|tmp)/", value)
    ):
        return None
    return value.strip()


def unavailable(now):
    return {
        "schema_version": 1,
        "available": False,
        "observed_at": now.isoformat(),
        "phase": "unavailable",
        "error": "Snapshot unavailable",
    }


def reject_constant(_value):
    raise ValueError("nonfinite JSON")


def project_snapshot(source, source_hash, now):
    """Project only public presentation fields; never forward an input object."""
    repository = source.get("repository")
    rows = source.get("rows")
    as_of = timestamp(source.get("as_of_utc"))
    if (
        not isinstance(repository, str)
        or not REPOSITORY.fullmatch(repository)
        or len(repository) > 200
        or any(part in {".", ".."} for part in repository.split("/"))
        or SENSITIVE.search(repository)
        or token(source.get("run_id")) is None
        or not isinstance(rows, list)
        or not rows
        or len(rows) > 1000
        or not as_of
    ):
        raise ValueError("unsupported curated evidence")
    age = (now - datetime.fromisoformat(as_of)).total_seconds()
    if age < 0:
        raise ValueError("future snapshot")
    clock = dictionary(source.get("clock"))
    t0 = timestamp(clock.get("T0_utc"))
    t1 = timestamp(clock.get("T1_utc"))
    if not t0 or datetime.fromisoformat(t0) > datetime.fromisoformat(as_of):
        raise ValueError("invalid release clock")
    finished = clock.get("full_delivery_finished") is True
    if finished and (
        not t1
        or not datetime.fromisoformat(t0)
        <= datetime.fromisoformat(t1)
        <= datetime.fromisoformat(as_of)
    ):
        raise ValueError("invalid completed clock")
    if not finished:
        t1 = None

    def occurred(value):
        at = timestamp(value)
        return (
            at
            if at
            and datetime.fromisoformat(t0)
            <= datetime.fromisoformat(at)
            <= datetime.fromisoformat(as_of)
            else None
        )

    tasks, events, ids = (
        [],
        [
            {
                "at": t0,
                "kind": "release",
                "task_id": None,
                "label": "Backlog released",
                "status": "released",
                "pr_url": None,
            }
        ],
        set(),
    )
    for raw in rows:
        row = dictionary(raw)
        task_id = token(row.get("task_id"))
        if not task_id or task_id in ids:
            raise ValueError("invalid task inventory")
        ids.add(task_id)
        recovery = dictionary(row.get("recovery"))
        bindings = dictionary(row.get("binding_checks"))
        qualified = (
            row.get("provisional_qualified") is True
            and bool(bindings)
            and all(value is True for value in bindings.values())
        )
        original = row.get("original_status")
        original = (
            original
            if isinstance(original, str) and original in ORIGINAL
            else "unknown"
        )
        attribution = recovery.get("attribution")
        attribution = attribution if attribution in ATTRIBUTIONS else None
        issue = row.get("issue")
        delivery = {
            "pr_url": github_url(recovery.get("pr_url"), repository),
            "head_sha": digest(recovery.get("reviewed_head_sha"), COMMIT),
            "merge_sha": digest(recovery.get("merge_commit_sha"), COMMIT),
            "merged_at": occurred(recovery.get("merged_at_utc")),
            "green_at": occurred(recovery.get("combined_green_observed_at_utc")),
            "quality_until": timestamp(recovery.get("quality_window_end_utc")),
            "head_checks_url": github_url(recovery.get("head_checks_url"), repository),
            "combined_checks_url": github_url(
                recovery.get("combined_checks_url"), repository
            ),
        }
        qualified = bool(
            qualified
            and row.get("queue_status") == "merged_green_provisional"
            and attribution is not None
            and all(
                delivery[key]
                for key in ("pr_url", "head_sha", "merge_sha", "merged_at", "green_at")
            )
            and datetime.fromisoformat(delivery["merged_at"])
            <= datetime.fromisoformat(delivery["green_at"])
        )
        task = {
            "id": task_id,
            "title": title(
                dictionary(source.get("dashboard_task_titles")).get(task_id)
            ),
            "issue_url": f"https://github.com/{repository}/issues/{issue}"
            if type(issue) is int and issue > 0
            else None,
            "original_status": original,
            "agent_exit": row.get("original_agent_exit")
            if type(row.get("original_agent_exit")) is int
            else None,
            "check_exit": row.get("original_helper_check_exit")
            if type(row.get("original_helper_check_exit")) is int
            else None,
            "empty_patch": row.get("original_patch_sha256")
            == hashlib.sha256(b"").hexdigest(),
            "queue_status": token(row.get("queue_status"), "not_recorded"),
            "provisional": qualified,
            "feature_candidate": row.get("workload_counts_as_feature_only_not_delivery")
            is True,
            "attribution": attribution,
            "review": {
                "reviewer": token(recovery.get("reviewer")),
                "started_at": occurred(recovery.get("review_started_at_utc")),
                "finished_at": occurred(recovery.get("review_finished_at_utc")),
                "duration_seconds": number(recovery.get("review_duration_seconds")),
            },
            "delivery": delivery,
            "hashes": {
                "original_patch": digest(row.get("original_patch_sha256")),
                "reviewed_patch": digest(recovery.get("reviewed_patch_sha256")),
                "review_receipt": digest(recovery.get("review_receipt_sha256")),
                "coordinator_delta": digest(recovery.get("coordinator_delta_sha256")),
            },
        }
        tasks.append(task)
        for at, kind, label in (
            (task["review"]["finished_at"], "review", "Independent review"),
            (delivery["merged_at"], "merge", "PR merged"),
            (delivery["green_at"], "green", "Main CI observed green"),
        ):
            if at:
                events.append(
                    {
                        "at": at,
                        "kind": kind,
                        "task_id": task_id,
                        "label": label,
                        "status": task["queue_status"],
                        "pr_url": delivery["pr_url"],
                    }
                )
    end = datetime.fromisoformat(t1 if finished else as_of)
    elapsed = max(0, (end - datetime.fromisoformat(t0)).total_seconds()) if t0 else None
    paused = dictionary(source.get("queue")).get("paused") is True
    cutoff_at = timestamp(clock.get("delivery_cutoff_utc"))
    window_closed = bool(not finished and cutoff_at and t0
                         and datetime.fromisoformat(t0) <= datetime.fromisoformat(cutoff_at)
                         <= datetime.fromisoformat(as_of))
    provisional = [task for task in tasks if task["provisional"]]
    financials = dictionary(source.get("dashboard_financials"))
    money = {key: number(financials.get(key)) for key in FINANCES}
    metrics = dictionary(source.get("final_metrics"))
    # A zero estimate or an unconfirmed invoice must never become billed cost.
    if metrics.get("billing_reconciled") is not True:
        money["actual_billed_total_usd"] = None
    money.update(
        source_observed_at=timestamp(financials.get("source_observed_at")),
        source_kind=token(financials.get("source_kind")),
    )
    links = dictionary(source.get("dashboard_links"))
    active = dictionary(source.get("dashboard_activity")).get("active_workers")
    activity_at = timestamp(
        dictionary(source.get("dashboard_activity")).get("observed_at")
    )
    if (
        type(active) is not int
        or not 0 <= active <= len(tasks)
        or not activity_at
        or datetime.fromisoformat(activity_at) > datetime.fromisoformat(as_of)
    ):
        active = None
    validation = dictionary(source.get("local_validation_summary"))
    return {
        "schema_version": 1,
        "available": True,
        "observed_at": now.isoformat(),
        "source_as_of": as_of,
        "stale": age > 120,
        "repository": repository,
        "run_id": source["run_id"],
        "phase": "complete" if finished else "window_closed" if window_closed else "paused" if paused else "integration",
        "clock": {
            "t0": t0,
            "t1": t1,
            "cutoff": timestamp(clock.get("delivery_cutoff_utc")),
            "elapsed_seconds": elapsed,
            "quality_window_seconds": number(
                clock.get("quality_window_seconds_after_latest_actual_merge")
            ),
        },
        "summary": {
            "assigned": len(tasks),
            "original_validated": sum(
                task["original_status"] == "validated" for task in tasks
            ),
            "original_needs_repair": sum(
                task["original_status"] == "needs_repair" for task in tasks
            ),
            "active_workers": active,
            "provisional_tasks": len(provisional),
            "provisional_product": sum(
                task["feature_candidate"] for task in provisional
            ),
            "provisional_support": sum(
                not task["feature_candidate"] for task in provisional
            ),
            "unchanged_recoveries": sum(
                task["attribution"] == "trusted_local_unchanged_failed_worker_recovery"
                for task in provisional
            ),
            "coordinator_repaired": sum(
                task["attribution"] == "coordinator_code_or_test_repair"
                for task in provisional
            ),
            "empty_patches": sum(task["empty_patch"] for task in tasks),
            "product_candidates": sum(task["feature_candidate"] for task in tasks),
            "support_candidates": sum(not task["feature_candidate"] for task in tasks),
        },
        "tasks": tasks,
        "financials": money,
        "timeline": sorted(events, key=lambda event: event["at"], reverse=True),
        "evidence": {
            "report_url": github_url(links.get("report_url"), repository),
            "tracking_url": github_url(links.get("tracking_url"), repository),
            "controller_commit": digest(source.get("controller_commit"), COMMIT),
            "worker_base_sha": digest(source.get("worker_base_sha"), COMMIT),
            "source_sha256": source_hash,
            "afk_proven": metrics.get("AFK_efficiency_win_proven") is True,
            "billing_reconciled": metrics.get("billing_reconciled") is True,
            "quality_accepted": None,
        },
        "validation": {
            "attempts": number(validation.get("attempt_count")),
            "failed": number(validation.get("failed_attempts")),
        },
    }


def load_snapshot(path, now=None):
    now = now or datetime.now(UTC)
    try:
        path = Path(path)
        if path.is_symlink():
            raise ValueError("symlink input")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversize input")
        source = json.loads(raw, parse_constant=reject_constant)
        if not isinstance(source, dict):
            raise TypeError("unsupported input")
        return project_snapshot(source, hashlib.sha256(raw).hexdigest(), now)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return unavailable(now)


def make_server(snapshot_path=None, host="127.0.0.1", port=0, *, catalog_path=None,
                workspace_path=None):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("dashboard requires a loopback address")
    if sum(value is not None for value in (snapshot_path, catalog_path, workspace_path)) != 1:
        raise ValueError("select exactly one curated snapshot, catalog or workspace")
    workspace = None
    csrf_token = secrets.token_urlsafe(32)
    if workspace_path is not None:
        from .workspace import Workspace
        workspace = Workspace(workspace_path)
    browser_launch = BrowserLaunch(workspace) if workspace is not None else None
    if workspace is not None:
        from .onboarding import Onboarding
        onboarding = Onboarding(workspace)
    else:
        onboarding = None
    activity_lock = threading.Lock()
    catalog = None
    if catalog_path is not None:
        from .dashboard_catalog import load_catalog
        catalog = load_catalog(catalog_path)

    def public_catalog():
        if workspace is not None:
            return workspace.public_catalog()
        if catalog is not None:
            return catalog.public()
        source = load_snapshot(snapshot_path)
        repository = source.get("repository", "")
        return {"workspace_label": "Local workspace", "projects": [
            {"id": "default", "label": repository or "Selected project", "repository": repository,
             "runs": [{"id": "current", "label": source.get("run_id") or "Current snapshot"}]}],
            "default_project_id": "default", "default_run_id": "current"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def respond(self, status, body, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            )
            try:
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Switching repositories aborts superseded fetches. A closed
                # browser connection must not trigger a second HTTP response.
                self.close_connection = True

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                parsed = urlsplit(self.path)
            except ValueError:
                self.respond(400, b'{"error":"Invalid request"}')
                return
            if parsed.scheme or parsed.netloc:
                self.respond(400, b'{"error":"Invalid request"}')
                return
            if len(self.path) > 8192:
                self.respond(414, b'{"error":"Request too long"}')
                return
            root_navigation = (
                parsed.path == "/"
                and self.headers.get("Sec-Fetch-Mode") == "navigate"
                and self.headers.get("Sec-Fetch-Dest") == "document"
            )
            authorities = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            hosts, origins = (
                self.headers.get_all("Host", []),
                self.headers.get_all("Origin", []),
            )
            if (
                len(hosts) != 1
                or hosts[0] not in authorities
                or len(origins) > 1
                or origins
                and origins[0] not in {f"http://{value}" for value in authorities}
                or (
                    self.headers.get("Sec-Fetch-Site") == "cross-site"
                    and not root_navigation
                )
            ):
                self.respond(403, b'{"error":"Forbidden"}')
                return
            if parsed.path == "/api/snapshot":
                try:
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                    if set(query) - {"project", "run"} or any(len(v) != 1 or not v[0] for v in query.values()):
                        raise ValueError("invalid selection")
                    project_id, run_id = query.get("project", [None])[0], query.get("run", [None])[0]
                    if workspace is not None:
                        selected, repository, project_id, run_id = workspace.historical_snapshot(project_id, run_id)
                    elif catalog is None:
                        if project_id not in {None, "default"} or run_id not in {None, "current"}:
                            raise ValueError("unknown selection")
                        selected, repository, project_id, run_id = snapshot_path, None, "default", "current"
                    else:
                        selected, repository, project_id, run_id = catalog.resolve(project_id, run_id)
                    data = load_snapshot(selected) if selected is not None else unavailable(datetime.now(UTC))
                    if repository and data.get("available") and data.get("repository") != repository:
                        data = unavailable(datetime.now(UTC))
                    data.update(selected_project_id=project_id, selected_run_id=run_id)
                except ValueError:
                    self.respond(400, b'{"error":"Invalid project or run selection"}')
                    return
                except (OSError, sqlite3.Error, subprocess.SubprocessError):
                    self.respond(503, b'{"error":"Selected run evidence is unavailable"}')
                    return
                self.respond(
                    200 if data["available"] else 503,
                    json.dumps(data, allow_nan=False).encode(),
                )
            elif parsed.path in {"/api/catalog", "/api/project", "/api/runs", "/api/run", "/api/launch"}:
                try:
                    if parsed.path == "/api/catalog":
                        if parsed.query:
                            raise ValueError("unexpected catalog query")
                        data = public_catalog()
                    elif workspace is None:
                        self.respond(404, b'{"error":"Workspace preparation is not enabled"}')
                        return
                    else:
                        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True,
                                         max_num_fields=2)
                        expected = {"project", "run"} if parsed.path in {"/api/run", "/api/launch"} else {"project"}
                        if set(query) != expected or any(len(v) != 1 or not v[0] for v in query.values()):
                            raise ValueError("invalid selection")
                        project_id = query["project"][0]
                        if parsed.path == "/api/project":
                            data = workspace.project_context(project_id)
                        elif parsed.path == "/api/run":
                            data = workspace.run_detail(project_id, query["run"][0])
                        elif parsed.path == "/api/launch":
                            workspace.run_detail(project_id, query["run"][0])
                            data = browser_launch.observation(project_id, query["run"][0])
                        else:
                            project = next((p for p in public_catalog()["projects"] if p["id"] == project_id), None)
                            if project is None:
                                raise ValueError("unknown project")
                            data = {"runs": project["runs"]}
                    self.respond(200, json.dumps(data, allow_nan=False).encode())
                except (ValueError, TypeError):
                    self.respond(400, b'{"error":"Invalid workspace selection"}')
                except (OSError, sqlite3.Error, subprocess.SubprocessError):
                    self.respond(503, b'{"error":"Workspace data unavailable"}')
            elif parsed.path in {"/api/setup", "/api/setup/job", "/api/setup/project"}:
                if onboarding is None:
                    self.respond(404, b'{"error":"Setup requires a local workspace"}')
                    return
                try:
                    if parsed.path == "/api/setup":
                        if parsed.query:
                            raise ValueError("Unexpected query")
                        data = onboarding.status()
                    elif parsed.path == "/api/setup/project":
                        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
                        if set(query) != {"project"} or len(query["project"]) != 1 or not query["project"][0]:
                            raise ValueError("Invalid project identity")
                        data = onboarding.selected_repository(query["project"][0])
                    else:
                        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
                        if set(query) != {"id"} or len(query["id"]) != 1:
                            raise ValueError("Invalid operation identity")
                        data = {"job": onboarding.job(query["id"][0])}
                    self.respond(200, json.dumps(data, allow_nan=False).encode())
                except (ValueError, OSError, TypeError, RecursionError):
                    self.respond(400, b'{"error":"Setup data is unavailable. Check the selected local workspace."}')
            elif parsed.path == "/api/session":
                data = {"mode": "workspace" if workspace is not None else "viewer",
                        "csrf_token": csrf_token if workspace is not None else None,
                        "read_only": workspace is None, "deployment_enabled": workspace is not None,
                        "launch_enabled": workspace is not None, "setup_enabled": workspace is not None}
                self.respond(200, json.dumps(data).encode())
            elif parsed.path == "/api/health":
                self.respond(200, json.dumps({"ok": True, "read_only": workspace is None,
                                             **({"deployment_enabled": True} if workspace is not None else {})}).encode())
            elif parsed.path == "/favicon.ico":
                self.respond(204, b"", "image/x-icon")
            elif parsed.path in {"/", "/assets/app.js", "/assets/styles.css", "/assets/setup.js", "/assets/setup.css",
                                 "/assets/navigation.js", "/assets/navigation.css", "/assets/planner.js", "/assets/planner.css"}:
                filename, kind = {
                    "/": ("index.html", "text/html"),
                    "/assets/app.js": ("app.js", "text/javascript"),
                    "/assets/styles.css": ("styles.css", "text/css"),
                    "/assets/setup.js": ("setup.js", "text/javascript"),
                    "/assets/setup.css": ("setup.css", "text/css"),
                    "/assets/navigation.js": ("navigation.js", "text/javascript"),
                    "/assets/navigation.css": ("navigation.css", "text/css"),
                    "/assets/planner.js": ("planner.js", "text/javascript"),
                    "/assets/planner.css": ("planner.css", "text/css"),
                }[parsed.path]
                try:
                    body = (STATIC / filename).read_bytes()
                except OSError:
                    self.respond(503, b'{"error":"Assets unavailable"}')
                    return
                self.respond(200, body, kind + "; charset=utf-8")
            else:
                self.respond(404, b'{"error":"Not found"}')

        def do_POST(self):
            if workspace is None or self.command != "POST":
                self.respond(405, b'{"error":"Read only"}')
                return
            hosts, origins = self.headers.get_all("Host", []), self.headers.get_all("Origin", [])
            tokens = self.headers.get_all("X-Swarm-CSRF", [])
            authorities = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if (len(hosts) != 1 or hosts[0] not in authorities or len(origins) != 1
                    or origins[0] != f"http://{hosts[0]}"
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or len(tokens) != 1 or not tokens[0].isascii()
                    or not hmac.compare_digest(tokens[0], csrf_token)):
                self.respond(403, b'{"error":"Forbidden"}')
                return
            operation = None
            try:
                parsed = urlsplit(self.path)
                operation = parsed.path
                if (parsed.scheme or parsed.netloc or parsed.query
                        or parsed.path not in {"/api/runs", "/api/runs/preview", "/api/projects", "/api/runs/launch", "/api/setup/action"}):
                    self.respond(404, b'{"error":"Not found"}')
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if (len(lengths) != 1 or not lengths[0].isdigit()
                        or not 0 < int(lengths[0]) <= 65536
                        or self.headers.get_all("Transfer-Encoding", [])
                        or self.headers.get_content_type() != "application/json"):
                    self.respond(400, b'{"error":"A bounded JSON request is required"}')
                    return
                self.connection.settimeout(5)

                def unique(pairs):
                    value = {}
                    for key, item in pairs:
                        if key in value:
                            raise ValueError("duplicate JSON key")
                        value[key] = item
                    return value

                def reject_constant(_value):
                    raise ValueError("invalid JSON constant")

                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    raise ValueError("incomplete JSON body")
                data = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
                if parsed.path == "/api/setup/action":
                    from .onboarding import SetupError
                    if not isinstance(data, dict) or set(data) != {"action", "options", "confirmed", "request_id"}:
                        raise ValueError("Invalid setup action fields")
                    with activity_lock:
                        if browser_launch.thread is not None and browser_launch.thread.is_alive():
                            raise SetupError("setup_busy", "An agent run is active. Wait for its outcome before changing setup.")
                        result = onboarding.start(data["action"], data["options"], data["confirmed"], data["request_id"])
                    self.respond(202, json.dumps({"job": result}, allow_nan=False).encode())
                    return
                if parsed.path == "/api/projects":
                    if (not isinstance(data, dict) or "path" not in data
                            or set(data) - {"path", "label"}):
                        raise ValueError("invalid connection fields")
                    result = workspace.connect_project(data["path"], label_text=data.get("label"))
                    self.respond(200, json.dumps(result, allow_nan=False).encode())
                    return
                if parsed.path == "/api/runs/launch":
                    if not isinstance(data, dict) or set(data) != {"project_id", "run_id", "gateway_url", "confirmed"}:
                        raise ValueError("invalid launch fields")
                    with activity_lock:
                        if onboarding.busy:
                            raise BrowserLaunchBusy("Setup operation in progress")
                        result = browser_launch.start(data["project_id"], data["run_id"],
                                                      data["gateway_url"], data["confirmed"])
                    self.respond(202, json.dumps(result, allow_nan=False).encode())
                    return
                if not isinstance(data, dict) or set(data) != {"project_id", "options"}:
                    raise ValueError("invalid request fields")
                result = workspace.prepare_run(data["project_id"], data["options"],
                                               save=parsed.path == "/api/runs")
                self.respond(201 if parsed.path == "/api/runs" else 200,
                             json.dumps(result, allow_nan=False).encode())
            except BrowserLaunchValidation as error:
                self.respond(400, json.dumps({"error": str(error), "code": "invalid_launch"}).encode())
            except BrowserLaunchBusy:
                self.respond(409, b'{"error":"A run is already launching on this dashboard. Wait for its recorded outcome.","code":"launch_busy"}')
            except (ValueError, TypeError, UnicodeError, RecursionError) as error:
                from .onboarding import SetupError
                from .workspace import ProjectConnectionError
                if operation == "/api/setup/action":
                    if isinstance(error, SetupError):
                        self.respond(409 if error.code == "setup_busy" else 400,
                                     json.dumps({"error": str(error), "code": error.code}).encode())
                    else:
                        self.respond(400, b'{"error":"Review the setup action and fields.","code":"invalid_setup"}')
                    return
                if isinstance(error, ProjectConnectionError):
                    self.respond(400, json.dumps({"error": str(error), "code": error.code}).encode())
                    return
                if operation == "/api/projects":
                    self.respond(400, b'{"error":"Choose a local repository folder or swarm.toml file and an optional display name.","code":"invalid_connection"}')
                    return
                if operation == "/api/runs/launch":
                    self.respond(400, b'{"error":"Choose an existing saved draft, a verified gateway origin and explicitly confirm the launch.","code":"invalid_launch"}')
                    return
                self.respond(400, b'{"error":"Invalid run configuration. Choose registered projects, existing tasks and an allowed agent count."}')
            except (OSError, sqlite3.Error, subprocess.SubprocessError):
                if operation == "/api/setup/action":
                    self.respond(503, b'{"error":"Could not preserve this setup request. Check local storage before trying again.","code":"setup_evidence_unavailable"}')
                    return
                if operation == "/api/runs/launch":
                    self.respond(503, b'{"error":"Could not preserve the private launch request. No workers were launched. Check local storage and permissions before trying again.","code":"launch_evidence_unavailable"}')
                    return
                if operation == "/api/projects":
                    self.respond(503, b'{"error":"Could not connect this local repository. Check that the folder and workspace are readable and writable, then try again.","code":"connection_unavailable"}')
                    return
                self.respond(503, b'{"error":"Could not prepare this local run. No workers were launched."}')

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST

    server = DashboardServer(("127.0.0.1", port), Handler)
    server.browser_launch = browser_launch
    server.onboarding = onboarding
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--snapshot",
        type=Path,
        help="Explicit curated JSON evidence file; never a raw worker log or ledger",
    )
    source.add_argument("--catalog", type=Path, help="Explicit local project/run catalog; source paths remain private")
    source.add_argument("--workspace", type=Path, nargs="?",
                        const=Path.home() / ".config/swarm-control/workspace.json",
                        help="Local registry, run preparation and explicitly confirmed launches")
    parser.add_argument("--port", default=8787, type=int)
    args = parser.parse_args()
    server = make_server(args.snapshot, port=args.port, catalog_path=args.catalog,
                         workspace_path=args.workspace)
    print(
        f"Swarm Control: http://127.0.0.1:{server.server_port} "
        f"({'local workspace; launches require confirmation' if args.workspace is not None else 'read only'}; deployment disabled)", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
