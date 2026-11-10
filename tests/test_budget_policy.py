"""Synthetic, offline accounting migrations; never touch a real target ledger."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from swarm_control import budget_policy as budget
from swarm_control import cli
from swarm_control.modal_runner import controller_lock
from swarm_control.pilot_integrate import integration_lock
from swarm_control.pilot_state import Ledger
from swarm_control.project import load_project
from tests.support import make_project


class BudgetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = make_project(self.root / "target")
        self.path = self.project.root / ".swarm/control.sqlite3"
        ledger = Ledger(self.path)
        ledger.allocate("retained_failed_attempt", 1800, infra_cents=670)
        ledger.claim([{"id": "T01"}], "finished_batch", "a" * 40)
        ledger.finish("T01", "finished_batch", "merged", "private-report.json")
        self.increase = {"model_limit_cents": 4500, "infra_limit_cents": 5000}

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in ("tasks", "events", "pilot_allocations", "pilot_infra_allocations")}

    def remove_policy(self):
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE budget_policy")

    def reviewed(self, *, migration=None, authorization_id="approved_synthetic_001"):
        migration = migration or budget.plan(self.project, self.increase)
        authorization = {key: migration[key] for key in (
            "repository", "ledger_path", "plan_sha256", "state_sha256", "old_policy_source", "old_policy", "new_policy",
        )}
        authorization.update(version=1, kind="budget_policy_increase", approved=True,
                             authorization_id=authorization_id, confirming_authority="synthetic-owner",
                             confirmation_evidence_sha256="c" * 64)
        plan_path, receipt_path = self.root / "reviewed-plan.json", self.root / "authorization.json"
        for path, value in ((plan_path, migration), (receipt_path, authorization)):
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        return plan_path, receipt_path

    def apply(self, paths=None):
        return budget.apply(self.project, *(paths or self.reviewed()), explicitly_apply=True)

    def test_missing_ledger_inspection_creates_no_state_or_policy(self):
        project = make_project(self.root / "unstarted")
        with patch("swarm_control.pilot_state.Ledger", side_effect=AssertionError("Ledger constructor called")):
            result = budget.inspect(project)
        self.assertFalse(result["ledger_exists"])
        self.assertFalse((project.root / ".swarm").exists())
        with self.assertRaisesRegex(ValueError, "missing budget ledger"):
            budget.plan(project, self.increase)
        self.assertFalse((project.root / ".swarm").exists())

    def test_legacy_inspect_and_plan_are_read_only_with_explicit_original_policy(self):
        self.remove_policy()
        before = self.path.read_bytes()
        files = sorted(path.name for path in self.path.parent.iterdir())
        with patch("swarm_control.pilot_state.Ledger", side_effect=AssertionError("Ledger constructor called")):
            result = budget.inspect(self.project)
            migration = budget.plan(self.project, self.increase)
        self.assertEqual(result["policy_source"], "legacy_inferred_v1_default")
        self.assertFalse(result["policy_table_present"])
        self.assertEqual(result["policy"], {"model_limit_cents": 1800, "infra_limit_cents": 700})
        self.assertEqual(result["allocation_totals"], {"model_cents": 1800, "infra_cents": 670})
        self.assertEqual(result["remaining_allowances"], {"model_cents": 0, "infra_cents": 30})
        self.assertFalse(migration["authorizes_spend"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), files)

    def test_reviewed_legacy_migration_preserves_every_allocation_task_and_event(self):
        self.remove_policy()
        before = self.rows()
        paths = self.reviewed()
        receipt_hash = budget.sha256(paths[1].read_bytes())
        profile = self.project.path.read_bytes()
        result = self.apply(paths)
        self.assertTrue(result["applied"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.project.path.read_bytes(), profile)
        inspected = budget.inspect(self.project)
        self.assertEqual(inspected["policy"], self.increase)
        self.assertEqual(inspected["policy_source"], "stored")
        self.assertEqual(inspected["remaining_allowances"], {"model_cents": 2700, "infra_cents": 4330})
        with sqlite3.connect(self.path) as db:
            receipt = db.execute("SELECT authorization_receipt_sha256,previous_policy_source,"
                                 "allocation_totals_before_json,allocation_totals_after_json FROM policy_migrations").fetchone()
        self.assertEqual(receipt[0], receipt_hash)
        self.assertEqual(receipt[1], "legacy_inferred_v1_default")
        self.assertEqual(receipt[2], receipt[3])
        self.assertEqual(json.loads(receipt[2]), {"model_cents": 1800, "infra_cents": 670})
        self.assertFalse(result["live_execution_enabled"])
        self.assertFalse(result["gateway_policy_modified"])

    def test_stored_policy_migrates_and_new_profile_reopens_the_same_history(self):
        before = self.rows()
        self.apply()
        self.project.path.write_text(self.project.path.read_text().replace(
            "model_limit_cents = 1800", "model_limit_cents = 4500",
        ).replace("infra_limit_cents = 700", "infra_limit_cents = 5000"))
        project = load_project(self.project.path)
        reopened = project.ledger(self.path)
        self.assertEqual(reopened.model_limit_cents, 4500)
        self.assertEqual(reopened.infra_limit_cents, 5000)
        self.assertEqual(self.rows(), before)
        with self.assertRaisesRegex(ValueError, "stored budget policy differs"):
            self.project.ledger(self.path)

    def test_apply_needs_flag_and_separate_private_matching_authorization(self):
        paths = self.reviewed()
        before = budget.inspect(self.project)["state_sha256"]
        with self.assertRaisesRegex(ValueError, "--apply is required"):
            budget.apply(self.project, *paths)
        for update in (
            {"approved": False}, {"confirming_authority": ""}, {"confirmation_evidence_sha256": ""},
            {"repository": "other/repository"}, {"old_policy_source": "invented"},
            {"new_policy": {"model_limit_cents": 9999, "infra_limit_cents": 9999}},
        ):
            with self.subTest(update=update):
                authorization = json.loads(paths[1].read_text())
                authorization.update(update)
                paths[1].write_text(json.dumps(authorization))
                with self.assertRaises(ValueError):
                    self.apply(paths)
                paths = self.reviewed()
        paths[1].chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private file permissions"):
            self.apply(paths)
        self.assertEqual(budget.inspect(self.project)["state_sha256"], before)

    def test_limits_cannot_decrease_overflow_use_bools_or_remain_unchanged(self):
        for new in (
            {"model_limit_cents": 1700, "infra_limit_cents": 5000},
            {"model_limit_cents": 4500, "infra_limit_cents": 699},
            {"model_limit_cents": 1_000_001, "infra_limit_cents": 5000},
            {"model_limit_cents": True, "infra_limit_cents": 5000},
            {"model_limit_cents": 1800, "infra_limit_cents": 700},
        ):
            with self.subTest(new=new), self.assertRaises(ValueError):
                budget.plan(self.project, new)
        self.assertEqual(budget.inspect(self.project)["policy"], budget.LEGACY_POLICY)

    def test_rehashed_review_cannot_authorize_a_policy_decrease(self):
        migration = budget.plan(self.project, self.increase)
        migration["new_policy"]["model_limit_cents"] = 1700
        migration["plan_sha256"] = budget.sha256(budget.canonical(
            {key: value for key, value in migration.items() if key != "plan_sha256"},
        ).encode())
        with self.assertRaisesRegex(ValueError, "decrease or reset"):
            self.apply(self.reviewed(migration=migration))

    def test_plan_and_authorization_are_bound_to_original_state_and_profile(self):
        paths = self.reviewed()
        migration = json.loads(paths[0].read_text())
        migration["new_policy"]["infra_limit_cents"] = 5001
        paths[0].write_text(json.dumps(migration))
        with self.assertRaisesRegex(ValueError, "plan digest mismatch"):
            self.apply(paths)
        paths = self.reviewed()
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO pilot_allocations VALUES ('new_zero_reservation',0)")
        with self.assertRaisesRegex(ValueError, "ledger changed"):
            self.apply(paths)
        paths = self.reviewed()
        self.project.path.write_text(self.project.path.read_text() + "\n# changed after review\n")
        with self.assertRaisesRegex(ValueError, "profile changed"):
            self.apply(paths)

    def test_active_and_awaiting_tasks_prevent_planning_and_applying(self):
        paths = self.reviewed()
        for state in ("running", "awaiting_integration"):
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE tasks SET state=?", (state,))
            with self.subTest(state=state):
                with self.assertRaisesRegex(ValueError, "active/awaiting"):
                    budget.plan(self.project, self.increase)
                with self.assertRaisesRegex(ValueError, "active/awaiting"):
                    self.apply(paths)
        self.assertEqual(budget.inspect(self.project)["policy"], budget.LEGACY_POLICY)

    def test_both_controller_locks_and_sqlite_writer_block_migration(self):
        paths = self.reviewed()
        state = self.path.parent
        for lock in (controller_lock, integration_lock):
            with self.subTest(lock=lock.__name__), lock(state), self.assertRaisesRegex(ValueError, "active"):
                self.apply(paths)
        db = sqlite3.connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(ValueError, "busy or invalid"):
                self.apply(paths)
        finally:
            db.rollback()
            db.close()
        self.assertEqual(budget.inspect(self.project)["policy"], budget.LEGACY_POLICY)

    def test_live_execution_or_early_profile_increase_cannot_bypass_migration_review(self):
        paths = self.reviewed()
        self.project.path.write_text(self.project.path.read_text().replace("live_enabled = false", "live_enabled = true"))
        enabled = load_project(self.project.path)
        with self.assertRaisesRegex(ValueError, "disable live execution"):
            budget.plan(enabled, self.increase)
        with self.assertRaisesRegex(ValueError, "live execution must remain disabled"):
            budget.apply(enabled, *paths, explicitly_apply=True)
        self.project.path.write_text(self.project.path.read_text().replace("live_enabled = true", "live_enabled = false").replace(
            "model_limit_cents = 1800", "model_limit_cents = 4500",
        ))
        with self.assertRaisesRegex(ValueError, "profile limits must match"):
            budget.plan(load_project(self.project.path), self.increase)

    def test_audit_is_append_only_and_authorization_identity_cannot_be_reused(self):
        self.apply()
        with sqlite3.connect(self.path) as db:
            for statement in ("UPDATE policy_migrations SET confirming_authority='changed'", "DELETE FROM policy_migrations"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    db.execute(statement)
        self.project.path.write_text(self.project.path.read_text().replace("model_limit_cents = 1800", "model_limit_cents = 4500").replace(
            "infra_limit_cents = 700", "infra_limit_cents = 5000",
        ))
        self.project = load_project(self.project.path)
        migration = budget.plan(self.project, {"model_limit_cents": 4600, "infra_limit_cents": 5100})
        with self.assertRaisesRegex(ValueError, "already been applied"):
            self.apply(self.reviewed(migration=migration))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM policy_migrations").fetchone()[0], 1)

    def test_unexpected_trigger_cannot_refund_allocations_during_migration(self):
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER accidental_refund AFTER UPDATE ON budget_policy "
                       "BEGIN DELETE FROM pilot_allocations; END")
        before = self.rows()
        paths = self.reviewed()
        with self.assertRaisesRegex(ValueError, "protected history"):
            self.apply(paths)
        self.assertEqual(self.rows(), before)
        self.assertEqual(budget.inspect(self.project)["policy"], budget.LEGACY_POLICY)
        with sqlite3.connect(self.path) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_schema WHERE name='policy_migrations'").fetchone())

    def test_symlinked_ledger_or_state_directory_is_rejected(self):
        original = self.path.with_name("retained.sqlite3")
        self.path.rename(original)
        self.path.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "regular budget ledger"):
            budget.inspect(self.project)
        self.path.unlink()
        original.rename(self.path)
        state = self.path.parent
        moved = state.with_name("private-state")
        state.rename(moved)
        state.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "state directory must not contain symlink"):
            budget.inspect(self.project)

    def test_apply_rejects_forged_matching_profile_hash_with_wrong_old_limits(self):
        migration = budget.plan(self.project, self.increase)
        self.project.path.write_text(self.project.path.read_text().replace("model_limit_cents = 1800", "model_limit_cents = 2000"))
        migration["profile_sha256"] = budget.sha256(self.project.path.read_bytes())
        migration["plan_sha256"] = budget.sha256(budget.canonical(
            {key: value for key, value in migration.items() if key != "plan_sha256"},
        ).encode())
        with self.assertRaisesRegex(ValueError, "profile limits must match the reviewed old policy"):
            self.apply(self.reviewed(migration=migration))

    def test_symlinked_lock_cannot_redirect_migration_and_same_state_replacement_is_rejected(self):
        paths = self.reviewed()
        victim = self.root / "unrelated.txt"
        victim.write_text("unrelated original bytes")
        lock = self.path.parent / "modal-controller.lock"
        lock.symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "lock files must be regular"):
            self.apply(paths)
        self.assertEqual(victim.read_text(), "unrelated original bytes")
        lock.unlink()
        retained = self.path.with_name("retained-original.sqlite3")
        original = self.path.read_bytes()
        self.path.rename(retained)
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "ledger file replaced"):
            self.apply(paths)

    def test_cli_routes_read_only_inspection_and_requires_explicit_apply_flag(self):
        output = io.StringIO()
        with patch("sys.argv", ["swarm-control", "budget", "inspect", "--project", str(self.project.path)]), redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(json.loads(output.getvalue())["read_only"])
        before = budget.inspect(self.project)["state_sha256"]
        paths = self.reviewed()
        with patch("sys.argv", ["swarm-control", "budget", "apply", "--project", str(self.project.path),
                               "--plan", str(paths[0]), "--authorization", str(paths[1])]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main()
        self.assertEqual(budget.inspect(self.project)["state_sha256"], before)
