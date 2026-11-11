"""Read-only reporting of local admission controls and provider spend evidence.

The local ledger is not a provider balance, and provider aggregate costs are not
automatically attributed to a repository/run or reconciled into its allowance.
"""

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

DEFAULT_MAX_AGE_SECONDS = 86400
PROVIDERS = ("openai", "modal")
SOURCE_KINDS = {"openai_costs_api", "dashboard_observation", "provider_export"}


def utc_now():
    return datetime.now(UTC)


def _aware(value):
    if not isinstance(value, str):
        raise TypeError("timestamp must be a timezone-aware ISO value")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("timestamp must be a timezone-aware ISO value") from None
    if parsed.utcoffset() is None:
        raise ValueError("timestamp must be a timezone-aware ISO value")
    return parsed.astimezone(UTC)


def parse_provider_time(value):
    """CLI calendar dates mean UTC midnight; timestamp arguments need a zone."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = datetime.fromisoformat(value).replace(tzinfo=UTC)
        else:
            parsed = _aware(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("use YYYY-MM-DD (UTC) or a timezone-aware ISO timestamp") from None
    return int(parsed.timestamp())


def _freshness(observed_at, now, max_age_seconds):
    if observed_at is None:
        return {"status": "unknown", "age_seconds": None, "reason": "observation_time_not_known"}
    try:
        observed = _aware(observed_at)
    except (TypeError, ValueError):
        return {"status": "unknown", "age_seconds": None, "reason": "observation_time_invalid"}
    age = (now - observed).total_seconds()
    if age < -300:
        return {"status": "unknown", "age_seconds": None, "reason": "observation_time_in_future"}
    age = max(age, 0)
    return {"status": "stale" if age > max_age_seconds else "recent", "age_seconds": age,
            "reason": "provider_reporting_can_lag"}


def _unknown_provider(provider, reason):
    return {
        "provider": provider, "available": False, "reason": reason,
        "reported_spend_usd": None, "monthly_reference_usd": None,
        "provider_credit_balance_usd": None, "provider_budget_remaining_usd": None,
        "repository_attributed_spend_usd": None, "billing_reconciled": False,
        "source_kind": None, "verified_by_provider_api": False,
        "scope": None, "period": None, "observed_at_utc": None,
        "recorded_at_utc": None, "source_sha256": None,
        "period_in_progress": None, "last_sync_attempt": None,
        "freshness": {"status": "unknown", "age_seconds": None, "reason": reason},
        "authorizes_spend": False,
    }


def provider_report(provider, snapshot, *, now, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Allowlist a normalized receipt, never arbitrary imported/account fields."""
    if snapshot is None:
        return _unknown_provider(provider, "no_provider_spend_evidence")
    from .provider_billing import validate_snapshot

    try:
        normalized = validate_snapshot(snapshot)
        if normalized["provider"] != provider:
            raise ValueError("provider identity mismatch")
    except (ValueError, KeyError, TypeError):
        return _unknown_provider(provider, "invalid_provider_spend_evidence")
    scope = normalized["scope"]
    period = normalized["period"]
    return {
        "provider": provider, "available": True, "reason": None,
        "reported_spend_usd": normalized["spend_usd"],
        "monthly_reference_usd": normalized["monthly_reference_usd"],
        "reference_kind": normalized["reference_kind"],
        "provider_credit_balance_usd": None, "provider_budget_remaining_usd": None,
        "repository_attributed_spend_usd": None, "billing_reconciled": False,
        "source_kind": normalized["source_kind"],
        "verified_by_provider_api": normalized["verified_by_provider_api"],
        "scope": {"level": scope["level"], "project_ids": list(scope["project_ids"]), "attribution": "unassigned"},
        "period": {"start_utc": period["start_utc"], "end_utc": period["end_utc"], "label": period["label"]},
        "period_in_progress": _aware(period["end_utc"]) > now if period["end_utc"] else None,
        "observed_at_utc": normalized["observed_at_utc"],
        "recorded_at_utc": normalized["recorded_at_utc"],
        "source_sha256": normalized["source_sha256"],
        "last_sync_attempt": None,
        "freshness": _freshness(normalized["observed_at_utc"], now, max_age_seconds),
        "interpretation": "Reported aggregate spend; not a credit balance, attributed run cost or reconciled invoice.",
        "authorizes_spend": False,
    }


def build_status(project, *, now=None, snapshot_loader=None, sync_attempt_loader=None,
                 max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Inspect the existing ledger/cache without network calls or SQL writes."""
    from .budget_policy import inspect

    now = now or utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("status requires a timezone-aware clock")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 31 * 86400:
        raise ValueError("invalid evidence freshness interval")
    now = now.astimezone(UTC)
    state = inspect(project)
    configured = {"model": project.model_limit_cents, "infrastructure": project.infra_limit_cents}
    policy = state.get("policy")
    local = {
        "source": "local_admission_ledger" if state["ledger_exists"] else "project_profile_no_ledger",
        "currency": "usd", "amount_unit": "whole_cents",
        "configured_limits_cents": configured,
        "limits_cents": {"model": policy["model_limit_cents"], "infrastructure": policy["infra_limit_cents"]} if policy else configured,
        "reserved_cents": {"model": state["allocation_totals"]["model_cents"], "infrastructure": state["allocation_totals"]["infra_cents"]} if state["ledger_exists"] else None,
        "unreserved_cents": {"model": state["remaining_allowances"]["model_cents"], "infrastructure": state["remaining_allowances"]["infra_cents"]} if state["ledger_exists"] else None,
        "ledger_initialized": state["ledger_exists"], "policy_source": state.get("policy_source"),
        "profile_matches_policy": configured == {"model": policy["model_limit_cents"], "infrastructure": policy["infra_limit_cents"]} if policy else None,
        "provider_balance": False, "measures_actual_spend": False,
        "interpretation": "Retained admission reservations, not provider charges. No reservations are released by this report.",
    }
    if snapshot_loader is None:
        from .provider_billing import load_latest_snapshot

        snapshot_loader = load_latest_snapshot
    if sync_attempt_loader is None:
        from .provider_billing import load_latest_sync_attempt

        sync_attempt_loader = load_latest_sync_attempt
    providers = {}
    for provider in PROVIDERS:
        try:
            snapshot = snapshot_loader(project.root, provider)
        except (ValueError, OSError):
            providers[provider] = _unknown_provider(provider, "provider_cache_unavailable_or_invalid")
        else:
            providers[provider] = provider_report(provider, snapshot, now=now, max_age_seconds=max_age_seconds)
        try:
            attempt = sync_attempt_loader(project.root, provider)
            if attempt is not None:
                from .provider_billing import ERROR_CODES

                if not isinstance(attempt, dict) or attempt.get("status") not in {"success", "failed"}:
                    raise ValueError("invalid synchronization attempt")
                recorded = _aware(attempt.get("recorded_at_utc"))
                code = attempt.get("error_code")
                if code is not None and code not in ERROR_CODES:
                    raise ValueError("invalid synchronization result")
                providers[provider]["last_sync_attempt"] = {
                    "status": attempt["status"], "recorded_at_utc": recorded.isoformat(), "error_code": code,
                }
        except (ValueError, TypeError, OSError):
            providers[provider]["last_sync_attempt"] = {
                "status": "unknown", "recorded_at_utc": None, "error_code": "sync_attempt_evidence_invalid",
            }
    return {
        "version": 1, "kind": "budget_status", "repository": project.repository,
        "recorded_at_utc": now.isoformat(), "read_only": True,
        "network_requests_made": 0,
        "local_admission_policy": local, "reported_provider_spend": providers,
        "provider_credit_balance_usd": None, "available_provider_funds_usd": None,
        "repository_actual_cost_usd": None,
        "local_allocations_changed": False, "billing_reconciled": False,
        "live_execution_enabled": project.live_enabled, "authorizes_spend": False,
        "launch_readiness_verified": False,
    }


def _usd(value):
    if value is None:
        return "unknown"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError
        return f"USD {amount:.2f}"
    except (InvalidOperation, ValueError):
        return "unknown"


def format_status(status):
    """Human output always names both the source and the accounting meaning."""
    local = status["local_admission_policy"]
    lines = [f"Budget status — {status['repository']}", "", "LOCAL ADMISSION POLICY (not provider funds or actual spend)"]
    for key, name in (("model", "Model"), ("infrastructure", "Infrastructure")):
        cap = Decimal(local["limits_cents"][key]) / 100
        reserved = Decimal(local["reserved_cents"][key]) / 100 if local["reserved_cents"] is not None else None
        unreserved = Decimal(local["unreserved_cents"][key]) / 100 if local["unreserved_cents"] is not None else None
        lines.append(f"  {name}: local ceiling {_usd(cap)}; reserved {_usd(reserved)}; unreserved local allowance {_usd(unreserved)}")
    if not local["ledger_initialized"]:
        lines.append("  No local ledger exists; no policy or reservations were initialized.")
    if local["profile_matches_policy"] is False:
        lines.append("  Profile limits differ from the stored local policy; review before admission.")
    lines += ["", "REPORTED PROVIDER SPEND (aggregate evidence; not a reconciled invoice)",
              "  Selected evidence is the latest locally recorded receipt, not necessarily the latest spending period."]
    for provider, evidence in status["reported_provider_spend"].items():
        attempt = evidence["last_sync_attempt"]
        if attempt:
            lines.append(f"  {provider} last API sync: {attempt['status']} at {attempt['recorded_at_utc'] or 'unknown'} ({attempt['error_code'] or 'no error'}).")
            if attempt["status"] == "failed":
                lines.append("    The refresh failed; any spend below is retained prior evidence, not a new successful reading.")
        if not evidence["available"]:
            lines.append(f"  {provider}: unknown ({evidence['reason']}).")
            continue
        verification = "provider API response" if evidence["verified_by_provider_api"] else "operator-supplied observation/export; not API verified"
        lines.append(f"  {provider}: reported spend {_usd(evidence['reported_spend_usd'])}; {verification}.")
        lines.append(f"    Source: {evidence['source_kind']}; scope: {evidence['scope']['level']}; repository/run attribution: unassigned.")
        projects = evidence["scope"]["project_ids"]
        if projects:
            lines.append(f"    Provider project IDs: {', '.join(projects)}")
        period = evidence["period"]
        lines.append(f"    Period: {period['label'] or 'unspecified'}; {period['start_utc'] or 'unknown'} → {period['end_utc'] or 'unknown'}")
        lines.append(f"    Observed: {evidence['observed_at_utc'] or 'unknown'}; recorded: {evidence['recorded_at_utc']}; freshness: {evidence['freshness']['status']}.")
        if evidence["period_in_progress"]:
            timing = "includes the current UTC day" if _aware(period["start_utc"]) <= _aware(status["recorded_at_utc"]) else "starts in the future"
            lines.append(f"    Reporting window is ongoing ({timing}). Spend is reported as of observation, not a final invoice.")
        if evidence["monthly_reference_usd"] is not None:
            lines.append(f"    Reported monthly reference: {_usd(evidence['monthly_reference_usd'])}; kind: {evidence['reference_kind'] or 'unknown'}. This does not authorize local spend.")
    lines += ["", "Provider credit balance / available funds: unknown (not returned by this evidence).",
              "Repository/run actual cost: unknown until separately attributed and reconciled.",
              f"Live execution enabled: {str(status['live_execution_enabled']).lower()}; this report authorizes no spending or launch."]
    return "\n".join(lines)


def import_spend(project, path):
    from .provider_billing import import_snapshot

    output = import_snapshot(project.root, path)
    return {"imported": True, "receipt": str(output), "authorizes_spend": False,
            "local_allocations_changed": False}


def sync_spend(project, *, project_ids=(), organization_scope=False, start_time=None, end_time=None, now=None):
    from .provider_billing import record_failure, save_snapshot, sync_openai

    now = now or utc_now()
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("sync requires a timezone-aware clock")
    now = now.astimezone(UTC)
    start_time = start_time if start_time is not None else int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    end_time = end_time if end_time is not None else int((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    try:
        snapshot = sync_openai(start_time=start_time, end_time=end_time,
                               project_ids=tuple(project_ids), organization_scope=organization_scope, now=now)
        output = save_snapshot(project.root, snapshot)
    except (ValueError, OSError) as error:
        code = getattr(error, "code", "provider_sync_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code):
            code = "provider_sync_failed"
        try:
            record_failure(project.root, code, provider="openai")
        except (ValueError, OSError):
            raise ValueError("OpenAI spend synchronization failed; private failure evidence could not be saved. Local allocations are unchanged") from None
        raise ValueError(f"OpenAI spend synchronization failed ({code}); prior evidence and local allocations are unchanged") from None
    return {"synced": True, "receipt": str(output), "source_kind": "openai_costs_api",
            "authorizes_spend": False, "local_allocations_changed": False}


def add_commands(subparsers):
    status = subparsers.add_parser("status", description="Read local reservations and cached provider evidence without a network request.")
    status.add_argument("--project", required=True)
    status.add_argument("--json", action="store_true", help="print the structured status instead of text")
    imported = subparsers.add_parser("import-spend", description="Import an unverified observation/export into private evidence; does not alter local budgets.")
    imported.add_argument("--project", required=True)
    imported.add_argument("--file", required=True)
    synced = subparsers.add_parser("sync-openai", description="Read OpenAI Costs API using OPENAI_ADMIN_KEY; no model or worker calls.")
    synced.add_argument("--project", required=True)
    scope = synced.add_mutually_exclusive_group(required=True)
    scope.add_argument("--openai-project-id", action="append", dest="provider_projects")
    scope.add_argument("--organization-scope", action="store_true")
    synced.add_argument("--start", type=parse_provider_time, help="YYYY-MM-DD in UTC or a timezone-aware ISO timestamp; default UTC month start")
    synced.add_argument("--end", type=parse_provider_time, help="UTC date or aware ISO timestamp; default next UTC midnight, including the ongoing current day")


def run_command(project, args):
    if args.command == "status":
        result = build_status(project)
        print(json.dumps(result, indent=2) if args.json else format_status(result))
    elif args.command == "import-spend":
        print(json.dumps(import_spend(project, args.file), indent=2))
    elif args.command == "sync-openai":
        print(json.dumps(sync_spend(project, project_ids=args.provider_projects or (),
                                    organization_scope=args.organization_scope,
                                    start_time=args.start, end_time=args.end), indent=2))
    else:
        raise ValueError("unknown budget reporting command")
    return 0
