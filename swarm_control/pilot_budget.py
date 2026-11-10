"""Offline policy and integer-money ledger for the bounded Modal pilot.

Only gateway code calls these transitions, under a durable run lock. Values are
plain JSON-compatible data so Modal Dict never unpickles worker-provided objects.
"""

import asyncio
import copy
import hashlib
import hmac
import json
import math
import re
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation

MODEL = "gpt-6.1-sol"
MAX_BUDGET_USD = 18
MAX_BODY_BYTES = 200_000
MAX_INPUT_TOKENS = 240_000  # Below the 272K long-context pricing threshold.
MAX_OUTPUT_TOKENS = 4096
MAX_REQUESTS = 60
MAX_TTL_SECONDS = 1800
MAX_TASKS = 10
# USD per million tokens, with a further 10% allowance. Cached inputs are
# deliberately charged at the highest input/cache-write rate, not discounted.
INPUT_RATE = Decimal("2.50")
OUTPUT_RATE = Decimal("10.00")
PRICE_MARGIN = Decimal("1.10")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
TOKEN_RE = re.compile(r"sk-[a-f0-9]{64}\Z")
TOP_FIELDS = {
    "model",
    "input",
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning",
    "store",
    "stream",
    "include",
    "prompt_cache_key",
    "text",
    "service_tier",
    "max_output_tokens",
    "metadata",
    "temperature",
    "top_p",
    "truncation",
    "previous_response_id",
    "background",
    "client_metadata",
}
COUNT_FIELDS = {
    "model",
    "input",
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning",
    "text",
    "truncation",
}


class PolicyError(ValueError):
    """A safe fixed message/code, never incorporating request values."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def valid_id(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise PolicyError("invalid_identifier")
    return value


def integer(value, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        raise PolicyError("invalid_integer")
    return value


def money(value):
    """Parse a USD limit without binary-float rounding, NaN, or fractional cents."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise PolicyError("invalid_budget")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 < amount <= MAX_BUDGET_USD:
            raise PolicyError("invalid_budget")
        if amount * 100 != (amount * 100).to_integral_value():
            raise PolicyError("budget_requires_whole_cents")
        return int(amount * 1_000_000)
    except InvalidOperation as exc:
        raise PolicyError("invalid_budget") from exc


def usd(micro):
    return round(micro / 1_000_000, 6)


def token_hash(token):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise PolicyError("invalid_token", 401)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def cost_micro(input_tokens, output_tokens):
    integer(input_tokens, maximum=MAX_INPUT_TOKENS)
    integer(output_tokens, maximum=MAX_OUTPUT_TOKENS)
    # $/million tokens is numerically equal to microdollars/token.
    cost = (input_tokens * INPUT_RATE + output_tokens * OUTPUT_RATE) * PRICE_MARGIN
    return int(cost.to_integral_value(rounding="ROUND_CEILING"))


def new_run(run_id, task_ids, model_budget_usd=18, ttl_seconds=1800, *, now):
    valid_id(run_id)
    if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= MAX_TASKS:
        raise PolicyError("invalid_task_count")
    if any(not isinstance(task, str) for task in task_ids):
        raise PolicyError("invalid_identifier")
    if len(set(task_ids)) != len(task_ids):
        raise PolicyError("duplicate_task")
    integer(ttl_seconds, 60, MAX_TTL_SECONDS)
    if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
        raise PolicyError("invalid_timestamp")
    budget = money(model_budget_usd)
    tasks = {}
    tokens = {}
    for task_id in task_ids:
        valid_id(task_id)
        token = "sk-" + secrets.token_hex(32)
        tokens[task_id] = token
        tasks[task_id] = {
            "token_hash": token_hash(token),
            "budget_micro": budget // len(task_ids),
            "committed_micro": 0,
            "retained_micro": 0,
            "reserved_micro": 0,
            "requests": 0,
            "completed": 0,
            "errors": 0,
            "count_requests": 0,
            "count_errors": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "pending": None,
            "last_error": None,
        }
    return {
        "version": 1,
        "run_id": run_id,
        "state": "active",
        "model": MODEL,
        "created_at": now,
        "expires_at": now + ttl_seconds,
        "budget_micro": budget,
        "tasks": tasks,
        "rate_limits": {},
    }, tokens


def check_active(run, now, stopped=False):
    if stopped or run["state"] != "active":
        raise PolicyError("run_stopped", 403)
    if now >= run["expires_at"]:
        raise PolicyError("run_expired", 403)


def charged(task):
    return sum(
        task[key] for key in ("committed_micro", "retained_micro", "reserved_micro")
    )


def reserve(run, task_id, input_upper, output_limit, *, now, stopped=False):
    check_active(run, now, stopped)
    task = run["tasks"].get(task_id)
    if task is None:
        raise PolicyError("invalid_token", 401)
    if task["pending"] is not None:
        raise PolicyError("task_inflight", 409)
    if task["requests"] >= MAX_REQUESTS:
        raise PolicyError("request_limit", 402)
    amount = cost_micro(input_upper, output_limit)
    total = sum(charged(item) for item in run["tasks"].values())
    if (
        amount > task["budget_micro"] - charged(task)
        or amount > run["budget_micro"] - total
    ):
        raise PolicyError("budget_exhausted", 402)
    pending = {
        "id": secrets.token_hex(16),
        "reserved_micro": amount,
        "input_upper": input_upper,
        "output_limit": output_limit,
        "created_at": now,
        "counted_input": None,
    }
    task["pending"] = pending
    task["requests"] += 1
    task["count_requests"] += 1
    task["reserved_micro"] += amount
    return copy.deepcopy(pending)


def counted(run, task_id, request_id, count):
    """Persist a verified preflight count before allowing inference."""
    task = run["tasks"][task_id]
    pending = task["pending"]
    if pending is None or pending["id"] != request_id:
        raise PolicyError("reservation_mismatch", 409)
    if type(count) is not int or not 0 <= count <= pending["input_upper"]:
        task["count_errors"] += 1
        raise PolicyError("input_count_rejected", 413)
    pending["counted_input"] = count


def usage_cost(event, pending):
    """Only a complete, internally consistent provider event permits a refund."""
    try:
        if not isinstance(event, dict) or event.get("type") != "response.completed":
            return None
        response = event["response"]
        if response["status"] != "completed" or response["model"] != MODEL:
            return None
        if response.get("service_tier", "default") != "default":
            return None
        usage = response["usage"]
        inputs = integer(usage["input_tokens"], maximum=pending["input_upper"])
        outputs = integer(usage["output_tokens"], maximum=pending["output_limit"])
        total = integer(
            usage["total_tokens"], maximum=MAX_INPUT_TOKENS + MAX_OUTPUT_TOKENS
        )
        if total != inputs + outputs:
            return None
        counted_input = pending["counted_input"]
        if counted_input is None or inputs < counted_input:
            return None
        for detail_key, total in (
            ("input_tokens_details", inputs),
            ("output_tokens_details", outputs),
        ):
            details = usage.get(detail_key, {})
            if not isinstance(details, dict):
                return None
            for value in details.values():
                if value is not None:
                    integer(value, maximum=total)
        return cost_micro(inputs, outputs), inputs, outputs
    except (KeyError, TypeError, PolicyError):
        return None


def finish(run, task_id, request_id, event=None, *, error="unknown_usage"):
    task = run["tasks"][task_id]
    pending = task["pending"]
    # Duplicate completion is harmless; an older completion cannot refund a new call.
    if pending is None or pending["id"] != request_id:
        return False
    verified = usage_cost(event, pending)
    task["reserved_micro"] -= pending["reserved_micro"]
    if verified is None:
        task["retained_micro"] += pending["reserved_micro"]
        task["errors"] += 1
        task["last_error"] = error
    else:
        amount, inputs, outputs = verified
        task["committed_micro"] += amount
        task["input_tokens"] += inputs
        task["output_tokens"] += outputs
        task["completed"] += 1
    task["pending"] = None
    return True


def run_report(run, *, now, stopped=False, locked=False):
    tasks = {}
    for task_id, item in run["tasks"].items():
        tasks[task_id] = {
            key: item[key]
            for key in (
                "token_hash",
                "requests",
                "completed",
                "errors",
                "count_requests",
                "count_errors",
                "input_tokens",
                "output_tokens",
                "last_error",
            )
        }
        for field in ("budget", "committed", "retained", "reserved"):
            tasks[task_id][field + "_usd"] = usd(item[field + "_micro"])
        tasks[task_id]["maximum_charged_usd"] = usd(charged(item))
        tasks[task_id]["inflight"] = item["pending"] is not None
    status = (
        "stopped"
        if stopped
        else ("expired" if now >= run["expires_at"] else run["state"])
    )
    return {
        "run_id": run["run_id"],
        "model": MODEL,
        "state": status,
        "created_at": run["created_at"],
        "expires_at": run["expires_at"],
        "budget_usd": usd(run["budget_micro"]),
        "maximum_charged_usd": usd(
            sum(charged(task) for task in run["tasks"].values())
        ),
        "committed_usd": usd(
            sum(task["committed_micro"] for task in run["tasks"].values())
        ),
        "retained_usd": usd(
            sum(task["retained_micro"] for task in run["tasks"].values())
        ),
        "reserved_usd": usd(
            sum(task["reserved_micro"] for task in run["tasks"].values())
        ),
        "ledger_locked": locked,
        "tasks": tasks,
        "rate_limits": run["rate_limits"],
        "billing_verified": False,
    }


def _keys(value, allowed):
    if not isinstance(value, dict) or set(value) - allowed:
        raise PolicyError("unsupported_request_shape")


def _text_content(content):
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise PolicyError("text_input_required")
    for part in content:
        _keys(part, {"type", "text", "refusal", "annotations", "logprobs"})
        kind = part.get("type")
        if not isinstance(kind, str) or kind not in {
            "input_text",
            "output_text",
            "refusal",
        }:
            raise PolicyError("text_input_required")
        if not isinstance(part.get("refusal" if kind == "refusal" else "text"), str):
            raise PolicyError("text_input_required")
        if part.get("annotations") or part.get("logprobs"):
            raise PolicyError("unsupported_annotations")


def _inputs(value):
    if isinstance(value, str):
        return 0
    if not isinstance(value, list) or len(value) > 512:
        raise PolicyError("invalid_input")
    tool_count = 0
    for item in value:
        if not isinstance(item, dict):
            raise PolicyError("invalid_input")
        kind = item.get("type", "message")
        if not isinstance(kind, str):
            raise PolicyError("invalid_input")
        if kind == "message":
            _keys(item, {"type", "id", "role", "content", "status", "phase"})
            if item.get("role") not in ("user", "assistant", "system", "developer"):
                raise PolicyError("invalid_role")
            _text_content(item.get("content"))
        elif kind in {"function_call", "custom_tool_call"}:
            _keys(
                item,
                {
                    "type",
                    "id",
                    "call_id",
                    "name",
                    "arguments",
                    "input",
                    "status",
                    "namespace",
                },
            )
            if not isinstance(
                item.get("arguments" if kind == "function_call" else "input"), str
            ):
                raise PolicyError("invalid_tool_input")
            _call_namespace(item)
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            _keys(
                item, {"type", "id", "call_id", "output", "status", "name", "namespace"}
            )
            _text_content(item.get("output"))
            _call_namespace(item)
        elif kind == "additional_tools":
            _keys(item, {"type", "id", "role", "tools"})
            if item.get("role") != "developer":
                raise PolicyError("invalid_tools_role")
            tool_count += _tools(item.get("tools"))
        elif kind == "reasoning":
            _keys(
                item,
                {"type", "id", "summary", "content", "encrypted_content", "status"},
            )
            for field in ("summary", "content"):
                if item.get(field) is not None:
                    if not isinstance(item[field], list):
                        raise PolicyError("invalid_reasoning")
                    for part in item[field]:
                        _keys(part, {"type", "text"})
                        if part.get("type") not in (
                            "summary_text",
                            "reasoning_text",
                        ) or not isinstance(part.get("text"), str):
                            raise PolicyError("invalid_reasoning")
            if item.get("encrypted_content") is not None and not isinstance(
                item["encrypted_content"], str
            ):
                raise PolicyError("invalid_reasoning")
        else:
            raise PolicyError("unsupported_input_type")
    return tool_count


def _call_namespace(item):
    if item.get("namespace") not in (None, "functions", "clock"):
        raise PolicyError("invalid_namespace")
    if isinstance(item.get("name"), str) and item["name"].startswith("collaboration."):
        raise PolicyError("invalid_namespace")


def _without_collaboration(tools):
    if not isinstance(tools, list):
        return tools  # The regular validator rejects malformed definitions.
    return [
        tool
        for tool in tools
        if not (
            isinstance(tool, dict)
            and tool.get("type") == "namespace"
            and tool.get("name") == "collaboration"
        )
    ]


def _tools(tools, *, nested=False):
    if not isinstance(tools, list) or len(tools) > 64:
        raise PolicyError("invalid_tools")
    total = 0
    for tool in tools:
        if not isinstance(tool, dict):
            raise PolicyError("local_tools_only")
        if tool.get("type") == "namespace" and not nested:
            _keys(tool, {"type", "name", "description", "tools"})
            if tool.get("name") not in ("functions", "clock") or not isinstance(
                tool.get("description"), str
            ):
                raise PolicyError("invalid_namespace")
            if not tool.get("tools"):
                raise PolicyError("invalid_namespace")
            total += _tools(tool["tools"], nested=True)
            continue
        if tool.get("type") not in ("function", "custom"):
            raise PolicyError("local_tools_only")
        if tool["type"] == "function":
            _keys(tool, {"type", "name", "description", "parameters", "strict"})
        else:
            _keys(tool, {"type", "name", "description", "format"})
        if not isinstance(tool.get("name"), str):
            raise PolicyError("invalid_tool_name")
        total += 1
    if total > 64:
        raise PolicyError("invalid_tool_count")
    return total


def _node_count(value, depth=0):
    if depth > 32:
        raise PolicyError("request_too_deep", 413)
    if isinstance(value, dict):
        return 1 + sum(_node_count(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_node_count(item, depth + 1) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise PolicyError("invalid_json")
    return 1


def prepare_request(raw):
    """Allow only bounded stateless text Responses and locally executed tools."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY_BYTES:
        raise PolicyError("request_too_large", 413)
    try:
        body = json.loads(
            raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
        nodes = _node_count(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PolicyError("invalid_json") from exc
    _keys(body, TOP_FIELDS)
    if body.get("model", MODEL) != MODEL:
        raise PolicyError("model_not_allowed")
    if (
        body.get("previous_response_id") is not None
        or body.get("background", False) is not False
    ):
        raise PolicyError("stateless_requests_only")
    if body.get("stream", True) is not True:
        raise PolicyError("stream_required")
    # CLI 0.166 may advertise collaboration even with multi_agent disabled.
    # Never expose those definitions to the model for a one-worker ticket.
    if "tools" in body:
        body["tools"] = _without_collaboration(body["tools"])
    if isinstance(body.get("input"), list):
        for item in body["input"]:
            if isinstance(item, dict) and item.get("type") == "additional_tools":
                item["tools"] = _without_collaboration(item.get("tools"))
    if _inputs(body.get("input")) + _tools(body.get("tools", [])) > 64:
        raise PolicyError("invalid_tool_count")
    if body.get("include", []) not in ([], ["reasoning.encrypted_content"]):
        raise PolicyError("unsupported_include")
    if body.get("tool_choice", "auto") not in ("auto", "none", "required"):
        raise PolicyError("unsupported_tool_choice")
    if body.get("reasoning") is not None:
        reasoning = body["reasoning"]
        _keys(reasoning, {"effort", "summary", "context"})
        if reasoning.get("effort", "medium") not in (
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ):
            raise PolicyError("unsupported_reasoning")
        if reasoning.get("summary") not in (None, "auto", "concise", "detailed"):
            raise PolicyError("unsupported_reasoning")
        if reasoning.get("context") not in (None, "auto", "current_turn", "all_turns"):
            raise PolicyError("unsupported_reasoning")
    limit = integer(body.get("max_output_tokens", MAX_OUTPUT_TOKENS), 1)
    body.update(
        model=MODEL,
        stream=True,
        store=False,
        service_tier="default",
        max_output_tokens=min(limit, MAX_OUTPUT_TOKENS),
        truncation="disabled",
    )
    body.pop("previous_response_id", None)
    body.pop("background", None)
    # Session/installation telemetry is not required for inference or accounting.
    body.pop("client_metadata", None)
    try:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except UnicodeError as exc:
        raise PolicyError("invalid_unicode") from exc
    # This is a reservation estimate, not a tokenizer. The provider's exact count
    # MUST be <= this bound before inference. It also bounds opaque reasoning.
    upper = len(encoded) + 32 * nodes + 1024
    if len(encoded) > MAX_BODY_BYTES or upper > MAX_INPUT_TOKENS:
        raise PolicyError("input_bound_exceeded", 413)
    return body, upper


class SSEUsage:
    """Incremental bounded SSE parser; never retains model output beyond one event."""

    def __init__(self):
        self.buffer = b""
        self.event = None
        self.failed = False

    def feed(self, chunk):
        if self.failed:
            return
        self.buffer += chunk
        if len(self.buffer) > 2_000_000:
            self.failed = True
            self.event = None
            self.buffer = b""
            return
        self.buffer = self.buffer.replace(b"\r\n", b"\n")
        while b"\n\n" in self.buffer:
            frame, self.buffer = self.buffer.split(b"\n\n", 1)
            data = b"\n".join(
                line[5:].lstrip(b" ")
                for line in frame.split(b"\n")
                if line.startswith(b"data:")
            )
            if not data or data == b"[DONE]":
                continue
            try:
                event = json.loads(data)
            except (ValueError, UnicodeError):
                self.failed = True
                self.event = None
                return
            if isinstance(event, dict) and event.get("type") == "response.completed":
                if self.event is not None:
                    self.failed = True
                    self.event = None
                    return
                # Retain usage metadata only, never prompts or generated content.
                response = event.get("response", {})
                if not isinstance(response, dict):
                    self.failed = True
                    return
                self.event = {
                    "type": "response.completed",
                    "response": {
                        key: response.get(key)
                        for key in ("status", "model", "usage", "service_tier")
                    },
                }
                if self.event["response"]["service_tier"] is None:
                    self.event["response"]["service_tier"] = "default"


class BudgetService:
    """Single writer with a persistent lock; a lost lock fails closed.

    The store supplies async get/put/pop. put(skip_if_exists=True) must be atomic.
    Locks have no automatic lease stealing: an interrupted write cannot produce
    two writers or resurrect a refunded reservation after container replacement.
    """

    def __init__(self, store):
        self.store = store
        self.local_lock = asyncio.Lock()

    async def create(self, run_id, task_ids, model_budget_usd, ttl_seconds, now):
        run, tokens = new_run(run_id, task_ids, model_budget_usd, ttl_seconds, now=now)
        run["state"] = "initializing"
        if not await self.store.put("run:" + run_id, run, skip_if_exists=True):
            raise PolicyError("run_already_exists", 409)
        for task_id, item in run["tasks"].items():
            await self.store.put("auth:" + item["token_hash"], [run_id, task_id])
        run["state"] = "active"
        await self.store.put("run:" + run_id, run)
        return {**run_report(run, now=now), "tokens": tokens}

    async def authenticate(self, token, now):
        digest = token_hash(token)
        identity = await self.store.get("auth:" + digest)
        if not identity:
            raise PolicyError("invalid_token", 401)
        run_id, task_id = identity
        run = await self.store.get("run:" + run_id)
        if run is None or not hmac.compare_digest(
            run["tasks"][task_id]["token_hash"], digest
        ):
            raise PolicyError("invalid_token", 401)
        check_active(run, now, bool(await self.store.get("stop:" + run_id)))
        return run_id, task_id

    @asynccontextmanager
    async def transaction(self, run_id):
        async with self.local_lock:
            key = "lock:" + run_id
            if not await self.store.put(key, True, skip_if_exists=True):
                raise PolicyError("ledger_locked", 503)
            # Do not release a durable lock after a failed/ambiguous read or
            # write. Recovery is read-only or stop_run, never resetting credits.
            run = await self.store.get("run:" + run_id)
            if run is None:
                await self.store.pop(key)
                raise PolicyError("run_not_found", 404)
            try:
                yield run
            except PolicyError:
                await self.store.pop(key)
                raise
            await self.store.put("run:" + run_id, run)
            await self.store.pop(key)

    async def reserve(self, run_id, task_id, input_upper, output_limit, now):
        async with self.transaction(run_id) as run:
            pending = reserve(
                run,
                task_id,
                input_upper,
                output_limit,
                now=now,
                stopped=bool(await self.store.get("stop:" + run_id)),
            )
        return pending

    async def record_count(self, run_id, task_id, request_id, count, now):
        error = None
        async with self.transaction(run_id) as run:
            check_active(run, now, bool(await self.store.get("stop:" + run_id)))
            try:
                counted(run, task_id, request_id, count)
            except PolicyError as exc:
                error = exc
        if error is not None:
            raise error

    async def finish(
        self, run_id, task_id, request_id, event=None, error="unknown_usage"
    ):
        async with self.transaction(run_id) as run:
            finish(run, task_id, request_id, event, error=error)

    async def rate_limits(self, run_id, headers):
        allowed = {
            "x-ratelimit-limit-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-reset-requests",
            "x-ratelimit-reset-tokens",
        }
        safe = {
            key: value
            for key, value in headers.items()
            if key in allowed
            and isinstance(value, str)
            and re.fullmatch(r"[0-9a-zA-Z. -]{1,80}", value)
        }
        if safe:
            async with self.transaction(run_id) as run:
                run["rate_limits"].update(safe)

    async def status(self, run_id, now):
        valid_id(run_id)
        run = await self.store.get("run:" + run_id)
        if run is None:
            raise PolicyError("run_not_found", 404)
        return run_report(
            run,
            now=now,
            stopped=bool(await self.store.get("stop:" + run_id)),
            locked=bool(await self.store.get("lock:" + run_id)),
        )

    async def stop(self, run_id, now):
        valid_id(run_id)
        # This separate monotonic key cannot be overwritten by a request's
        # in-progress ledger transaction. In-flight reservations remain charged.
        await self.store.put("stop:" + run_id, True)
        return await self.status(run_id, now)
