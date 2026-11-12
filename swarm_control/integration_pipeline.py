"""Bounded local preparation lanes; publish, CI retries and merges stay explicit."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import pilot_integrate as integration
from .dependencies import analyze_dependencies
from .integration_queue import plan_queue
from .pilot_state import common_state
from .project import load_project

MAX_ITEMS = 1000
MAX_METADATA_BYTES = 1024 * 1024
MAX_PATCH_BYTES = 32 * 1024 * 1024
SHA = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
TASK = re.compile(r"[A-Z][A-Z0-9_-]{1,39}")
_PROJECT_LOCK = threading.Lock()


def _require(value, message):
    if not value:
        raise ValueError(message)


def _now():
    return datetime.now(UTC).isoformat()


def _json(path):
    path = Path(path)
    _require(not path.is_symlink() and path.is_file()
             and path.stat().st_size <= MAX_METADATA_BYTES, "Invalid bounded local JSON input")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "Duplicate JSON key")
            value[key] = item
        return value

    def constant(_):
        raise ValueError("Non-finite JSON value")

    value = json.loads(path.read_bytes(), object_pairs_hook=unique, parse_constant=constant)
    _require(isinstance(value, dict), "Local JSON input must be an object")
    return value


def _save(path, value):
    raw = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _pipeline_lock(state):
    """One lane scheduler per shared checkout; candidates still prepare in parallel."""
    _require(not state.is_symlink(), "Unsafe integration state directory")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state / "integration-pipeline.lock"
    _require(not path.is_symlink(), "Unsafe integration pipeline lock")
    with path.open("a") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another integration preparation pipeline is active") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class PreparationJournal:
    """A reservation is never automatically retried, including after interruption."""

    def __init__(self, path):
        self.path = Path(path)
        _require(not self.path.is_symlink(), "Unsafe integration preparation journal")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS preparations (
                identity TEXT PRIMARY KEY, task_id TEXT NOT NULL, patch_sha256 TEXT NOT NULL,
                base_sha TEXT NOT NULL, artifacts TEXT NOT NULL, run_id TEXT NOT NULL,
                state TEXT NOT NULL, reserved_at_utc TEXT NOT NULL,
                finished_at_utc TEXT, tested_tree TEXT, error_type TEXT)""")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def identity(repository, candidate, base_sha):
        value = [repository, candidate["task"]["id"], candidate["report"]["base_sha"],
                 candidate["digest"], base_sha]
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()

    def reserve(self, repository, candidate, base_sha, run_id):
        identity = self.identity(repository, candidate, base_sha)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM preparations WHERE identity=?", (identity,)).fetchone()
            if previous is not None:
                return None, dict(previous)
            db.execute("INSERT INTO preparations VALUES (?,?,?,?,?,?,?, ?,NULL,NULL,NULL)",
                       (identity, candidate["task"]["id"], candidate["digest"], base_sha,
                        str(candidate["directory"]), run_id, "preparing", _now()))
        return identity, None

    def finish(self, identity, state, *, tested_tree=None, error_type=None):
        _require(state in {"prepared", "failed", "interrupted"}, "Invalid preparation outcome")
        with self.connect() as db:
            result = db.execute("""UPDATE preparations SET state=?,finished_at_utc=?,tested_tree=?,error_type=?
                                 WHERE identity=? AND state='preparing'""",
                                (state, _now(), tested_tree, error_type, identity))
            _require(result.rowcount == 1, "Preparation reservation already settled")

    def pending(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM preparations WHERE state IN ('preparing','prepared','interrupted')")]


def _load_candidates(manifest, manifest_dir):
    _require(isinstance(manifest, dict) and set(manifest) == {"schema_version", "items"}
             and type(manifest["schema_version"]) is int and manifest["schema_version"] == 1,
             "Pipeline manifest needs schema_version=1 and items")
    entries = manifest["items"]
    _require(isinstance(entries, list) and 1 <= len(entries) <= MAX_ITEMS,
             "Select between 1 and 1000 integration candidates")
    candidates, seen, metadata_bytes = [], set(), 0
    for entry in entries:
        _require(isinstance(entry, dict) and {"artifacts", "reviewed_patch_sha"} <= set(entry)
                 and not set(entry) - {"artifacts", "reviewed_patch_sha", "recovery_reviewed", "reviewed_applied_patch_sha"},
                 "Each candidate needs artifacts and reviewed_patch_sha")
        selected, reviewed = entry["artifacts"], entry["reviewed_patch_sha"]
        recovery = entry.get("recovery_reviewed", False)
        applied_review = entry.get("reviewed_applied_patch_sha")
        _require(isinstance(selected, str) and 0 < len(selected) <= 4096
                 and not any(ord(c) < 32 for c in selected)
                 and isinstance(reviewed, str) and SHA.fullmatch(reviewed)
                 and type(recovery) is bool
                 and (applied_review is None or isinstance(applied_review, str) and SHA.fullmatch(applied_review)),
                 "Invalid candidate path, review hash or recovery flag")
        directory = Path(selected).expanduser()
        if not directory.is_absolute():
            directory = manifest_dir / directory
        _require(not directory.is_symlink() and directory.is_dir(), "Candidate artifacts are unavailable")
        directory = directory.resolve()
        for name in ("manifest.json", "report.json"):
            _json(directory / name)
            metadata_bytes += (directory / name).stat().st_size
        _require(metadata_bytes <= 32 * 1024 * 1024, "Pipeline metadata exceeds 32 MiB")
        patch_path = directory / "changes.patch"
        _require(not patch_path.is_symlink() and patch_path.is_file()
                 and 0 < patch_path.stat().st_size <= MAX_PATCH_BYTES, "Candidate patch is unavailable or too large")
        original_hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                           for name in ("manifest.json", "report.json", "changes.patch")}
        task, report, digest = integration.artifacts(directory, recovery_reviewed=recovery)
        _require(original_hashes == {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                                    for name in original_hashes}, "Candidate changed during input inspection")
        _require(digest == reviewed, "Reviewed patch hash differs from candidate")
        _require(task["id"] not in seen, "Duplicate pipeline task ID")
        seen.add(task["id"])
        dependencies = task.get("dependsOn", [])
        _require(isinstance(dependencies, list) and len(dependencies) <= MAX_ITEMS
                 and all(isinstance(dep, str) and TASK.fullmatch(dep) and dep != task["id"] for dep in dependencies)
                 and len(set(dependencies)) == len(dependencies), "Invalid candidate dependencies")
        record = _json(directory / "integration.json") if (directory / "integration.json").exists() else None
        binding_error = False
        if record is not None:
            _require(isinstance(record, dict) and record.get("task_id") == task["id"]
                     and record.get("patch_sha256") == digest
                     and record.get("worker_base_sha") == report["base_sha"], "Existing integration binding differs")
            try:
                integration.check_source_bindings(directory, record, task, report)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                binding_error = True
        candidates.append({"directory": directory, "task": task, "report": report,
                           "digest": digest, "recovery_reviewed": recovery, "integration": record,
                           "source_binding_error": binding_error,
                           "reviewed_applied_patch_sha": applied_review, "source_sha256": original_hashes})
    return candidates


def _verified_dependency(candidate, base, project):
    """Fresh read-only merge/head/combined checks, plus explicit reviewed source bytes."""
    record = candidate["integration"]
    if not record or record.get("status") != "merged":
        return False
    if (record.get("application_method") == "three_way"
            and candidate["reviewed_applied_patch_sha"] != record.get("applied_patch_sha256")):
        return False
    number, head, commit = record.get("pr_number"), record.get("head_sha"), record.get("merge_commit_sha")
    if (type(number) is not int or number <= 0 or not isinstance(head, str) or not COMMIT.fullmatch(head)
            or not isinstance(commit, str) or not COMMIT.fullmatch(commit)
            or record.get("green_head_sha") != head
            or not isinstance(record.get("tested_tree"), str) or not COMMIT.fullmatch(record["tested_tree"])
            or not isinstance(record.get("applied_patch_sha256"), str)
            or not SHA.fullmatch(record["applied_patch_sha256"])):
        return False
    try:
        integration.check_source_bindings(candidate["directory"], record, candidate["task"], candidate["report"])
        if (integration.git(project.root, "rev-parse", f"{head}^{{tree}}") != record["tested_tree"]
                or integration.git(project.root, "rev-parse", f"{commit}^{{tree}}") != record["tested_tree"]
                or integration.git(project.root, "rev-parse", f"{head}^") != record["tested_base_sha"]):
            return False
        applied = integration.command(["git", "diff", "--binary", "--full-index", "--no-ext-diff",
            "--no-textconv", "--no-renames", record["tested_base_sha"], head, "--"], project.root)
        if hashlib.sha256(applied).hexdigest() != record["applied_patch_sha256"]:
            return False
        pr = integration.github(f"pulls/{number}")
        if (pr.get("merged") is not True or pr.get("merge_commit_sha") != commit
                or pr.get("head", {}).get("sha") != head
                or pr.get("base", {}).get("ref") != project.base_branch):
            return False
        integration.git(project.root, "merge-base", "--is-ancestor", commit, base)
        integration.green_checks(integration.all_checks(head), head, project.required_checks)
        integration.green_checks(integration.all_checks(commit), commit, project.required_checks)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False
    return True


def _queue(candidates, base, lanes, *, online=False, project=None):
    indexed = {candidate["task"]["id"]: candidate for candidate in candidates}
    items, qualified = [], set()
    for candidate in candidates:
        task, record = candidate["task"], candidate["integration"]
        stage = "ready"
        if record:
            stage = {"prepared": "awaiting_review", "committed": "awaiting_review",
                     "published": "waiting_ci", "merged": "merged", "checking": "preparing",
                     "preparing": "preparing"}.get(record.get("status"), "needs_repair")
            if record.get("tested_base_sha") != base and stage != "merged":
                stage = "needs_repair"
        row = {"task_id": task["id"], "stage": stage, "ownership": task["ownership"],
               "depends_on": task.get("dependsOn", [])}
        if candidate.get("source_binding_error"):
            row.update(stage="needs_repair", analysis_error_code="integration_source_binding_invalid")
            stage = "needs_repair"
        if stage == "waiting_ci":
            row["waiting_since_utc"] = record.get("published_at")
        if stage == "merged" and online and _verified_dependency(candidate, base, project):
            row.update(merge_commit_sha=record["merge_commit_sha"], independent_acceptance=True,
                       combined_ci_green=True)
            qualified.add(task["id"])
        items.append(row)
    analysis = analyze_dependencies([candidate["task"] for candidate in candidates],
                                    workers=lanes, completed_ids=qualified)
    rows = {row["task_id"]: row for row in items}
    for edge in analysis["edges"]:
        if edge["consumer"] in rows:
            row = rows[edge["consumer"]]
            row["depends_on"] = sorted(set(row["depends_on"]) | {edge["producer"]})
    for error in analysis["errors"]:
        for task_id in error.get("task_ids", []):
            if task_id in rows and task_id not in qualified:
                rows[task_id].update(stage="needs_repair", depends_on=[])
                rows[task_id].setdefault("analysis_error_code", error["code"])
    missing = {dep for row in items for dep in row["depends_on"] if dep not in indexed}
    for task_id in sorted(missing):
        # Missing artifacts are a conservative dependency hold, never a claim of
        # completion manufactured from a queue's arbitrary boolean metadata.
        items.append({"task_id": task_id, "stage": "needs_repair",
                      "ownership": [f"unavailable-dependency/{task_id}"]})
    _require(len(items) <= MAX_ITEMS, "Candidates and unresolved dependencies exceed 1000 items")
    source = {"base_sha": base, "lanes": lanes, "items": items}
    plan_queue(source)  # Validate IDs, ownership and the complete dependency graph.
    return source, analysis


def _public_result(base, lanes, max_pending, candidates, source, analysis, *, execute):
    plan = plan_queue(source)
    decisions = {row["task_id"]: row for row in plan["decisions"]}
    rows = {row["task_id"]: row for row in source["items"]}
    return {"schema_version": 1, "kind": "integration_preparation_pipeline",
            "base_sha": base, "lanes": lanes, "max_pending": max_pending,
            "execution_requested": execute, "authorizes_spend": False,
            "remote_mutations_performed": 0, "automatic_ci_retry": False,
            "automatic_worker_replacement": False, "revalidate_changed_base": True,
            "dependency_analysis": analysis,
            "items": [{"task_id": item["task"]["id"], "patch_sha256": item["digest"],
                       "state": "eligible" if decisions[item["task"]["id"]]["action"] == "prepare" else "held",
                       "reason": ("dependency_analysis:" + rows[item["task"]["id"]]["analysis_error_code"]
                                  if "analysis_error_code" in rows[item["task"]["id"]]
                                  else decisions[item["task"]["id"]]["reason"])} for item in candidates],
            "prepared_count": 0, "failed_count": 0, "accepted_deliveries": 0,
            "actual_cost_usd": None, "finished_at_utc": None}


def _pending_keys(journal, root, base, candidates):
    pending, uncertain = set(), set()
    for row in journal.pending():
        key = row["task_id"], row["patch_sha256"]
        try:
            record = _json(Path(row["artifacts"]) / "integration.json")
            commit = record.get("merge_commit_sha")
            if (record.get("status") == "merged" and record.get("patch_sha256") == row["patch_sha256"]
                    and record.get("tested_base_sha") == row["base_sha"]
                    and isinstance(commit, str) and COMMIT.fullmatch(commit)):
                integration.check_source_bindings(Path(row["artifacts"]), record)
                integration.git(root, "merge-base", "--is-ancestor", commit, base)
                continue
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            pass
        pending.add(key)
        if row["state"] in {"preparing", "interrupted"}:
            uncertain.add(key)
    for candidate in candidates:
        record = candidate["integration"]
        if record and record.get("status") in {"preparing", "checking", "interrupted", "prepared", "committed", "published"}:
            key = candidate["task"]["id"], candidate["digest"]
            pending.add(key)
            if record["status"] in {"preparing", "checking", "interrupted"}:
                uncertain.add(key)
    return pending, uncertain


def run_pipeline(project, manifest, *, execute=False, lanes=2, max_pending=None,
                 online=False, check_timeout=1200, manifest_dir=None, github_get_limit=200):
    """Plan or explicitly execute local preparation; every source remains immutable."""
    _require(_PROJECT_LOCK.acquire(blocking=False), "Another integration pipeline owns this process")
    saved = {key: getattr(integration, key) for key in ("ROOT", "REPO", "PROJECT", "BASE_BRANCH")}
    original_github = integration.github
    observed = {"requests": 0, "limit": github_get_limit, "limit_reached": False}
    cache = {}

    def query(path, **kwargs):
        _require(kwargs.get("method", "GET") == "GET" and "body" not in kwargs,
                 "Integration preparation only permits read-only GitHub requests")
        if path not in cache:
            if observed["requests"] >= github_get_limit:
                observed["limit_reached"] = True
                raise ValueError("Integration dependency GET budget exhausted")
            observed["requests"] += 1
            cache[path] = original_github(path, **kwargs)
        return cache[path]

    try:
        _require(type(github_get_limit) is int and 1 <= github_get_limit <= 10000,
                 "GitHub GET limit must be between 1 and 10000")
        integration.github = query
        return _run_pipeline(project, manifest, execute=execute, lanes=lanes, max_pending=max_pending,
                             online=online, check_timeout=check_timeout, manifest_dir=manifest_dir,
                             github_observation=observed)
    finally:
        integration.github = original_github
        for key, value in saved.items():
            setattr(integration, key, value)
        _PROJECT_LOCK.release()


def _run_pipeline(project, manifest, *, execute, lanes, max_pending,
                  online, check_timeout, manifest_dir, github_observation):
    _require(type(lanes) is int and 1 <= lanes <= 8, "Preparation lanes must be between 1 and 8")
    max_pending = lanes * 2 if max_pending is None else max_pending
    _require(type(max_pending) is int and lanes <= max_pending <= MAX_ITEMS,
             "Pending candidate bound must be between lane count and 1000")
    _require(type(check_timeout) is int and 1 <= check_timeout <= 3600,
             "Per-command check time must be between 1 and 3600 seconds")
    _require(type(execute) is bool and type(online) is bool, "Execution and online options must be boolean")
    project.verify_origin()
    integration.configure(project)
    candidates = _load_candidates(manifest, Path(manifest_dir or Path.cwd()).resolve())
    base = integration.git(project.root, "rev-parse", f"origin/{project.base_branch}")
    _require(COMMIT.fullmatch(base), "Current integration base is unavailable")
    source, analysis = _queue(candidates, base, lanes, online=online and not execute, project=project)
    result = _public_result(base, lanes, max_pending, candidates, source, analysis, execute=execute)
    result["read_only_github"] = github_observation
    if not execute:
        return result
    state = common_state(project.root)
    with _pipeline_lock(state):
        with integration.integration_lock(state, wait=True):
            integration.git(project.root, "fetch", "origin", f"{project.base_branch}:refs/remotes/origin/{project.base_branch}")
            base = integration.git(project.root, "rev-parse", f"origin/{project.base_branch}")
        source, analysis = _queue(candidates, base, lanes, online=online, project=project)
        result = _public_result(base, lanes, max_pending, candidates, source, analysis, execute=True)
        result["read_only_github"] = github_observation
        journal = PreparationJournal(state / "integration-pipeline.sqlite3")
        run_id = "prepare-" + uuid.uuid4().hex
        directory = state / "integration-pipelines" / run_id
        _require(not directory.parent.is_symlink(), "Unsafe integration pipeline evidence directory")
        directory.mkdir(mode=0o700, parents=True)
        path = directory / "receipt.json"
        result.update(run_id=run_id, started_at_utc=_now())
        _save(path, result)
        indexed = {candidate["task"]["id"]: candidate for candidate in candidates}
        rows = {row["task_id"]: row for row in result["items"]}
        source_rows = {row["task_id"]: row for row in source["items"]}
        pending_keys, uncertain_keys = _pending_keys(journal, project.root, base, candidates)
        pending_before = len(pending_keys)
        admitted, futures = 0, {}

        def prepare_one(candidate, identity):
            started = time.monotonic()
            try:
                # Re-read trusted hashes after reservation, immediately before
                # commands. A changed input leaves a failed, non-retryable attempt.
                task, report, digest = integration.artifacts(candidate["directory"],
                    recovery_reviewed=candidate["recovery_reviewed"])
                _require(digest == candidate["digest"] and task == candidate["task"]
                         and report == candidate["report"]
                         and candidate["source_sha256"] == {name: hashlib.sha256(
                             (candidate["directory"] / name).read_bytes()).hexdigest()
                             for name in candidate["source_sha256"]}, "Candidate changed after reservation")
                prepared = integration.prepare(candidate["directory"], task, report, digest,
                    expected_base=base, fetch_base=False, check_timeout=check_timeout,
                    recovery_reviewed=candidate["recovery_reviewed"])
                journal.finish(identity, "prepared", tested_tree=prepared["tested_tree"])
                return {"state": "prepared", "reason": "independent_current_base_checks_passed",
                        "tested_base_sha": base, "tested_tree": prepared["tested_tree"],
                        "applied_patch_sha256": prepared["applied_patch_sha256"],
                        "attribution": prepared["attribution"], "elapsed_seconds": time.monotonic() - started}
            except (Exception, SystemExit) as error:  # noqa: BLE001 - background terminal boundary preserves unknown failures.
                try:
                    failure = directory / f"{candidate['task']['id']}-failure.txt"
                    with failure.open("xb") as stream:
                        os.chmod(failure, 0o600)
                        stream.write(traceback.format_exc().encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                    journal.finish(identity, "failed", error_type=type(error).__name__)
                except (OSError, ValueError, sqlite3.Error):
                    return {"state": "uncertain", "reason": "preparation_evidence_unavailable",
                            "elapsed_seconds": time.monotonic() - started}
                return {"state": "failed", "reason": "local_preparation_failed",
                        "error_type": type(error).__name__, "elapsed_seconds": time.monotonic() - started}

        def submit_available(pool):
            nonlocal admitted
            while len(futures) < lanes and admitted < max_pending:
                choices = plan_queue(source)["prepare"]
                if not choices:
                    return
                task_id = choices[0]
                candidate = indexed[task_id]
                key = task_id, candidate["digest"]
                if key in uncertain_keys:
                    source_rows[task_id]["stage"] = "needs_repair"
                    rows[task_id].update(state="held", reason="prior_preparation_outcome_unknown")
                    continue
                if key not in pending_keys and len(pending_keys) >= max_pending:
                    source_rows[task_id]["stage"] = "needs_repair"
                    rows[task_id].update(state="held", reason="pending_candidate_bound_reached")
                    continue
                identity, previous = journal.reserve(project.repository, candidate, base, run_id)
                if previous:
                    source_rows[task_id]["stage"] = "needs_repair"
                    rows[task_id].update(state="held", reason="preparation_already_attempted",
                                         previous_state=previous["state"])
                    continue
                admitted += 1
                pending_keys.add(key)
                source_rows[task_id]["stage"] = "preparing"
                rows[task_id].update(state="preparing", reason="durable_preparation_reserved")
                _save(path, result)
                futures[pool.submit(prepare_one, candidate, identity)] = task_id

        try:
            with ThreadPoolExecutor(max_workers=lanes) as pool:
                submit_available(pool)
                while futures:
                    finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in finished:
                        task_id = futures.pop(future)
                        rows[task_id].update(future.result())
                        source_rows[task_id]["stage"] = "awaiting_review" if rows[task_id]["state"] == "prepared" else "needs_repair"
                        result["prepared_count"] += rows[task_id]["state"] == "prepared"
                        result["failed_count"] += rows[task_id]["state"] in {"failed", "uncertain"}
                        _save(path, result)
                    submit_available(pool)
            decisions = {row["task_id"]: row for row in plan_queue(source)["decisions"]}
            for row in result["items"]:
                decision = decisions[row["task_id"]]
                if (row["state"] in {"eligible", "held"}
                        and decision["reason"] in {"independent_candidate_on_current_base", "preparation_lanes_full"}):
                    row.update(state="held", reason="pending_candidate_bound_reached")
            result.update(finished_at_utc=_now(), status="finished", attempts_admitted=admitted,
                          pending_candidates_before=pending_before)
            _save(path, result)
        except BaseException:
            result.update(status="interrupted", finished_at_utc=_now())
            _save(path, result)
            raise
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--lanes", type=int, default=2)
    parser.add_argument("--max-pending", type=int)
    parser.add_argument("--check-timeout", type=int, default=1200)
    parser.add_argument("--online", action="store_true", help="Permit read-only GitHub checks for merged dependency candidates")
    parser.add_argument("--github-get-limit", type=int, default=200,
                        help="Shared bounded dependency-query budget, including failed GETs (default: 200)")
    parser.add_argument("--execute", action="store_true", help="Explicitly run configured local install/check commands; never deploy or merge")
    args = parser.parse_args()
    result = run_pipeline(load_project(args.project), _json(args.manifest), execute=args.execute,
        lanes=args.lanes, max_pending=args.max_pending, online=args.online,
        check_timeout=args.check_timeout, manifest_dir=args.manifest.resolve().parent,
        github_get_limit=args.github_get_limit)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if result["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
