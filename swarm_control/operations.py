"""Durable admission brakes and single-host action reservations, separate from money."""

import argparse
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from .health import (
    FailurePolicy,
    classify_worker_result,
    failure_decision,
    scaling_advice,
)


def utc(value=None):
    value = value or datetime.now(UTC)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("an aware observation time is required")
    return value.astimezone(UTC)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise ValueError("invalid operations identity")
    return value


class OperationsJournal:
    """Append observations/control events; atomically reserve before a remote action.

    Constructing or reading this object creates no files. Clearing a brake never
    changes budget accounting or gives permission to launch or repeat an action.
    """

    def __init__(self, path):
        self.path = Path(path).absolute()

    def _check(self):
        if self.path.name == "control.sqlite3" or self.path.is_symlink():
            raise ValueError("operations state must be separate from the budget ledger")
        budget = self.path.parent / "control.sqlite3"
        if self.path.exists() and (
            not self.path.is_file() or budget.exists() and self.path.samefile(budget)
        ):
            raise ValueError("unsafe operations state file")

    @contextmanager
    def _write(self):
        self._check()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            os.chmod(self.path, 0o600)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                batch_id TEXT, task_id TEXT, observed_at_utc TEXT NOT NULL,
                status TEXT NOT NULL, cause TEXT NOT NULL, PRIMARY KEY(batch_id,task_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS controls (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, at_utc TEXT NOT NULL,
                reason TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS actions (
                token TEXT PRIMARY KEY, action TEXT NOT NULL, subject TEXT NOT NULL,
                at_utc TEXT NOT NULL, outcome TEXT NOT NULL)""")
            yield db

    @contextmanager
    def _read(self):
        self._check()
        if not self.path.exists():
            yield None
            return
        db = sqlite3.connect("file:" + quote(str(self.path)) + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()

    @staticmethod
    def _state(db, policy, now):
        control = db.execute("SELECT * FROM controls ORDER BY id DESC LIMIT 1").fetchone()
        boundary = control["at_utc"] if control and control["kind"] == "clear" else None
        cutoff = (now - timedelta(seconds=policy.window_seconds)).isoformat()
        rows = [dict(row) for row in db.execute("SELECT * FROM observations WHERE observed_at_utc>=?", (cutoff,))]
        if boundary:
            rows = [row for row in rows if datetime.fromisoformat(row["observed_at_utc"]) > datetime.fromisoformat(boundary)]
        decision = failure_decision(rows, policy, now=now)
        held = bool(control and control["kind"] == "hold")
        return {"admission_held": held or decision["hold"],
                "persistent_hold": held, "reason": control["reason"] if held else decision["reason"],
                "observed_outcomes": len(rows), "decision": decision,
                "policy": asdict(policy), "authorizes_spend": False,
                "budget_accounting_changed": False}

    def status(self, policy=None, *, now=None):
        policy, now = policy or FailurePolicy(), utc(now)
        with self._read() as db:
            if db is None:
                return {"admission_held": False, "persistent_hold": False,
                        "observed_outcomes": 0, "reason": "no_operations_observations",
                        "policy": asdict(policy), "authorizes_spend": False,
                        "budget_accounting_changed": False}
            return self._state(db, policy, now)

    def require_admission(self, policy=None):
        if self.status(policy)["admission_held"]:
            raise ValueError("operations admission is held; inspect 'swarm-control ops status' before retrying")

    def observe(self, batch_id, task_id, report, policy=None, *, now=None):
        policy, now = policy or FailurePolicy(), utc(now)
        batch_id, task_id = identifier(batch_id), identifier(task_id)
        outcome = classify_worker_result(report)
        with self._write() as db:
            previous = db.execute("SELECT * FROM observations WHERE batch_id=? AND task_id=?", (batch_id, task_id)).fetchone()
            if previous and (previous["status"], previous["cause"]) != (outcome["status"], outcome["cause"]):
                raise ValueError("a recorded worker outcome cannot be rewritten")
            if not previous:
                db.execute("INSERT INTO observations VALUES (?,?,?,?,?)",
                           (batch_id, task_id, now.isoformat(), outcome["status"], outcome["cause"]))
            state = self._state(db, policy, now)
            if state["decision"]["hold"] and not state["persistent_hold"]:
                db.execute("INSERT INTO controls(kind,at_utc,reason) VALUES ('hold',?,?)",
                           (now.isoformat(), state["decision"]["reason"]))
            return self._state(db, policy, now)

    def clear(self, reason, *, now=None):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 240 or any(ord(c) < 32 for c in reason):
            raise ValueError("clearing a hold requires a short, single-line reason")
        if re.search(r"sk-[A-Za-z0-9_-]{16,}|github_pat_|gh[pousr]_[A-Za-z0-9]{30,}|Bearer\s|PRIVATE KEY", reason):
            raise ValueError("credentials must not enter operations history")
        with self._write() as db:
            db.execute("INSERT INTO controls(kind,at_utc,reason) VALUES ('clear',?,?)",
                       (utc(now).isoformat(), reason.strip()))

    def reserve_action(self, action, subject, *, now=None, maximum_attempts=1, cooldown_seconds=600):
        action, subject, now = identifier(action), identifier(subject), utc(now)
        if type(maximum_attempts) is not int or not 1 <= maximum_attempts <= 10:
            raise ValueError("invalid action attempt cap")
        if type(cooldown_seconds) is not int or not 0 <= cooldown_seconds <= 86400:
            raise ValueError("invalid action cooldown")
        with self._write() as db:
            rows = list(db.execute("SELECT * FROM actions WHERE action=? AND subject=? ORDER BY at_utc", (action, subject)))
            if any(row["outcome"] in {"reserved", "uncertain"} for row in rows):
                raise ValueError("action outcome requires read-only reconciliation; automatic retry is blocked")
            if len(rows) >= maximum_attempts:
                raise ValueError("action attempt cap exhausted for this subject")
            if rows and (now - datetime.fromisoformat(rows[-1]["at_utc"])).total_seconds() < cooldown_seconds:
                raise ValueError("action cooldown has not elapsed")
            token = uuid.uuid4().hex
            db.execute("INSERT INTO actions VALUES (?,?,?,?,?)", (token, action, subject, now.isoformat(), "reserved"))
            return token

    def finish_action(self, token, outcome):
        if outcome not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("invalid action outcome")
        with self._write() as db:
            row = db.execute("SELECT outcome FROM actions WHERE token=?", (identifier(token),)).fetchone()
            if row is None or row["outcome"] not in {"reserved", "uncertain", outcome}:
                raise ValueError("action outcome cannot be overwritten")
            db.execute("UPDATE actions SET outcome=? WHERE token=?", (outcome, token))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "clear-hold", "inspect-ci", "advise-scale"])
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--reason")
    args = parser.parse_args()
    from .pilot_state import common_state
    from .project import load_project

    project = load_project(args.project)
    journal = OperationsJournal(common_state(project.root) / "operations.sqlite3")
    if args.command == "clear-hold":
        journal.clear(args.reason)
    if args.command in {"status", "clear-hold"}:
        result = journal.status(project.failure_policy)
    else:
        if args.input is None:
            parser.error("this command requires a local observation JSON file")
        with args.input.open("rb") as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("observation input is too large")
        source = json.loads(raw)
        if not isinstance(source, dict):
            raise ValueError("observation must be an object")
        if args.command == "advise-scale":
            result = scaling_advice(source, maximum_workers=project.max_live_agents)
        else:
            from .ci_health import classify_ci
            if source.get("repository") != project.repository:
                raise ValueError("CI observation belongs to another repository")
            result = classify_ci(source.get("head_sha"), project.required_checks,
                                 source.get("check_payload"), source.get("workflow_runs"),
                                 observed_at=datetime.now(UTC))
            result["authorizes_spend"] = False
        result["remote_actions_performed"] = 0
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0
