"""Offline budget, persistence, auth, request-policy, and SSE regression tests."""

import asyncio
import copy
import importlib.util
import json
import os
import time
import unittest
from unittest.mock import patch

from swarm_control.pilot_budget import (
    MAX_BODY_BYTES,
    MAX_OUTPUT_TOKENS,
    MAX_REQUESTS,
    MODEL,
    BudgetService,
    PolicyError,
    SSEUsage,
    charged,
    cost_micro,
    counted,
    finish,
    money,
    new_run,
    prepare_request,
    reserve,
    run_report,
    token_hash,
)


def payload(**changes):
    return json.dumps(
        {"model": MODEL, "input": "Write one unit test.", **changes}
    ).encode()


def completed(inputs=100, outputs=20, **changes):
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "model": MODEL,
            "service_tier": "default",
            "usage": {
                "input_tokens": inputs,
                "output_tokens": outputs,
                "total_tokens": inputs + outputs,
                "input_tokens_details": {
                    "cached_tokens": inputs // 2,
                    "cache_write_tokens": inputs // 2,
                },
                "output_tokens_details": {"reasoning_tokens": outputs // 2},
            },
            **changes,
        },
    }


def codex_wire_input(after_tool=False):
    """Synthetic fixture of CLI 0.166 field shapes; no captured user prompts."""
    items = [
        {
            "type": "additional_tools",
            "id": "tools_fixture",
            "role": "developer",
            "tools": [
                {
                    "type": "namespace",
                    "name": "functions",
                    "description": "Local tools",
                    "tools": [
                        {
                            "type": "custom",
                            "name": "exec",
                            "description": "Execute local JavaScript",
                            "format": {"type": "text"},
                        },
                        {
                            "type": "function",
                            "name": "wait",
                            "parameters": {},
                            "strict": False,
                        },
                    ],
                },
                {
                    "type": "namespace",
                    "name": "clock",
                    "description": "Clock",
                    "tools": [
                        {
                            "type": "function",
                            "name": "sleep",
                            "parameters": {},
                            "strict": False,
                        },
                    ],
                },
                {
                    "type": "namespace",
                    "name": "collaboration",
                    "description": "Disabled",
                    "tools": [
                        {"type": "function", "name": "spawn_agent", "parameters": {}},
                    ],
                },
            ],
        },
        {
            "type": "message",
            "id": "msg_fixture",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Execute printf, then READY."},
            ],
        },
    ]
    if after_tool:
        items.extend(
            [
                {
                    "type": "custom_tool_call",
                    "id": "ctc_fixture",
                    "call_id": "call_fixture",
                    "name": "exec",
                    "namespace": "functions",
                    "input": "text('gateway-ready');",
                },
                {
                    "type": "custom_tool_call_output",
                    "id": "out_fixture",
                    "call_id": "call_fixture",
                    "output": "gateway-ready",
                },
            ]
        )
    return items


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.run, self.tokens = new_run("pilot-1", ["A", "B"], now=100)

    def test_money_rejects_invalid_and_fractional_cents(self):
        for value in (
            True,
            None,
            "NaN",
            "Infinity",
            -1,
            0,
            180.01,
            "1.001",
            [],
            "secret",
        ):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                money(value)
        self.assertEqual(money("18.00"), 18_000_000)
        self.assertEqual(money(0.25), 250_000)
        self.assertEqual(money("180.00"), 180_000_000)

    def test_tokens_random_hashed_and_absent_from_report(self):
        self.assertNotEqual(self.tokens["A"], self.tokens["B"])
        report = json.dumps(run_report(self.run, now=101))
        for task, token in self.tokens.items():
            self.assertNotIn(token, report)
            self.assertEqual(self.run["tasks"][task]["token_hash"], token_hash(token))
        for token in (None, "sk-short", "x" * 10000):
            with self.assertRaises(PolicyError):
                token_hash(token)

    def test_invalid_run_inputs(self):
        for tasks in (["A", "A"], [], [None], ["../x"], [str(i) for i in range(101)]):
            with self.assertRaises(PolicyError):
                new_run("pilot", tasks, now=100)
        for ttl in (True, 59, 3601, -1):
            with self.assertRaises(PolicyError):
                new_run("pilot", ["A"], ttl_seconds=ttl, now=100)

    def test_opt_in_long_ttl_preserves_budget_and_expires_at_actual_deadline(self):
        default, _ = new_run("default-window", ["A"], now=100)
        self.assertEqual(default["expires_at"], 1900)
        extended, _ = new_run("extended-window", ["A"], model_budget_usd=18,
                              ttl_seconds=2700, now=100)
        self.assertEqual(extended["expires_at"], 2800)
        self.assertEqual(run_report(extended, now=101)["budget_usd"], 18)
        reserve(extended, "A", 100, 100, now=2799)
        with self.assertRaisesRegex(PolicyError, "run_expired"):
            reserve(extended, "A", 100, 100, now=2800)

    def test_reserve_before_work_and_per_task_quota(self):
        pending = reserve(self.run, "A", 1000, 100, now=101)
        self.assertEqual(charged(self.run["tasks"]["A"]), cost_micro(1000, 100))
        with self.assertRaisesRegex(PolicyError, "task_inflight"):
            reserve(self.run, "A", 1000, 100, now=101)
        self.assertEqual(self.run["tasks"]["A"]["requests"], 1)
        finish(self.run, "A", pending["id"])
        self.assertEqual(
            self.run["tasks"]["A"]["retained_micro"], cost_micro(1000, 100)
        )

    def test_exact_count_must_fit_reserved_bound(self):
        pending = reserve(self.run, "A", 1000, 100, now=101)
        for value in (True, -1, 1001, "100", None):
            with self.assertRaises(PolicyError):
                counted(self.run, "A", pending["id"], value)
        self.assertIsNone(self.run["tasks"]["A"]["pending"]["counted_input"])
        counted(self.run, "A", pending["id"], 100)
        self.assertEqual(self.run["tasks"]["A"]["pending"]["counted_input"], 100)

    def test_only_verified_final_usage_refunds_once(self):
        pending = reserve(self.run, "A", 1000, 100, now=101)
        counted(self.run, "A", pending["id"], 100)
        self.assertTrue(finish(self.run, "A", pending["id"], completed()))
        task = self.run["tasks"]["A"]
        self.assertEqual(task["committed_micro"], cost_micro(100, 20))
        self.assertEqual(task["reserved_micro"], 0)
        self.assertFalse(finish(self.run, "A", pending["id"], completed()))
        next_pending = reserve(self.run, "A", 1000, 100, now=102)
        self.assertFalse(finish(self.run, "A", pending["id"], completed()))
        self.assertEqual(task["pending"]["id"], next_pending["id"])

    def test_unknown_invalid_and_incomplete_usage_keep_full_reservation(self):
        events = [
            None,
            {},
            completed(model="other"),
            completed(status="incomplete"),
            completed(service_tier="priority"),
            completed(inputs=1001),
            completed(outputs=101),
            completed(inputs=99),
            completed(usage={}),
            completed(
                usage={"input_tokens": True, "output_tokens": 20, "total_tokens": 21}
            ),
        ]
        for event in events:
            with self.subTest(event=event):
                run, _ = new_run("unknown", ["A"], now=100)
                pending = reserve(run, "A", 1000, 100, now=101)
                counted(run, "A", pending["id"], 100)
                finish(run, "A", pending["id"], event)
                self.assertEqual(
                    run["tasks"]["A"]["retained_micro"], pending["reserved_micro"]
                )

    def test_exhaustion_does_not_reset_on_restart(self):
        run, _ = new_run("small", ["A"], model_budget_usd=0.01, now=100)
        pending = reserve(run, "A", 1000, 100, now=101)
        finish(run, "A", pending["id"])
        run = json.loads(json.dumps(run))
        pending = reserve(run, "A", 1000, 100, now=102)
        finish(run, "A", pending["id"])
        before = copy.deepcopy(run)
        with self.assertRaisesRegex(PolicyError, "budget_exhausted"):
            reserve(run, "A", 1000, 100, now=103)
        self.assertEqual(run, before)

    def test_expiry_cancellation_and_request_limit(self):
        for args in ({"now": 1900}, {"now": 101, "stopped": True}):
            with self.assertRaises(PolicyError):
                reserve(self.run, "A", 100, 100, **args)
        self.run["tasks"]["A"]["requests"] = MAX_REQUESTS
        with self.assertRaisesRegex(PolicyError, "request_limit"):
            reserve(self.run, "A", 100, 100, now=101)

    def test_total_ten_quotas_and_rounding_never_exceed_cap(self):
        run, _ = new_run("ten", [str(i) for i in range(10)], now=100)
        self.assertTrue(
            all(task["budget_micro"] == 1_800_000 for task in run["tasks"].values())
        )
        for task_id in run["tasks"]:
            while True:
                try:
                    pending = reserve(run, task_id, 1000, 1000, now=101)
                except PolicyError:
                    break
                finish(run, task_id, pending["id"])
        self.assertLessEqual(
            sum(charged(task) for task in run["tasks"].values()), 18_000_000
        )


class RequestTests(unittest.TestCase):
    def test_codex_first_and_tool_round_trip_shapes_strip_telemetry_and_collaboration(
        self,
    ):
        for after_tool in (False, True):
            body, _ = prepare_request(
                payload(
                    input=codex_wire_input(after_tool),
                    client_metadata={"session_id": "private-session"},
                )
            )
            self.assertNotIn("client_metadata", body)
            self.assertEqual(
                [tool["name"] for tool in body["input"][0]["tools"]],
                ["functions", "clock"],
            )
            if after_tool:
                self.assertEqual(body["input"][-2]["namespace"], "functions")

    def test_additional_namespaces_cannot_hide_paid_or_nested_remote_tools(self):
        for kind in (
            "web_search",
            "mcp",
            "file_search",
            "code_interpreter",
            "image_generation",
            "namespace",
        ):
            items = codex_wire_input()
            items[0]["tools"][0]["tools"] = [{"type": kind, "name": "hidden"}]
            with self.subTest(kind=kind), self.assertRaises(PolicyError):
                prepare_request(payload(input=items))
        for namespace in ("collaboration", "unknown"):
            items = codex_wire_input(True)
            items[-2]["namespace"] = namespace
            with self.assertRaises(PolicyError):
                prepare_request(payload(input=items))
        items = codex_wire_input()
        items[0]["role"] = "user"
        with self.assertRaises(PolicyError):
            prepare_request(payload(input=items))

    def test_tool_count_is_bounded_across_additional_and_top_level_definitions(self):
        items = codex_wire_input()
        tools = [
            {"type": "function", "name": str(i), "parameters": {}} for i in range(62)
        ]
        with self.assertRaisesRegex(PolicyError, "invalid_tool_count"):
            prepare_request(payload(input=items, tools=tools))

    def test_clamps_model_tier_output_and_disables_storage(self):
        body, upper = prepare_request(
            payload(max_output_tokens=999999, store=True, service_tier="priority")
        )
        self.assertEqual(body["max_output_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(body["service_tier"], "default")
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertGreater(upper, len(json.dumps(body)))

    def test_accepts_codex_local_tool_conversation_and_encrypted_reasoning(self):
        prepare_request(
            payload(
                tools=[
                    {"type": "function", "name": "shell", "parameters": {}},
                    {
                        "type": "custom",
                        "name": "apply_patch",
                        "format": {"type": "text"},
                    },
                ],
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "test"}],
                    },
                    {
                        "type": "reasoning",
                        "id": "r",
                        "summary": [],
                        "encrypted_content": "opaque",
                    },
                    {
                        "type": "function_call",
                        "call_id": "c",
                        "name": "shell",
                        "arguments": "{}",
                    },
                    {"type": "function_call_output", "call_id": "c", "output": "ok"},
                ],
                include=["reasoning.encrypted_content"],
            )
        )

    def test_blocks_paid_remote_tools_and_external_context(self):
        variants = [
            {"tools": [{"type": kind}]}
            for kind in (
                "web_search",
                "file_search",
                "mcp",
                "image_generation",
                "code_interpreter",
                "shell",
                "namespace",
            )
        ]
        variants += [
            {"previous_response_id": "resp_old"},
            {"conversation": "conversation_old"},
            {"background": True},
            {"stream": False},
            {"model": "gpt-6-astra"},
            {"input": [{"type": "item_reference", "id": "remote"}]},
            {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": "https://example.com"}
                        ],
                    }
                ]
            },
            {"prompt_cache_retention": "24h"},
            {"max_output_tokens": True},
            {"reasoning": {"mode": "pro"}},
        ]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(PolicyError):
                prepare_request(payload(**changes))

    def test_malformed_bounded_inputs(self):
        for raw in (
            b"{",
            b"[]",
            b"null",
            b"NaN",
            b"x" * (MAX_BODY_BYTES + 1),
            payload(input=[{"type": []}]),
            payload(tools=[{"type": []}]),
            payload(input="\ud800"),
        ):
            with self.subTest(raw=raw[:50]), self.assertRaises(PolicyError):
                prepare_request(raw)

    def test_sse_chunk_boundaries_and_no_output_retention(self):
        parser = SSEUsage()
        event = completed()
        event["response"]["output"] = ["private generated text"]
        wire = (
            "event: response.completed\r\ndata: "
            + json.dumps(event)
            + "\r\n\r\ndata: [DONE]\r\n\r\n"
        ).encode()
        for byte in wire:
            parser.feed(bytes([byte]))
        self.assertIsNotNone(parser.event)
        self.assertNotIn("private generated text", json.dumps(parser.event))
        self.assertFalse(parser.failed)

    def test_sse_malformed_duplicate_and_oversized_events_fail_closed(self):
        for chunks in (
            [b"data: invalid\n\n"],
            [b"x" * 2_000_001],
            [("data: " + json.dumps(completed()) + "\n\n").encode()] * 2,
        ):
            parser = SSEUsage()
            for chunk in chunks:
                parser.feed(chunk)
            self.assertTrue(parser.failed)
            self.assertIsNone(parser.event)


class MemoryStore:
    def __init__(self):
        self.values = {}
        self.fail_write = None

    async def get(self, key):
        await asyncio.sleep(0)
        return copy.deepcopy(self.values.get(key))

    async def put(self, key, value, *, skip_if_exists=False):
        await asyncio.sleep(0)
        if skip_if_exists and key in self.values:
            return False
        self.values[key] = copy.deepcopy(value)
        if self.fail_write == key:
            raise OSError("ambiguous storage acknowledgement")
        return True

    async def pop(self, key):
        await asyncio.sleep(0)
        return self.values.pop(key, None)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.service = BudgetService(self.store)
        self.created = await self.service.create("pilot", ["A", "B"], 18, 1800, 100)

    async def test_create_is_one_time_and_tokens_are_not_stored(self):
        with self.assertRaisesRegex(PolicyError, "run_already_exists"):
            await self.service.create("pilot", ["A"], 18, 1800, 200)
        serialized = json.dumps(self.store.values)
        for token in self.created["tokens"].values():
            self.assertNotIn(token, serialized)
        self.assertEqual(
            await self.service.authenticate(self.created["tokens"]["A"], 101),
            ("pilot", "A"),
        )

    async def test_concurrent_requests_only_reserve_once(self):
        results = await asyncio.gather(
            *[self.service.reserve("pilot", "A", 1000, 100, 101) for _ in range(10)],
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        report = await self.service.status("pilot", 101)
        self.assertEqual(report["tasks"]["A"]["requests"], 1)

    async def test_100_concurrent_task_reservations_settle_once_within_unchanged_quota(self):
        task_ids = [f"T{index:03}" for index in range(100)]
        await self.service.create("hundred", task_ids, 180, 1800, 100)
        pending = await asyncio.gather(*[
            self.service.reserve("hundred", task, 1000, 100, 101) for task in task_ids
        ])
        report = await self.service.status("hundred", 101)
        self.assertEqual(report["reserved_usd"], 100 * cost_micro(1000, 100) / 1_000_000)
        self.assertTrue(all(task["budget_usd"] == 1.8 for task in report["tasks"].values()))
        collisions = await asyncio.gather(*[
            self.service.reserve("hundred", task, 1000, 100, 101) for task in task_ids
        ], return_exceptions=True)
        self.assertTrue(all(isinstance(error, PolicyError) and error.code == "task_inflight" for error in collisions))

        async def settle(task, reservation):
            await self.service.record_count("hundred", task, reservation["id"], 100, 102)
            await self.service.finish("hundred", task, reservation["id"], completed())

        await asyncio.gather(*[settle(task, reservation) for task, reservation in zip(task_ids, pending, strict=True)])
        replacement = BudgetService(self.store)
        report = await replacement.status("hundred", 103)
        self.assertEqual(report["reserved_usd"], 0)
        self.assertEqual(report["committed_usd"], 100 * cost_micro(100, 20) / 1_000_000)
        self.assertLessEqual(report["maximum_charged_usd"], 180)
        self.assertTrue(all(task["completed"] == 1 for task in report["tasks"].values()))
        original = await replacement.status("pilot", 103)
        self.assertEqual(original["budget_usd"], 18)

    async def test_restart_preserves_inflight_reservation(self):
        await self.service.reserve("pilot", "A", 1000, 100, 101)
        replacement = BudgetService(self.store)
        with self.assertRaisesRegex(PolicyError, "task_inflight"):
            await replacement.reserve("pilot", "A", 1000, 100, 102)
        await replacement.reserve("pilot", "B", 1000, 100, 102)

    async def test_ambiguous_write_leaves_lock_and_money_charged(self):
        self.store.fail_write = "run:pilot"
        with self.assertRaises(OSError):
            await self.service.reserve("pilot", "A", 1000, 100, 101)
        replacement = BudgetService(self.store)
        self.store.fail_write = None
        with self.assertRaisesRegex(PolicyError, "ledger_locked"):
            await replacement.reserve("pilot", "B", 1000, 100, 102)
        report = await replacement.stop("pilot", 102)
        self.assertTrue(report["ledger_locked"])
        self.assertGreater(report["reserved_usd"], 0)
        self.assertEqual(report["state"], "stopped")

    async def test_stop_is_independent_of_inflight_ledger_write(self):
        async with self.service.transaction("pilot") as run:
            await self.service.stop("pilot", 101)
            run["rate_limits"]["x-ratelimit-limit-requests"] = "5000"
        with self.assertRaisesRegex(PolicyError, "run_stopped"):
            await self.service.authenticate(self.created["tokens"]["A"], 102)

    async def test_count_failure_persists_metrics_and_retains_credit(self):
        pending = await self.service.reserve("pilot", "A", 1000, 100, 101)
        with self.assertRaises(PolicyError):
            await self.service.record_count("pilot", "A", pending["id"], None, 102)
        await self.service.finish("pilot", "A", pending["id"], error="count_failed")
        report = await self.service.status("pilot", 103)
        self.assertEqual(report["tasks"]["A"]["count_errors"], 1)
        self.assertEqual(report["tasks"]["A"]["count_requests"], 1)
        self.assertGreater(report["retained_usd"], 0)

    async def test_rate_limit_headers_are_allowlisted(self):
        await self.service.rate_limits(
            "pilot",
            {
                "x-ratelimit-limit-requests": "5000",
                "x-ratelimit-reset-tokens": "1m20s",
                "authorization": "secret",
                "set-cookie": "private",
                "x-ratelimit-remaining-tokens": "bad\nvalue",
            },
        )
        report = await self.service.status("pilot", 101)
        self.assertEqual(
            report["rate_limits"],
            {"x-ratelimit-limit-requests": "5000", "x-ratelimit-reset-tokens": "1m20s"},
        )


HAS_HTTP = all(
    importlib.util.find_spec(name) is not None for name in ("modal", "fastapi", "httpx")
)


@unittest.skipUnless(HAS_HTTP, "optional HTTP tests need modal, fastapi, and httpx")
class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx

        from swarm_control.pilot_gateway import build_web_app

        self.service = BudgetService(MemoryStore())
        self.created = await self.service.create("http", ["A"], 18, 1800, time.time())
        self.calls = []
        self.mode = "success"

        async def upstream(request):
            self.calls.append(request)
            if request.url.path.endswith("/input_tokens"):
                if self.mode == "count_error":
                    return httpx.Response(
                        429, json={"error": "private upstream details"}
                    )
                return httpx.Response(
                    200, json={"object": "response.input_tokens", "input_tokens": 100}
                )
            if self.mode == "upstream_error":
                return httpx.Response(400, json={"error": "private upstream details"})
            data = b"data: " + json.dumps(completed()).encode() + b"\n\n"
            if self.mode == "incomplete":
                data = b'data: {"type":"response.incomplete"}\n\n'
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "x-ratelimit-limit-tokens": "1000000",
                },
                content=data,
            )

        web = build_web_app(
            self.service,
            "fake-test-only-upstream",
            transport=httpx.MockTransport(upstream),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web), base_url="http://gateway.test"
        )
        self.headers = {
            "Authorization": "Bearer " + self.created["tokens"]["A"],
            "Content-Type": "application/json",
            "OpenAI-Project": "worker-project",
        }

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_authenticated_admission_reports_actual_gateway_capacity(self):
        from swarm_control import pilot_gateway
        from swarm_control.provider_rate import ProviderRatePolicy

        with (
            patch.object(pilot_gateway, "BudgetService", return_value=self.service),
            patch.object(pilot_gateway, "ModalStore", return_value=object()),
            patch.object(pilot_gateway, "MAX_UPSTREAM_REQUESTS", 100),
            patch.object(pilot_gateway, "PROVIDER_RATE", ProviderRatePolicy(800_000, 4000)),
        ):
            issued = await pilot_gateway.create_run.local("reported-capacity", ["A"], 18, 1800)
        self.assertEqual(issued["gateway_concurrency"], 100)
        self.assertEqual(issued["provider_rate_tpm"], 800_000)
        self.assertEqual(issued["provider_rate_rpm"], 4000)
        self.assertEqual(issued["provider_rate_queue_seconds"], 300)
        self.assertEqual(issued["budget_usd"], 18)
        self.assertEqual(set(issued["tokens"]), {"A"})

    async def test_pacing_configuration_requires_paired_explicit_positive_limits(self):
        from swarm_control.pilot_gateway import configured_provider_rate

        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(configured_provider_rate())
        for settings in (
            {"SWARM_GATEWAY_TPM": "100"}, {"SWARM_GATEWAY_RPM": "100"},
            {"SWARM_GATEWAY_TPM": "0", "SWARM_GATEWAY_RPM": "100"},
            {"SWARM_GATEWAY_TPM": "100", "SWARM_GATEWAY_RPM": "-1"},
            {"SWARM_GATEWAY_TPM": "1.5", "SWARM_GATEWAY_RPM": "100"},
            {"SWARM_GATEWAY_TPM": "100", "SWARM_GATEWAY_RPM": ""},
            {"SWARM_GATEWAY_TPM": "9" * 1000, "SWARM_GATEWAY_RPM": "100"},
        ):
            with patch.dict(os.environ, settings, clear=True), self.assertRaisesRegex(ValueError, "paired positive"):
                configured_provider_rate()
        with patch.dict(os.environ, {"SWARM_GATEWAY_TPM": "800000", "SWARM_GATEWAY_RPM": "4000"}, clear=True):
            configured = configured_provider_rate()
        self.assertEqual(configured.tokens_per_minute, 800_000)
        self.assertEqual(configured.requests_per_minute, 4000)

    async def test_pacing_uses_exact_counts_and_holds_two_sessions_before_inference(self):
        import httpx

        from swarm_control.pilot_gateway import build_web_app

        class HoldingRate:
            def __init__(self):
                self.weights = []
                self.counted = asyncio.Event()
                self.release = asyncio.Event()

            def deadline(self):
                return time.monotonic() + 300

            async def acquire(self, *, tokens, deadline):
                self.weights.append((tokens, deadline))
                if tokens:
                    if sum(bool(weight) for weight, _ in self.weights) == 2:
                        self.counted.set()
                    await self.release.wait()

        rate = HoldingRate()
        service = BudgetService(MemoryStore())
        issued = await service.create("http-paced", ["A", "B"], 18, 1800, time.time())
        calls = []

        async def upstream(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b"data: " + json.dumps(completed()).encode() + b"\n\n")

        web = build_web_app(service, "fake", transport=httpx.MockTransport(upstream),
                            rate_limiter=rate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://pacing.test") as client:
            requests = [asyncio.create_task(client.post("/v1/responses", headers={
                "Authorization": "Bearer " + issued["tokens"][task], "Content-Type": "application/json",
            }, content=payload(max_output_tokens=32))) for task in ("A", "B")]
            try:
                await asyncio.wait_for(rate.counted.wait(), 2)
                self.assertEqual(calls, ["/v1/responses/input_tokens"] * 2)
                self.assertEqual(sorted(weight for weight, _ in rate.weights), [0, 0, 132, 132])
                # Each count/inference pair shares its original request deadline.
                deadlines = [deadline for _, deadline in rate.weights]
                self.assertEqual(len(set(deadlines)), 2)
                self.assertTrue(all(deadlines.count(value) == 2 for value in deadlines))
                report = await service.status("http-paced", time.time())
                self.assertTrue(all(task["count_requests"] == 1 for task in report["tasks"].values()))
                self.assertTrue(all(task["inflight"] for task in report["tasks"].values()))
                self.assertTrue(all(task["completed"] == 0 for task in report["tasks"].values()))
            finally:
                rate.release.set()
                responses = await asyncio.gather(*requests)
        self.assertTrue(all(response.status_code == 200 for response in responses))
        report = await service.status("http-paced", time.time())
        self.assertEqual(report["reserved_usd"], 0)
        self.assertEqual(report["retained_usd"], 0)
        self.assertTrue(all(task["completed"] == 1 for task in report["tasks"].values()))

    async def test_pacing_timeout_retains_money_and_releases_slot_without_inference(self):
        import httpx

        from swarm_control.pilot_gateway import build_web_app
        from swarm_control.provider_rate import ProviderRateLimiter, ProviderRatePolicy
        from tests.test_provider_rate import FakeClock, settle

        clock = FakeClock()
        rate = ProviderRateLimiter(ProviderRatePolicy(4200, 100, max_wait_seconds=5),
                                   clock=clock, sleep=clock.sleep)
        await rate.acquire(tokens=4200)
        calls = []

        async def upstream(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})

        web = build_web_app(self.service, "fake", max_upstream_requests=1,
                            transport=httpx.MockTransport(upstream), rate_limiter=rate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://pacing.test") as client:
            for _ in range(2):
                request = asyncio.create_task(client.post("/v1/responses", headers=self.headers, content=payload()))
                await settle()
                self.assertFalse(request.done())
                clock.advance(5)
                await settle()
                response = await request
                self.assertEqual(response.status_code, 429)
                self.assertEqual(response.json()["error"]["code"], "gateway_rate_wait_expired")
                self.assertEqual(response.headers["retry-after"], "60")
        self.assertEqual(calls, ["/v1/responses/input_tokens"] * 2)
        report = await self.service.status("http", time.time())
        prepared, upper = prepare_request(payload())
        expected = 2 * cost_micro(upper, prepared["max_output_tokens"]) / 1_000_000
        self.assertEqual(report["retained_usd"], expected)
        self.assertEqual(report["maximum_charged_usd"], expected)
        self.assertEqual(report["committed_usd"], 0)
        self.assertEqual(report["reserved_usd"], 0)
        self.assertEqual(report["tasks"]["A"]["count_requests"], 2)
        self.assertEqual(report["tasks"]["A"]["errors"], 2)
        self.assertEqual(report["tasks"]["A"]["completed"], 0)
        self.assertFalse(report["tasks"]["A"]["inflight"])

    async def test_cancelling_pacing_waiter_retains_reservation_and_allows_recovery(self):
        import httpx

        from swarm_control.pilot_gateway import build_web_app
        from swarm_control.provider_rate import ProviderRateLimiter, ProviderRatePolicy
        from tests.test_provider_rate import FakeClock, settle

        clock = FakeClock()
        rate = ProviderRateLimiter(ProviderRatePolicy(4200, 100), clock=clock, sleep=clock.sleep)
        await rate.acquire(tokens=4200)
        calls = []

        async def upstream(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b"data: " + json.dumps(completed()).encode() + b"\n\n")

        web = build_web_app(self.service, "fake", max_upstream_requests=1,
                            transport=httpx.MockTransport(upstream), rate_limiter=rate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://pacing.test") as client:
            request = asyncio.create_task(client.post("/v1/responses", headers=self.headers, content=payload()))
            await settle()
            self.assertEqual(calls, ["/v1/responses/input_tokens"])
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            await settle()
            report = await self.service.status("http", time.time())
            self.assertGreater(report["retained_usd"], 0)
            self.assertEqual(report["committed_usd"], 0)
            self.assertFalse(report["tasks"]["A"]["inflight"])
            clock.advance(60)
            response = await client.post("/v1/responses", headers=self.headers, content=payload(max_output_tokens=32))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["/v1/responses/input_tokens", "/v1/responses/input_tokens", "/v1/responses"])
        report = await self.service.status("http", time.time())
        self.assertEqual(report["tasks"]["A"]["completed"], 1)
        self.assertEqual(report["tasks"]["A"]["errors"], 1)
        self.assertGreater(report["retained_usd"], 0)
        self.assertGreater(report["committed_usd"], 0)

    async def test_round_trip_credentials_usage_and_immediate_followup(self):
        for _ in range(2):
            response = await self.client.post(
                "/v1/responses", headers=self.headers, content=payload()
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("response.completed", response.text)
        report = await self.service.status("http", time.time())
        self.assertEqual(report["tasks"]["A"]["completed"], 2)
        self.assertEqual(report["tasks"]["A"]["count_requests"], 2)
        self.assertEqual(report["reserved_usd"], 0)
        self.assertEqual(len(self.calls), 4)
        for call in self.calls:
            self.assertEqual(
                call.headers["authorization"], "Bearer fake-test-only-upstream"
            )
            self.assertNotIn("openai-project", call.headers)
            self.assertNotIn(self.created["tokens"]["A"], call.content.decode())
        self.assertEqual(report["rate_limits"]["x-ratelimit-limit-tokens"], "1000000")

    async def test_codex_shapes_send_same_sanitized_input_to_counter_and_inference(
        self,
    ):
        for after_tool in (False, True):
            response = await self.client.post(
                "/v1/responses",
                headers=self.headers,
                content=payload(
                    input=codex_wire_input(after_tool),
                    client_metadata={"session_id": "private-session"},
                ),
            )
            self.assertEqual(response.status_code, 200)
            counted_body = json.loads(self.calls[-2].content)
            generated_body = json.loads(self.calls[-1].content)
            self.assertEqual(counted_body["input"], generated_body["input"])
            self.assertNotIn("client_metadata", generated_body)
            self.assertNotIn("collaboration", json.dumps(generated_body))

    async def test_unauthenticated_forbidden_and_compressed_requests_make_no_provider_call(
        self,
    ):
        response = await self.client.post("/v1/responses", content=payload())
        self.assertEqual(response.status_code, 401)
        response = await self.client.post(
            "/v1/responses",
            headers=self.headers,
            content=payload(tools=[{"type": "web_search"}]),
        )
        self.assertEqual(response.status_code, 400)
        response = await self.client.post(
            "/v1/responses",
            headers={**self.headers, "Content-Encoding": "gzip"},
            content=b"compressed",
        )
        self.assertEqual(response.status_code, 415)
        self.assertFalse(self.calls)

    async def test_capacity_wait_timeout_has_no_debit_and_stream_completion_releases_slot(self):
        import httpx

        from swarm_control.pilot_gateway import build_web_app

        service = BudgetService(MemoryStore())
        created = await service.create("capacity", ["A", "B"], 18, 1800, time.time())
        streaming = asyncio.Event()
        release = asyncio.Event()

        class DelayedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                streaming.set()
                await release.wait()
                yield b"data: " + json.dumps(completed()).encode() + b"\n\n"

        async def upstream(request):
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=DelayedStream())

        web = build_web_app(service, "fake", transport=httpx.MockTransport(upstream),
                            max_upstream_requests=1, capacity_wait_seconds=0.02)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://capacity.test") as client:
            def headers(task):
                return {"Authorization": "Bearer " + created["tokens"][task], "Content-Type": "application/json"}

            first = asyncio.create_task(client.post("/v1/responses", headers=headers("A"), content=payload()))
            try:
                await asyncio.wait_for(streaming.wait(), 2)
                rejected = await client.post("/v1/responses", headers=headers("B"), content=payload())
                self.assertEqual(rejected.status_code, 429)
                self.assertEqual(rejected.json()["error"]["code"], "gateway_busy")
                self.assertEqual(rejected.headers["retry-after"], "1")
                report = await service.status("capacity", time.time())
                self.assertEqual(report["tasks"]["B"]["requests"], 0)
                self.assertEqual(report["tasks"]["B"]["maximum_charged_usd"], 0)
            finally:
                release.set()
                response = await first
            self.assertEqual(response.status_code, 200)
            retried = await client.post("/v1/responses", headers=headers("B"), content=payload())
            self.assertEqual(retried.status_code, 200)
        report = await service.status("capacity", time.time())
        self.assertEqual(report["reserved_usd"], 0)
        self.assertEqual(report["tasks"]["B"]["completed"], 1)

    async def test_reservation_rejection_and_cancelled_connection_release_capacity(self):
        import httpx

        from swarm_control.pilot_gateway import build_web_app

        service = BudgetService(MemoryStore())
        created = await service.create("capacity", ["A", "B"], 18, 1800, time.time())
        pending = await service.reserve("capacity", "A", 1000, 100, time.time())
        connecting = asyncio.Event()
        release = asyncio.Event()

        async def upstream(request):
            if request.url.path.endswith("/input_tokens"):
                connecting.set()
                await release.wait()
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b"data: " + json.dumps(completed()).encode() + b"\n\n")

        web = build_web_app(service, "fake", transport=httpx.MockTransport(upstream),
                            max_upstream_requests=1, capacity_wait_seconds=0.02)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://capacity.test") as client:
            def headers(task):
                return {"Authorization": "Bearer " + created["tokens"][task], "Content-Type": "application/json"}

            response = await client.post("/v1/responses", headers=headers("A"), content=payload())
            self.assertEqual(response.status_code, 409)
            await service.finish("capacity", "A", pending["id"])
            cancelled = asyncio.create_task(client.post("/v1/responses", headers=headers("A"), content=payload()))
            await asyncio.wait_for(connecting.wait(), 2)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            release.set()
            response = await client.post("/v1/responses", headers=headers("B"), content=payload())
            self.assertEqual(response.status_code, 200)
        report = await service.status("capacity", time.time())
        self.assertGreater(report["tasks"]["A"]["retained_usd"], 0)
        self.assertFalse(report["tasks"]["A"]["inflight"])

    async def test_100_concurrent_http_tasks_use_bounded_upstream_capacity(self):
        import httpx

        from swarm_control.pilot_gateway import build_web_app

        service = BudgetService(MemoryStore())
        task_ids = [f"T{index:03}" for index in range(100)]
        created = await service.create("http-hundred", task_ids, 180, 1800, time.time())
        active = peak = 0

        async def upstream(request):
            nonlocal active, peak
            if request.url.path.endswith("/input_tokens"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})
            active -= 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b"data: " + json.dumps(completed()).encode() + b"\n\n")

        web = build_web_app(service, "fake", transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://capacity.test") as client:
            responses = await asyncio.gather(*[
                client.post("/v1/responses", headers={
                    "Authorization": "Bearer " + created["tokens"][task], "Content-Type": "application/json",
                }, content=payload()) for task in task_ids
            ])
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 20)
        report = await service.status("http-hundred", time.time())
        self.assertEqual(report["reserved_usd"], 0)
        self.assertTrue(all(task["completed"] == 1 for task in report["tasks"].values()))

    async def test_configured_100_capacity_admits_full_cohort_before_any_response_finishes(self):
        import httpx

        from swarm_control.pilot_gateway import (
            build_web_app,
            configured_gateway_concurrency,
        )

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_gateway_concurrency(), 20)
        for value in ("0", "101", "true", "1.5", "-1", "", "9" * 5000):
            with patch.dict(os.environ, {"SWARM_GATEWAY_CONCURRENCY": value}), self.assertRaisesRegex(ValueError, "SWARM_GATEWAY_CONCURRENCY"):
                configured_gateway_concurrency()
        with patch.dict(os.environ, {"SWARM_GATEWAY_CONCURRENCY": "100"}):
            configured = configured_gateway_concurrency()
        self.assertEqual(configured, 100)

        service = BudgetService(MemoryStore())
        task_ids = [f"T{index:03}" for index in range(100)]
        created = await service.create("configured-hundred", task_ids, 180, 1800, time.time())
        all_admitted = asyncio.Event()
        finish = asyncio.Event()
        active = 0

        async def upstream(request):
            nonlocal active
            if request.url.path.endswith("/input_tokens"):
                active += 1
                if active == 100:
                    all_admitted.set()
                await finish.wait()
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b"data: " + json.dumps(completed()).encode() + b"\n\n")

        web = build_web_app(service, "fake", max_upstream_requests=configured,
                            capacity_wait_seconds=0.02, transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://capacity.test") as client:
            requests = [asyncio.create_task(client.post("/v1/responses", headers={
                "Authorization": "Bearer " + created["tokens"][task], "Content-Type": "application/json",
            }, content=payload())) for task in task_ids]
            try:
                await asyncio.wait_for(all_admitted.wait(), 5)
                report = await service.status("configured-hundred", time.time())
                self.assertEqual(active, 100)
                self.assertTrue(all(task["inflight"] for task in report["tasks"].values()))
                self.assertTrue(all(task["completed"] == 0 for task in report["tasks"].values()))
            finally:
                finish.set()
                responses = await asyncio.gather(*requests)
        self.assertTrue(all(response.status_code == 200 for response in responses))
        report = await service.status("configured-hundred", time.time())
        self.assertEqual(report["reserved_usd"], 0)
        self.assertTrue(all(task["completed"] == 1 for task in report["tasks"].values()))

    async def test_count_rate_limit_preserves_429_code_and_full_reservation(self):
        self.mode = "count_error"
        response = await self.client.post(
            "/v1/responses", headers=self.headers, content=payload()
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "upstream_count_rate_limit")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0].url.path.endswith("/input_tokens"))
        self.assertNotIn("private upstream details", response.text)
        report = await self.service.status("http", time.time())
        prepared, upper = prepare_request(payload())
        expected = cost_micro(upper, prepared["max_output_tokens"]) / 1_000_000
        self.assertEqual(report["retained_usd"], expected)
        self.assertEqual(report["maximum_charged_usd"], expected)
        self.assertEqual(report["reserved_usd"], 0)
        self.assertEqual(report["committed_usd"], 0)
        self.assertEqual(report["tasks"]["A"]["count_errors"], 1)
        self.assertEqual(report["tasks"]["A"]["count_requests"], 1)
        self.assertEqual(report["tasks"]["A"]["errors"], 1)
        self.assertEqual(report["tasks"]["A"]["completed"], 0)
        self.assertFalse(report["tasks"]["A"]["inflight"])

    async def test_upstream_error_is_sanitized_and_incomplete_keeps_reservation(self):
        self.mode = "upstream_error"
        response = await self.client.post(
            "/v1/responses", headers=self.headers, content=payload()
        )
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private upstream details", response.text)
        self.mode = "incomplete"
        response = await self.client.post(
            "/v1/responses", headers=self.headers, content=payload()
        )
        self.assertEqual(response.status_code, 200)
        report = await self.service.status("http", time.time())
        self.assertEqual(report["tasks"]["A"]["errors"], 2)
        self.assertEqual(report["tasks"]["A"]["completed"], 0)
        self.assertGreater(report["retained_usd"], 0)


if __name__ == "__main__":
    unittest.main()
