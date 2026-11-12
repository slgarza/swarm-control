"""Explicit GUI policy edits reuse the ledger-preserving migration workflow."""

import hashlib
import json
import secrets

from . import budget_policy
from .project import load_project
from .setup_common import SetupError, private_json, require
from .setup_common import project_path as _path


def configure_budget(repository, path, model_limit_cents, infra_limit_cents,
                     max_live_agents, live_enabled=False, *, directory):
    require(all(type(value) is int and 0 <= value <= 1_000_000
                for value in (model_limit_cents, infra_limit_cents)), "Enter whole-cent cumulative allowances.")
    require(type(max_live_agents) is int and 1 <= max_live_agents <= 100,
            "The repository worker capacity must be between 1 and 100.")
    require(type(live_enabled) is bool)
    require(not live_enabled or min(model_limit_cents, infra_limit_cents) > 0,
            "Enabling execution requires positive model and infrastructure allowances.")
    project = load_project(_path(path))
    project.verify_origin()
    observed = budget_policy.inspect(project)
    require(not observed.get("active_task_count", 0),
            "Finish or reconcile existing active and awaiting tasks before changing this policy.")
    limits = {"model_limit_cents": model_limit_cents, "infra_limit_cents": infra_limit_cents}
    migrated = False
    if observed["ledger_exists"]:
        old = observed["policy"]
        require(all(limits[key] >= old[key] for key in limits),
                "Existing cumulative allowances cannot be reduced or reset.")
        require(all(getattr(project, key) == old[key] for key in limits),
                "The profile differs from its retained policy. Reconcile it before configuring a new allowance.")
        if limits != old:
            # Each write is tracked in the repository preparation journal. A
            # separately preserved confirmation binds exactly this migration.
            if project.live_enabled:
                repository.update_profile(path, {"budget": {"live_enabled": False}})
                project = load_project(project.path)
            migration = budget_policy.plan(project, limits)
            confirmation = {"version": 1, "kind": "budget_policy_increase", "approved": True,
                "authorization_id": "gui-" + secrets.token_hex(16),
                "confirming_authority": "local_gui_operator",
                "confirmation_evidence_sha256": hashlib.sha256((directory / "request.json").read_bytes()).hexdigest(),
                **{key: migration[key] for key in ("repository", "ledger_path", "plan_sha256", "state_sha256",
                                                   "old_policy_source", "old_policy", "new_policy")}}
            private_json(directory / "budget-plan.json", migration)
            private_json(directory / "budget-authorization.json", confirmation)
            applied = budget_policy.apply(project, directory / "budget-plan.json",
                                          directory / "budget-authorization.json", explicitly_apply=True)
            private_json(directory / "budget-migration.json", applied)
            migrated = True
    try:
        updates = {"budget": {**limits, "max_live_agents": max_live_agents, "live_enabled": live_enabled}}
        if max_live_agents > project.gateway_concurrency:
            updates["runtime"] = {"gateway_concurrency": max_live_agents}
        result = repository.update_profile(path, updates)
        current = load_project(project.path)
        status = budget_policy.inspect(current)
        private_json(directory / "budget-result.json", status)
        return {**result, "ledger_migrated": migrated, "live_enabled": live_enabled,
                "max_live_agents": max_live_agents, "limits_cents": limits,
                "local_admission_policy": status["local_admission_policy"],
                "provider_capacity_verified": False, "provider_balance_verified": False,
                "gateway_redeployment_required": "runtime" in updates,
                "next_step": "Review and publish the profile, then explicitly deploy the gateway before a run."}
    except Exception as error:
        if migrated:
            raise SetupError("budget_profile_update_incomplete",
                "The retained budget policy was increased, but the profile update did not finish. Inspect budget status and reconcile the profile; preserve the ledger and this attempt.") from error
        raise


def main():
    # This module is used by the bounded desktop job service, not as an
    # independent allowance-editing command.
    print(json.dumps({"error": "Use the reviewed budget form or budget migration CLI."}))
    return 1
