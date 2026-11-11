"""Loopback-only, read-only viewer for explicitly selected curated evidence."""

import argparse
import hashlib
import json
import math
import re
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


def make_server(snapshot_path=None, host="127.0.0.1", port=0, *, catalog_path=None):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("dashboard requires a loopback address")
    if (snapshot_path is None) == (catalog_path is None):
        raise ValueError("select exactly one curated snapshot or local catalog")
    catalog = None
    if catalog_path is not None:
        from .dashboard_catalog import load_catalog
        catalog = load_catalog(catalog_path)

    def public_catalog():
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
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

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
                    if catalog is None:
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
                self.respond(
                    200 if data["available"] else 503,
                    json.dumps(data, allow_nan=False).encode(),
                )
            elif parsed.path == "/api/catalog":
                self.respond(200, json.dumps(public_catalog(), allow_nan=False).encode())
            elif parsed.path == "/api/health":
                self.respond(200, b'{"ok":true,"read_only":true}')
            elif parsed.path == "/favicon.ico":
                self.respond(204, b"", "image/x-icon")
            elif parsed.path in {"/", "/assets/app.js", "/assets/styles.css"}:
                filename, kind = {
                    "/": ("index.html", "text/html"),
                    "/assets/app.js": ("app.js", "text/javascript"),
                    "/assets/styles.css": ("styles.css", "text/css"),
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
            self.respond(405, b'{"error":"Read only"}')

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--snapshot",
        type=Path,
        help="Explicit curated JSON evidence file; never a raw worker log or ledger",
    )
    source.add_argument("--catalog", type=Path, help="Explicit local project/run catalog; source paths remain private")
    parser.add_argument("--port", default=8787, type=int)
    args = parser.parse_args()
    server = make_server(args.snapshot, port=args.port, catalog_path=args.catalog)
    print(
        f"Swarm Control: http://127.0.0.1:{server.server_port} (read only)", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
