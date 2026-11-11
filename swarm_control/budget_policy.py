"""Offline, explicitly reviewed increases to the cumulative local budget policy.

Inspection and planning never instantiate Ledger or open SQLite for writing.
Applying a reviewed plan is a separate operation, creates no paid resources and
cannot refund, replace or remove any allocation or task history.
"""

import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .modal_runner import controller_lock
from .pilot_integrate import integration_lock
from .pilot_state import common_state
from .project import load_project

LIMIT_KEYS = ("model_limit_cents", "infra_limit_cents")
LEGACY_POLICY = dict(zip(LIMIT_KEYS, (1800, 700), strict=True))
REQUIRED_TABLES = {"tasks", "events", "pilot_allocations", "pilot_infra_allocations"}
AUTH_FIELDS = {
    "version", "kind", "approved", "authorization_id", "repository", "ledger_path",
    "plan_sha256", "state_sha256", "old_policy_source", "old_policy", "new_policy",
    "confirming_authority", "confirmation_evidence_sha256",
}
ACTIVE_STATES = {"running", "starting", "preparing", "working", "checking", "validated"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def policy(value):
    require(isinstance(value, dict) and set(value) == set(LIMIT_KEYS), "invalid policy fields")
    require(all(type(value[key]) is int and 0 <= value[key] <= 1_000_000 for key in LIMIT_KEYS),
            "policy limits must be whole cents from 0 to 1000000")
    return value


def ledger_path(project):
    return common_state(project.root) / "control.sqlite3"


def identity(path):
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino}


def check_state_directory(state):
    require(not state.is_symlink() and state.resolve() == state,
            "budget state directory must not contain symlink components")
    for name in ("modal-controller.lock", "integration.lock"):
        path = state / name
        require(not path.is_symlink() and (not path.exists() or path.is_file()),
                "budget migration lock files must be regular and not symlinks")


def private_json(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), "review inputs must be regular private files")
    require(path.stat().st_mode & 0o077 == 0, "review inputs must have private file permissions (chmod 600)")
    require(path.stat().st_size <= 1_000_000, "review input is too large")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("review input is not valid JSON") from None
    require(isinstance(value, dict), "review input must be a JSON object")
    return value, sha256(raw)


@contextmanager
def connection(path, mode):
    check_state_directory(path.parent)
    require(not path.is_symlink() and path.is_file(), "existing regular budget ledger required")
    require(path.stat().st_size <= 256 * 1024 * 1024, "budget ledger exceeds inspection size bound")
    db = sqlite3.connect(path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=0)
    try:
        db.execute("PRAGMA trusted_schema = OFF")
        if mode == "ro":
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
        yield db
    except sqlite3.Error:
        raise ValueError("budget ledger unavailable, busy or invalid; no migration committed") from None
    finally:
        db.close()


def snapshot(db):
    schema = list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema "
                             "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
    tables = {row[1] for row in schema if row[0] == "table"}
    require(REQUIRED_TABLES <= tables, "ledger lacks required allocation/task history tables")
    fingerprints = {}
    for table in sorted(tables):
        quoted = '"' + table.replace('"', '""') + '"'
        rows = []
        for row in db.execute(f"SELECT * FROM {quoted}"):
            require(len(rows) < 1_000_000, "ledger exceeds inspection row bound")
            rows.append(canonical([{"blob_hex": cell.hex()} if isinstance(cell, bytes) else cell for cell in row]))
        fingerprints[table] = sha256(canonical(sorted(rows)).encode())
    state_digest = sha256(canonical({"schema": schema, "tables": fingerprints}).encode())
    totals = {}
    for key, table in (("model_cents", "pilot_allocations"), ("infra_cents", "pilot_infra_allocations")):
        entries = list(db.execute(f"SELECT run_id,cents FROM {table}"))
        require(all(isinstance(run_id, str) and run_id.strip() and type(cents) is int and cents >= 0
                    for run_id, cents in entries), "invalid retained budget allocation")
        totals[key] = sum(cents for _, cents in entries)
    rows = list(db.execute("SELECT singleton,model_limit_cents,infra_limit_cents FROM budget_policy")) if "budget_policy" in tables else []
    require(len(rows) <= 1 and (not rows or rows[0][0] == 1), "invalid stored budget policy")
    current = policy(dict(zip(LIMIT_KEYS, rows[0][1:], strict=True))) if rows else dict(LEGACY_POLICY)
    require(totals["model_cents"] <= current["model_limit_cents"]
            and totals["infra_cents"] <= current["infra_limit_cents"], "allocations exceed existing policy")
    states = [state for (state,) in db.execute("SELECT state FROM tasks")]
    require(all(isinstance(state, str) for state in states), "invalid task state")
    active = sum(state in ACTIVE_STATES or state.startswith("awaiting") for state in states)
    return {
        "policy": current,
        "policy_source": "stored" if rows else "legacy_inferred_v1_default",
        "policy_table_present": "budget_policy" in tables,
        "allocation_totals": totals,
        "remaining_allowances": {"model_cents": current["model_limit_cents"] - totals["model_cents"],
                                 "infra_cents": current["infra_limit_cents"] - totals["infra_cents"]},
        "active_task_count": active,
        "state_sha256": state_digest,
        "preserved_tables_sha256": {key: value for key, value in fingerprints.items()
                                    if key not in {"budget_policy", "policy_migrations"}},
    }


def inspect(project):
    path = ledger_path(project)
    result = {"repository": project.repository, "ledger_path": str(path),
              "ledger_exists": path.exists(), "read_only": True, "authorizes_spend": False,
              "live_execution_enabled": project.live_enabled}
    if path.exists():
        with connection(path, "ro") as db:
            result.update(snapshot(db))
            result["ledger_identity"] = identity(path)
    # Retain the original JSON fields for migration clients, but identify their
    # actual meaning. Neither an allocation nor its local remainder is a bill
    # or the balance of the provider account.
    limits = result.get("policy", {key: getattr(project, key) for key in LIMIT_KEYS})
    totals = result.get("allocation_totals")
    remaining = result.get("remaining_allowances")
    result.update(
        accounting_kind="local_admission_reservations",
        remaining_allowances_meaning="unreserved_local_admission_allowance_not_provider_balance",
        local_admission_policy={
            "currency": "usd", "amount_unit": "whole_cents",
            "limits_source": result.get("policy_source", "profile_without_ledger"),
            "limits_cents": {"model": limits["model_limit_cents"],
                             "infrastructure": limits["infra_limit_cents"]},
            "reserved_cents": {"model": totals["model_cents"], "infrastructure": totals["infra_cents"]}
            if totals is not None else None,
            "unreserved_cents": {"model": remaining["model_cents"], "infrastructure": remaining["infra_cents"]}
            if remaining is not None else None,
            "profile_matches_recorded_policy":
                {key: getattr(project, key) for key in LIMIT_KEYS} == limits if totals is not None else None,
            "provider_balance": False, "measures_actual_spend": False,
        },
        provider_account={
            "queried": False, "reported_spend_usd": None,
            "credit_balance_usd": None, "monthly_spend_limit_usd": None,
            "reason": "inspect_reads_only_local_admission_policy_use_budget_status_for_spend_evidence",
        },
    )
    return result


def plan(project, new_policy):
    require(not project.live_enabled, "disable live execution before planning a budget migration")
    new_policy = policy(new_policy)
    state = inspect(project)
    require(state["ledger_exists"], "cannot migrate a missing budget ledger")
    old = state["policy"]
    require({key: getattr(project, key) for key in LIMIT_KEYS} == old,
            "profile limits must match existing policy before migration")
    require(all(new_policy[key] >= old[key] for key in LIMIT_KEYS) and new_policy != old,
            "migration must increase at least one limit and cannot decrease either limit")
    require(state["active_task_count"] == 0, "finish or reconcile active/awaiting tasks before migration")
    result = {
        "version": 1, "kind": "budget_policy_migration_plan", "repository": project.repository,
        "ledger_path": state["ledger_path"], "created_at": datetime.now(UTC).isoformat(),
        "ledger_identity": state["ledger_identity"],
        "profile_sha256": sha256(project.path.read_bytes()),
        "old_policy_source": state["policy_source"], "old_policy": old, "new_policy": new_policy,
        "allocation_totals": state["allocation_totals"], "state_sha256": state["state_sha256"],
        "authorizes_spend": False, "gateway_policy_modified": False,
    }
    result["plan_sha256"] = sha256(canonical(result).encode())
    return result


def validate_review(project, migration, authorization):
    require(migration.get("version") == 1 and migration.get("kind") == "budget_policy_migration_plan",
            "invalid migration plan")
    expected_digest = sha256(canonical({key: value for key, value in migration.items() if key != "plan_sha256"}).encode())
    require(migration.get("plan_sha256") == expected_digest, "migration plan digest mismatch")
    require(set(authorization) == AUTH_FIELDS and authorization.get("version") == 1
            and authorization.get("kind") == "budget_policy_increase" and authorization.get("approved") is True,
            "separate explicit budget authorization receipt required")
    require(isinstance(authorization.get("authorization_id"), str)
            and re.fullmatch(r"[A-Za-z0-9_-]{8,100}", authorization["authorization_id"]), "invalid authorization identity")
    require(isinstance(authorization.get("confirming_authority"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._@:/+-]{0,199}", authorization["confirming_authority"]),
            "confirming authority identity required")
    require(isinstance(authorization.get("confirmation_evidence_sha256"), str)
            and re.fullmatch(r"[a-f0-9]{64}", authorization["confirmation_evidence_sha256"]),
            "private confirmation evidence digest required")
    for key in ("repository", "ledger_path", "plan_sha256", "state_sha256", "old_policy_source", "old_policy", "new_policy"):
        require(authorization.get(key) == migration.get(key), f"authorization does not bind reviewed {key}")
    require(migration.get("repository") == project.repository and migration.get("ledger_path") == str(ledger_path(project)),
            "reviewed plan targets another repository or ledger")
    policy(migration.get("old_policy"))
    policy(migration.get("new_policy"))
    require(all(migration["new_policy"][key] >= migration["old_policy"][key] for key in LIMIT_KEYS)
            and migration["new_policy"] != migration["old_policy"], "reviewed increase cannot decrease or reset limits")


def apply(project, plan_file, authorization_file, *, explicitly_apply=False):
    require(explicitly_apply, "--apply is required; no budget migration was performed")
    require(not project.live_enabled, "live execution must remain disabled during migration")
    migration, _ = private_json(plan_file)
    authorization, receipt_hash = private_json(authorization_file)
    validate_review(project, migration, authorization)
    state_dir = common_state(project.root)
    check_state_directory(state_dir)
    with controller_lock(state_dir), integration_lock(state_dir):
        check_state_directory(state_dir)
        current_project = load_project(project.path)
        require(not current_project.live_enabled, "live execution must remain disabled during migration")
        require(sha256(current_project.path.read_bytes()) == migration["profile_sha256"], "profile changed since migration review")
        require({key: getattr(current_project, key) for key in LIMIT_KEYS} == migration["old_policy"],
                "profile limits must match the reviewed old policy")
        current_project.verify_origin()
        require(identity(ledger_path(current_project)) == migration["ledger_identity"], "ledger file replaced since migration review")
        with connection(ledger_path(current_project), "rw") as db:
            db.execute("BEGIN IMMEDIATE")
            before = snapshot(db)
            require(before["active_task_count"] == 0, "active/awaiting tasks prevent migration")
            require(before["state_sha256"] == migration["state_sha256"], "ledger changed since migration review")
            require(before["policy"] == migration["old_policy"] and before["policy_source"] == migration["old_policy_source"],
                    "existing policy differs from reviewed policy")
            require(before["allocation_totals"] == migration["allocation_totals"], "retained allocations changed")
            db.execute("CREATE TABLE IF NOT EXISTS budget_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                       "model_limit_cents INTEGER NOT NULL, infra_limit_cents INTEGER NOT NULL)")
            if before["policy_source"] == "legacy_inferred_v1_default":
                require(before["policy"] == LEGACY_POLICY, "legacy policy must preserve the original allowance")
                db.execute("INSERT INTO budget_policy VALUES (1,?,?)", tuple(LEGACY_POLICY.values()))
            db.execute("CREATE TABLE IF NOT EXISTS policy_migrations (authorization_id TEXT PRIMARY KEY, "
                       "repository TEXT NOT NULL, applied_at TEXT NOT NULL, confirming_authority TEXT NOT NULL, "
                       "authorization_receipt_sha256 TEXT NOT NULL, confirmation_evidence_sha256 TEXT NOT NULL, "
                       "plan_sha256 TEXT NOT NULL, before_state_sha256 TEXT NOT NULL, previous_policy_source TEXT NOT NULL, "
                       "old_policy_json TEXT NOT NULL, new_policy_json TEXT NOT NULL, "
                       "allocation_totals_before_json TEXT NOT NULL, allocation_totals_after_json TEXT NOT NULL)")
            require(not db.execute("SELECT 1 FROM policy_migrations WHERE authorization_id=?", (authorization["authorization_id"],)).fetchone(),
                    "authorization identity has already been applied")
            db.execute("CREATE TRIGGER IF NOT EXISTS policy_migrations_no_update BEFORE UPDATE ON policy_migrations "
                       "BEGIN SELECT RAISE(ABORT, 'policy migration audit is append-only'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS policy_migrations_no_delete BEFORE DELETE ON policy_migrations "
                       "BEGIN SELECT RAISE(ABORT, 'policy migration audit is append-only'); END")
            old, new = migration["old_policy"], migration["new_policy"]
            updated = db.execute("UPDATE budget_policy SET model_limit_cents=?,infra_limit_cents=? "
                                 "WHERE singleton=1 AND model_limit_cents=? AND infra_limit_cents=?",
                                 (new[LIMIT_KEYS[0]], new[LIMIT_KEYS[1]], old[LIMIT_KEYS[0]], old[LIMIT_KEYS[1]]))
            require(updated.rowcount == 1, "stored policy changed during migration")
            totals = canonical(before["allocation_totals"])
            db.execute("INSERT INTO policy_migrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                authorization["authorization_id"], project.repository, datetime.now(UTC).isoformat(),
                authorization["confirming_authority"], receipt_hash, authorization["confirmation_evidence_sha256"],
                migration["plan_sha256"], before["state_sha256"], before["policy_source"],
                canonical(old), canonical(new), totals, totals,
            ))
            after = snapshot(db)
            require(after["preserved_tables_sha256"] == before["preserved_tables_sha256"]
                    and after["allocation_totals"] == before["allocation_totals"] and after["policy"] == new,
                    "migration changed protected history; transaction rolled back")
            require(sha256(current_project.path.read_bytes()) == migration["profile_sha256"], "profile changed during migration")
            check_state_directory(state_dir)
            require(identity(ledger_path(current_project)) == migration["ledger_identity"], "ledger file replaced during migration")
            db.commit()
    return {"applied": True, "repository": project.repository, "authorization_id": authorization["authorization_id"],
            "authorization_receipt_sha256": receipt_hash, "old_policy": old, "new_policy": new,
            "allocation_totals": after["allocation_totals"], "state_sha256": after["state_sha256"],
            "live_execution_enabled": False, "gateway_policy_modified": False,
            "next_step": "Review and update only the profile limits to match the stored policy; keep live execution disabled."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    from .budget_reporting import add_commands, run_command

    add_commands(subparsers)
    for name in ("inspect", "plan", "apply"):
        command = subparsers.add_parser(name)
        command.add_argument("--project", type=Path, required=True)
        if name == "plan":
            command.add_argument("--model-limit-cents", type=int, required=True)
            command.add_argument("--infra-limit-cents", type=int, required=True)
        if name == "apply":
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--authorization", type=Path, required=True)
            command.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    project = load_project(args.project)
    if args.command in {"status", "import-spend", "sync-openai"}:
        return run_command(project, args)
    if args.command == "inspect":
        result = inspect(project)
    elif args.command == "plan":
        result = plan(project, {"model_limit_cents": args.model_limit_cents, "infra_limit_cents": args.infra_limit_cents})
    else:
        result = apply(project, args.plan, args.authorization, explicitly_apply=args.apply)
    print(json.dumps(result, indent=2))
    return 0
