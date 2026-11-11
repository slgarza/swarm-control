"""Provider-reported spend evidence, isolated from local admission accounting.

Only the explicitly invoked OpenAI synchronizer makes network calls. All evidence
stays in the target's ignored state directory; no function changes a budget ledger,
grants spend authority, infers account credit or attributes an account total to a run.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import httpx

from .pilot_state import common_state

API_URL = "https://api.openai.com/v1/organization/costs"
MAX_FILE_BYTES = 1_000_000
MAX_PAGE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 10_000_000
MAX_PAGES = 32
MAX_RECORDS = 100_000
FIELDS = {
    "version", "provider", "source_kind", "recorded_at_utc", "observed_at_utc",
    "period", "scope", "spend_usd", "monthly_reference_usd", "reference_kind",
    "source_sha256", "verified_by_provider_api", "billing_reconciled",
    "authorizes_spend", "provider_credit_balance_usd",
}
ERROR_CODES = {
    "admin_key_missing", "admin_key_invalid", "provider_permission_denied",
    "provider_rate_limited", "provider_request_failed", "provider_response_invalid",
    "provider_response_too_large", "provider_pagination_invalid", "provider_scope_mismatch",
    "provider_period_mismatch", "provider_currency_unsupported", "provider_sync_failed",
}


class BillingError(ValueError):
    """A fixed code that never includes credentials, headers or provider errors."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Snapshot(dict):
    """Keep original source bytes in memory until explicitly saved privately."""

    def __init__(self, value, source_bytes):
        super().__init__(value)
        self.source_bytes = source_bytes


def require(condition, code="billing_snapshot_invalid"):
    if not condition:
        raise BillingError(code)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _timestamp(value):
    require(isinstance(value, str) and len(value) <= 40)
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.utcoffset() is not None)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise BillingError("billing_timestamp_invalid") from None


def _utc(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _money(value):
    require(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool))
    try:
        number = Decimal(value)
        require(number.is_finite() and number.copy_abs() <= Decimal(1000000000000))
        require(number.as_tuple().exponent >= -30)
    except (InvalidOperation, ValueError, TypeError):
        raise BillingError("billing_amount_invalid") from None
    return format(number, "f")


def _label(value, maximum=256):
    require(isinstance(value, str) and 0 < len(value) <= maximum
            and all(ord(character) >= 32 and ord(character) != 127 for character in value))
    return value


def _project_ids(values):
    require(isinstance(values, (list, tuple)) and len(values) <= 100)
    require(all(isinstance(value, str) and re.fullmatch(r"proj[_-][A-Za-z0-9_-]{1,128}", value)
                for value in values))
    require(len(set(values)) == len(values))
    return sorted(values)


def validate_snapshot(snapshot):
    """Validate normalized evidence; this does not authenticate a local cache."""
    try:
        return _validate_snapshot(snapshot)
    except BillingError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        raise BillingError("billing_snapshot_invalid") from None


def _validate_snapshot(snapshot):
    require(isinstance(snapshot, dict) and set(snapshot) == FIELDS)
    require(type(snapshot["version"]) is int and snapshot["version"] == 1)
    require(isinstance(snapshot["provider"], str) and snapshot["provider"] in {"openai", "modal"})
    require(isinstance(snapshot["source_kind"], str)
            and snapshot["source_kind"] in {"openai_costs_api", "dashboard_observation", "provider_export"})
    result = dict(snapshot)
    result["recorded_at_utc"] = _utc(_timestamp(snapshot["recorded_at_utc"]))
    observed = snapshot["observed_at_utc"]
    result["observed_at_utc"] = _utc(_timestamp(observed)) if observed is not None else None
    period = snapshot["period"]
    require(isinstance(period, dict) and set(period) == {"start_utc", "end_utc", "label"})
    start, end = period["start_utc"], period["end_utc"]
    require((start is None) == (end is None))
    if start is not None:
        start, end = _timestamp(start), _timestamp(end)
        require(start < end)
    result["period"] = {"start_utc": _utc(start) if start else None,
                        "end_utc": _utc(end) if end else None,
                        "label": _label(period["label"]) if period["label"] is not None else None}
    scope = snapshot["scope"]
    require(isinstance(scope, dict) and set(scope) == {"level", "project_ids", "attribution"})
    require(isinstance(scope["level"], str) and scope["level"] in {"organization", "project"}
            and scope["attribution"] == "unassigned")
    projects = _project_ids(scope["project_ids"])
    require(bool(projects) == (scope["level"] == "project"))
    result["scope"] = {"level": scope["level"], "project_ids": projects, "attribution": "unassigned"}
    result["spend_usd"] = _money(snapshot["spend_usd"])
    reference = snapshot["monthly_reference_usd"]
    result["monthly_reference_usd"] = _money(reference) if reference is not None else None
    if reference is not None:
        require(Decimal(result["monthly_reference_usd"]) >= 0 and snapshot["reference_kind"] is not None)
    if snapshot["reference_kind"] is not None:
        _label(snapshot["reference_kind"], 64)
    require(isinstance(snapshot["source_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", snapshot["source_sha256"]))
    require(type(snapshot["verified_by_provider_api"]) is bool)
    if snapshot["source_kind"] == "openai_costs_api":
        require(snapshot["provider"] == "openai" and snapshot["verified_by_provider_api"]
                and observed is not None and start is not None)
    else:
        require(snapshot["verified_by_provider_api"] is False)
    require(snapshot["billing_reconciled"] is False and snapshot["authorizes_spend"] is False
            and snapshot["provider_credit_balance_usd"] is None)
    return result


def sync_openai(*, start_time, end_time, project_ids=(), organization_scope=False,
                admin_key=None, transport=None, now=None):
    """Fetch only reported USD costs. The caller explicitly selects its scope.

    Costs API data may lag and is not a final invoice. No balance or rate-limit
    endpoint is called. No retries are made and an incomplete result is rejected.
    """
    require(type(start_time) is int and type(end_time) is int
            and 0 <= start_time < end_time <= 253402214400
            and end_time - start_time <= 366 * 86400, "provider_period_mismatch")
    projects = _project_ids(project_ids)
    require(type(organization_scope) is bool and bool(projects) != organization_scope,
            "provider_scope_mismatch")
    key = os.environ.get("OPENAI_ADMIN_KEY") if admin_key is None else admin_key
    require(bool(key), "admin_key_missing")
    require(isinstance(key, str) and 1 <= len(key) <= 512
            and all(33 <= ord(character) <= 126 for character in key), "admin_key_invalid")
    now = now or datetime.now(UTC)
    require(isinstance(now, datetime) and now.utcoffset() is not None)
    query = [("start_time", str(start_time)), ("end_time", str(end_time)),
             ("bucket_width", "1d"), ("limit", "180"), ("group_by", "project_id")]
    query += [("project_ids", project) for project in projects]
    source_pages, cursors, buckets, records = [], set(), set(), 0
    total, total_bytes, cursor = Decimal(0), 0, None
    try:
        with httpx.Client(transport=transport, timeout=10, follow_redirects=False, trust_env=False) as client:
            for _ in range(MAX_PAGES):
                params = query + ([("page", cursor)] if cursor else [])
                with client.stream("GET", API_URL, params=params,
                                   headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}) as response:
                    if response.status_code in {401, 403}:
                        raise BillingError("provider_permission_denied")
                    if response.status_code == 429:
                        raise BillingError("provider_rate_limited")
                    require(response.status_code == 200, "provider_request_failed")
                    chunks, page_bytes = [], 0
                    for chunk in response.iter_bytes():
                        page_bytes += len(chunk)
                        total_bytes += len(chunk)
                        require(page_bytes <= MAX_PAGE_BYTES and total_bytes <= MAX_TOTAL_BYTES,
                                "provider_response_too_large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                source_pages.append(raw)
                try:
                    data = json.loads(raw, parse_float=Decimal)
                except (ValueError, UnicodeError):
                    raise BillingError("provider_response_invalid") from None
                require(isinstance(data, dict) and data.get("object") == "page"
                        and isinstance(data.get("data"), list) and len(data["data"]) <= 180
                        and type(data.get("has_more")) is bool, "provider_response_invalid")
                for bucket in data["data"]:
                    require(isinstance(bucket, dict) and bucket.get("object") == "bucket"
                            and type(bucket.get("start_time")) is int and type(bucket.get("end_time")) is int
                            and isinstance(bucket.get("results"), list), "provider_response_invalid")
                    interval = bucket["start_time"], bucket["end_time"]
                    require(start_time <= interval[0] < interval[1] <= end_time,
                            "provider_period_mismatch")
                    require(interval not in buckets, "provider_pagination_invalid")
                    require(not any(interval[0] < old[1] and old[0] < interval[1] for old in buckets),
                            "provider_period_mismatch")
                    buckets.add(interval)
                    groups = set()
                    for record in bucket["results"]:
                        records += 1
                        require(records <= MAX_RECORDS and isinstance(record, dict)
                                and record.get("object") == "organization.costs.result",
                                "provider_response_invalid")
                        project = record.get("project_id")
                        require(project is None or isinstance(project, str), "provider_response_invalid")
                        require(not projects or project in projects, "provider_scope_mismatch")
                        require(project not in groups, "provider_pagination_invalid")
                        groups.add(project)
                        amount = record.get("amount")
                        require(isinstance(amount, dict) and amount.get("currency") == "usd",
                                "provider_currency_unsupported")
                        require(type(amount.get("value")) in {int, Decimal}, "provider_response_invalid")
                        try:
                            with localcontext() as context:
                                context.prec = 64
                                total += Decimal(_money(amount["value"]))
                        except BillingError:
                            raise BillingError("provider_response_invalid") from None
                next_page = data.get("next_page")
                if not data["has_more"]:
                    require(next_page is None, "provider_pagination_invalid")
                    break
                require(isinstance(next_page, str) and 1 <= len(next_page) <= 512
                        and next_page not in cursors and bool(data["data"]), "provider_pagination_invalid")
                cursors.add(next_page)
                cursor = next_page
            else:
                raise BillingError("provider_pagination_invalid")
    except httpx.HTTPError:
        raise BillingError("provider_request_failed") from None
    source_bytes = _json({"endpoint": API_URL, "start_time": start_time, "end_time": end_time,
                          "project_ids": projects, "organization_scope": organization_scope,
                          "responses_base64": [base64.b64encode(page).decode("ascii") for page in source_pages]})
    result = {"version": 1, "provider": "openai", "source_kind": "openai_costs_api",
              "recorded_at_utc": _utc(now), "observed_at_utc": _utc(now),
              "period": {"start_utc": _utc(datetime.fromtimestamp(start_time, UTC)),
                         "end_utc": _utc(datetime.fromtimestamp(end_time, UTC)), "label": "queried UTC interval"},
              "scope": {"level": "project" if projects else "organization", "project_ids": projects,
                        "attribution": "unassigned"}, "spend_usd": _money(total),
              "monthly_reference_usd": None, "reference_kind": None, "source_sha256": _sha(source_bytes),
              "verified_by_provider_api": True, "billing_reconciled": False,
              "authorizes_spend": False, "provider_credit_balance_usd": None}
    return _Snapshot(validate_snapshot(result), source_bytes)


def _directory(root, create=False):
    state = common_state(Path(root).resolve())
    billing = state / "billing"
    require(state.resolve() == state and not state.is_symlink(), "billing_directory_unsafe")
    for directory in (state, billing):
        if not directory.exists():
            if not create:
                return billing
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        require(directory.is_dir() and not directory.is_symlink(), "billing_directory_unsafe")
    if billing.exists():
        require(billing.stat().st_mode & 0o077 == 0, "billing_directory_must_be_private")
    if create:
        ignored = subprocess.run(["git", "check-ignore", "--quiet", "--no-index", "--", str(billing)],
                                 cwd=state.parent, capture_output=True, check=False)
        require(ignored.returncode == 0, "billing_directory_not_ignored")
    return billing


def _read(path, maximum=MAX_FILE_BYTES, *, private=False):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= maximum,
            "billing_file_invalid")
    if private:
        require(path.stat().st_mode & 0o077 == 0, "billing_file_must_be_private")
    return path.read_bytes()


def _append(path, raw):
    temporary = path.parent / f".pending-{uuid.uuid4()}"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publish without replacing an earlier receipt or source artifact.
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        raise
    except OSError:
        raise BillingError("billing_storage_unavailable") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def save_snapshot(project_root, snapshot, *, now=None):
    value = validate_snapshot(snapshot)
    # recorded_at is local ingestion time, never an imported claim of freshness.
    recorded = now or datetime.now(UTC)
    require(isinstance(recorded, datetime) and recorded.utcoffset() is not None)
    value["recorded_at_utc"] = _utc(recorded)
    directory = _directory(project_root, create=True)
    source = getattr(snapshot, "source_bytes", None)
    if source is None:
        source_file = directory / f"source-{value['source_sha256']}.json"
        if source_file.exists():
            source = _read(source_file, MAX_TOTAL_BYTES * 2, private=True)
            require(_sha(source) == value["source_sha256"], "billing_source_digest_mismatch")
        else:
            require(not value["verified_by_provider_api"], "billing_source_evidence_missing")
            source = _json(value)
            value["source_sha256"] = _sha(source)
    require(isinstance(source, bytes) and len(source) <= MAX_TOTAL_BYTES * 2
            and _sha(source) == value["source_sha256"], "billing_source_digest_mismatch")
    source_file = directory / f"source-{value['source_sha256']}.json"
    if source_file.exists():
        require(_sha(_read(source_file, MAX_TOTAL_BYTES * 2, private=True)) == value["source_sha256"],
                "billing_source_digest_mismatch")
    else:
        try:
            _append(source_file, source)
        except FileExistsError:
            require(_sha(_read(source_file, MAX_TOTAL_BYTES * 2, private=True)) == value["source_sha256"],
                    "billing_source_digest_mismatch")
    path = directory / f"{value['provider']}-{uuid.uuid4()}.json"
    receipt = {"version": 1, "kind": "provider_spend_snapshot_receipt", "snapshot": value,
               "snapshot_sha256": _sha(_json(value))}
    _append(path, _json(receipt) + b"\n")
    return path


def import_snapshot(project_root, path):
    raw = _read(Path(path))
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BillingError("billing_import_invalid") from None
    require(isinstance(value, dict) and isinstance(value.get("source_kind"), str)
            and value.get("source_kind") in {"dashboard_observation", "provider_export"}
            and value.get("verified_by_provider_api") is False, "billing_import_cannot_claim_api_verification")
    value["source_sha256"] = _sha(raw)
    return save_snapshot(project_root, _Snapshot(validate_snapshot(value), raw))


def _load_receipt(path):
    try:
        receipt = json.loads(_read(path, private=True))
        require(isinstance(receipt, dict) and set(receipt) == {"version", "kind", "snapshot", "snapshot_sha256"}
                and type(receipt["version"]) is int and receipt["version"] == 1
                and receipt["kind"] == "provider_spend_snapshot_receipt", "billing_cached_snapshot_invalid")
        require(_sha(_json(receipt["snapshot"])) == receipt["snapshot_sha256"],
                "billing_snapshot_digest_mismatch")
        return validate_snapshot(receipt["snapshot"])
    except BillingError:
        raise
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise BillingError("billing_cached_snapshot_invalid") from None


def load_latest_snapshot(project_root, provider="openai"):
    """Select latest locally ingested evidence, not latest covered spending period."""
    require(isinstance(provider, str) and provider in {"openai", "modal"}, "billing_provider_invalid")
    directory = _directory(project_root)
    if not directory.exists():
        return None
    values = []
    for index, path in enumerate(sorted(directory.iterdir())):
        require(index < 10_000, "billing_cache_too_large")
        if not path.name.startswith(provider + "-") or not path.name.endswith(".json"):
            continue
        value = _load_receipt(path)
        require(value["provider"] == provider, "billing_cached_snapshot_invalid")
        source_file = directory / f"source-{value['source_sha256']}.json"
        require(_sha(_read(source_file, MAX_TOTAL_BYTES * 2, private=True)) == value["source_sha256"],
                "billing_source_digest_mismatch")
        values.append(value)
    return max(values, key=lambda value: (_timestamp(value["recorded_at_utc"]),
                                         value["source_sha256"])) if values else None


def load_latest_sync_attempt(project_root, provider="openai"):
    """Show a failed refresh even when an older successful snapshot remains."""
    require(isinstance(provider, str) and provider in {"openai", "modal"}, "billing_provider_invalid")
    directory = _directory(project_root)
    if not directory.exists():
        return None
    attempts = []
    for index, path in enumerate(sorted(directory.iterdir())):
        require(index < 10_000, "billing_cache_too_large")
        if path.name.startswith(f"failure-{provider}-") and path.name.endswith(".json"):
            try:
                value = json.loads(_read(path, private=True))
            except (ValueError, UnicodeError):
                raise BillingError("billing_cached_attempt_invalid") from None
            require(isinstance(value, dict) and value.get("provider") == provider
                    and value.get("kind") == "provider_spend_sync_failure"
                    and isinstance(value.get("code"), str) and value.get("code") in ERROR_CODES
                    and value.get("authorizes_spend") is False,
                    "billing_cached_attempt_invalid")
            recorded = _utc(_timestamp(value.get("recorded_at_utc")))
            attempts.append({"status": "failed", "recorded_at_utc": recorded, "error_code": value["code"]})
        elif path.name.startswith(provider + "-") and path.name.endswith(".json"):
            value = _load_receipt(path)
            if value["source_kind"] != "openai_costs_api":
                continue
            require(value["provider"] == provider, "billing_cached_snapshot_invalid")
            source_file = directory / f"source-{value['source_sha256']}.json"
            require(_sha(_read(source_file, MAX_TOTAL_BYTES * 2, private=True)) == value["source_sha256"],
                    "billing_source_digest_mismatch")
            attempts.append({"status": "success", "recorded_at_utc": value["recorded_at_utc"], "error_code": None})
    return max(attempts, key=lambda value: (_timestamp(value["recorded_at_utc"]),
                                          value["status"] == "failed")) if attempts else None


def record_failure(project_root, code, provider="openai"):
    require(isinstance(provider, str) and provider in {"openai", "modal"}, "billing_provider_invalid")
    if not isinstance(code, str) or code not in ERROR_CODES:
        code = "provider_sync_failed"
    value = {"kind": "provider_spend_sync_failure", "provider": provider, "code": code,
             "recorded_at_utc": _utc(datetime.now(UTC)), "authorizes_spend": False,
             "billing_reconciled": False}
    path = _directory(project_root, create=True) / f"failure-{provider}-{uuid.uuid4()}.json"
    _append(path, _json(value) + b"\n")
    return path
