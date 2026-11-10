"""Durable single-host admission and event summaries shared by Git worktrees."""

import json
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def common_state(root):
    directory = subprocess.check_output(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=root,
        text=True,
    ).strip()
    return Path(directory).parent / ".swarm"


class Ledger:
    def __init__(self, path, *, model_limit_cents=1800, infra_limit_cents=700):
        for amount in (model_limit_cents, infra_limit_cents):
            if type(amount) is not int or amount < 0:
                raise ValueError("budget limits must be nonnegative whole cents")
        self.model_limit_cents = model_limit_cents
        self.infra_limit_cents = infra_limit_cents
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, base_sha TEXT NOT NULL,
                state TEXT NOT NULL, report TEXT, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                state TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS pilot_allocations (
                run_id TEXT PRIMARY KEY, cents INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS pilot_infra_allocations (
                run_id TEXT PRIMARY KEY, cents INTEGER NOT NULL)""")
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS budget_policy (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                model_limit_cents INTEGER NOT NULL, infra_limit_cents INTEGER NOT NULL)""")
            policy = db.execute("SELECT model_limit_cents,infra_limit_cents FROM budget_policy").fetchone()
            requested = (model_limit_cents, infra_limit_cents)
            if policy and tuple(policy) != requested:
                raise ValueError("stored budget policy differs; explicit budget migration is required")
            if not policy:
                legacy = db.execute("SELECT COUNT(*) FROM pilot_allocations").fetchone()[0]
                legacy += db.execute("SELECT COUNT(*) FROM pilot_infra_allocations").fetchone()[0]
                if legacy and requested != (1800, 700):
                    raise ValueError("legacy pilot allowance must be preserved during migration")
                db.execute("INSERT INTO budget_policy VALUES (1,?,?)", requested)

    def _validate_allowances(self, cents, infra_cents):
        for amount, maximum, name in (
            (cents, self.model_limit_cents, "model"), (infra_cents, self.infra_limit_cents, "infrastructure")
        ):
            if type(amount) is not int or not 0 <= amount <= maximum:
                raise ValueError(f"invalid {name} allowance")

    def _allocate(self, db, run_id, cents, infra_cents):
        self._validate_allowances(cents, infra_cents)
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("allocation needs a run ID")
        for table, amount, maximum, name in (
            ("pilot_allocations", cents, self.model_limit_cents, "model"),
            ("pilot_infra_allocations", infra_cents, self.infra_limit_cents, "infrastructure"),
        ):
            if db.execute(f"SELECT 1 FROM {table} WHERE run_id=?", (run_id,)).fetchone():
                raise ValueError(f"run {run_id} already has an allocation")
            allocated = db.execute(f"SELECT COALESCE(SUM(cents),0) FROM {table}").fetchone()[0]
            if allocated + amount > maximum:
                raise ValueError(f"the approved pilot {name} allowance is exhausted")
        db.execute("INSERT INTO pilot_allocations VALUES (?,?)", (run_id, cents))
        db.execute("INSERT INTO pilot_infra_allocations VALUES (?,?)", (run_id, infra_cents))

    def allocate(self, run_id, cents, infra_cents=0):
        """Reserve model and infrastructure against the persisted project policy.

        Failed/ambiguous launches retain allocation; do not silently reset this ledger.
        Infrastructure amounts are conservative admission reserves, not provider invoices.
        Zero model cents supports setup/smoke runs without model calls.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._allocate(db, run_id, cents, infra_cents)

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

    def claim(self, tasks, batch_id, base_sha, *, retry=False, budget_cents=None, infra_cents=0):
        """Claim all tasks and optional reserves in one admission transaction."""
        self._validate_allowances(0 if budget_cents is None else budget_cents, infra_cents)
        if len({task["id"] for task in tasks}) != len(tasks):
            raise ValueError("duplicate task IDs in claim")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for task in tasks:
                previous = db.execute(
                    "SELECT * FROM tasks WHERE task_id=?", (task["id"],)
                ).fetchone()
                if previous and (
                    previous["state"] in {"running", "awaiting_integration", "merged"}
                    or not retry
                ):
                    raise ValueError(
                        f"{task['id']} already recorded as {previous['state']}; inspect its run"
                    )
            if budget_cents is not None or infra_cents:
                self._allocate(db, batch_id, 0 if budget_cents is None else budget_cents, infra_cents)
            timestamp = datetime.now(UTC).isoformat()
            for task in tasks:
                db.execute(
                    "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?)",
                    (task["id"], batch_id, base_sha, "running", None, timestamp),
                )
                db.execute(
                    "INSERT INTO events(task_id,batch_id,state,recorded_at) VALUES (?,?,?,?)",
                    (task["id"], batch_id, "running", timestamp),
                )

    def finish(self, task_id, batch_id, state, report):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            timestamp = datetime.now(UTC).isoformat()
            result = db.execute(
                "UPDATE tasks SET state=?,report=?,updated_at=? WHERE task_id=? AND batch_id=?",
                (state, str(report), timestamp, task_id, batch_id),
            )
            if result.rowcount != 1:
                raise ValueError("attempt does not own this task")
            db.execute(
                "INSERT INTO events(task_id,batch_id,state,recorded_at) VALUES (?,?,?,?)",
                (task_id, batch_id, state, timestamp),
            )

    def rows(self):
        with self.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY task_id")
            ]


def verify_github_tasks(tasks, root, base_sha, *, repository, tasks_dir=None, query=None):
    """Only fresh issues with merged dependencies may enter a paid batch."""

    def github(path):
        return json.loads(
            subprocess.check_output(["gh", "api", path], cwd=root, text=True)
        )

    issue_numbers = set()
    for task in tasks:
        if task.get("status", "ready") != "ready":
            raise ValueError(f"{task['id']} is {task['status']}, not ready")
        number = task.get("issue")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError(f"{task['id']} needs its own GitHub issue")
        if number in issue_numbers:
            raise ValueError(f"duplicate GitHub issue #{number} in batch")
        issue_numbers.add(number)
    query = query or github
    tasks_dir = tasks_dir or root / "swarm/tasks"
    open_prs = query(f"repos/{repository}/pulls?state=open&per_page=100")
    if len(open_prs) == 100:
        raise ValueError("PR pagination requires review before admission")
    for task in tasks:
        number = task["issue"]
        issue = query(f"repos/{repository}/issues/{number}")
        if issue.get("state") != "open" or "pull_request" in issue:
            raise ValueError(f"{task['id']}: issue #{number} is not open work")
        if any(f"[{task['id']}]" in pr.get("title", "") for pr in open_prs):
            raise ValueError(f"{task['id']} already has an open PR")
        for dependency in task.get("dependsOn", []):
            metadata = json.loads(
                (tasks_dir / f"{dependency}.json").read_text()
            )
            pr_number = metadata.get("pullRequest")
            if not isinstance(pr_number, int) or isinstance(pr_number, bool):
                raise ValueError(f"{task['id']}: {dependency} has no integration PR")  # noqa: TRY004 - invalid metadata contract
            pr = query(f"repos/{repository}/pulls/{pr_number}")
            commit = pr.get("merge_commit_sha")
            if not pr.get("merged") or not isinstance(commit, str):
                raise ValueError(f"{task['id']}: {dependency} is not merged")
            if subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, base_sha],
                cwd=root,
                capture_output=True,
                check=False,
            ).returncode:
                raise ValueError(f"{task['id']}: base does not include {dependency}")


def peak_intervals(intervals):
    events = []
    for start, end in intervals:
        if start and end and start < end:
            events.extend([(start, 1), (end, -1)])
    current = peak = 0
    for _, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    return peak


def batch_summary(batch):
    reports = []
    productive_intervals = []
    for path in sorted(Path(batch).glob("*/report.json")):
        report = json.loads(path.read_text())
        report["commands_completed"] = 0
        report["file_change_events"] = 0
        events = path.parent / "agent.jsonl"
        first_work = None
        if events.exists():
            for line in events.read_text().splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item = event.get("item", {})
                if (
                    event.get("type") == "item.completed"
                    and item.get("type") == "command_execution"
                ):
                    report["commands_completed"] += 1
                    first_work = first_work or event.get("swarm_received_at")
                if (
                    item.get("type") == "file_change"
                    and event.get("type") == "item.completed"
                ):
                    report["file_change_events"] += 1
        if first_work and report["commands_completed"]:
            productive_intervals.append((first_work, report.get("agent_finished_at")))
        reports.append(report)
    return {
        "task_count": len(reports),
        "validated_count": sum(r["status"] == "validated" for r in reports),
        "live_process_peak": peak_intervals(
            [(r.get("agent_started_at"), r.get("agent_finished_at")) for r in reports]
        ),
        "sessions_after_first_command_peak": peak_intervals(productive_intervals),
        "all_sandboxes_terminated": all(
            r.get("sandbox_terminated") is True for r in reports
        ),
        "merged_green_prs": 0,
        "tasks": [
            {
                key: r.get(key)
                for key in (
                    "task_id",
                    "status",
                    "wall_seconds",
                    "commands_completed",
                    "file_change_events",
                    "agent_started_at",
                    "agent_finished_at",
                    "token_usage",
                )
            }
            for r in reports
        ],
    }
