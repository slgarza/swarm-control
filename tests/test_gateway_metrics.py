"""Safe gateway phase observations using only clocks, memory and fake transports."""

import asyncio
import json
import time
import unittest

from swarm_control.pilot_budget import BudgetService, token_hash
from swarm_control.request_metrics import RequestMetrics
from tests.test_pilot_budget import MemoryStore, completed, payload


class MetricsTests(unittest.TestCase):
    def test_monotonic_phases_retention_and_log_limits_are_explicit(self):
        tick, emitted = [0.0], []
        metrics = RequestMetrics(emit=emitted.append, maximum_receipts=2, maximum_emissions=2,
            monotonic=lambda: tick[0], utc_now=lambda: "2026-11-11T22:00:00+00:00")
        for _ in range(3):
            record = metrics.begin()
            record.bind("run", "A")
            for _ in range(40):
                with record.phase("stream_auth"):
                    tick[0] += 0.25
            record.finish("completion_observed")
            record.finish("handler_failed")
        result = metrics.snapshot("run")
        self.assertEqual(result["retained_receipts"], 2)
        self.assertEqual(result["completed_receipts_evicted"], 1)
        self.assertEqual(result["dropped_log_emissions"], 4)
        self.assertEqual(len(emitted), 2)
        self.assertEqual(result["phase_summary"]["stream_auth"]["observations"], 80)
        self.assertEqual(result["phase_summary"]["stream_auth"]["measured_seconds"], 20)
        self.assertEqual(result["receipts"][0]["omitted_phase_details"], 8)
        self.assertEqual(len(result["receipts"][0]["phases"]), 32)
        self.assertIsNone(result["worker_deliveries"])
        self.assertIsNone(result["actual_billed_cost"])

    def test_broken_observers_record_unknown_time_without_raising(self):
        def failed(*_):
            raise OSError("private diagnostic")

        metrics = RequestMetrics(emit=failed, monotonic=failed, utc_now=failed)
        record = metrics.begin()
        record.bind("run", "A")
        with record.phase("cleanup"):
            record.flag("cleanup_finished")
        record.finish("usage_unverified")
        result = metrics.snapshot()
        self.assertEqual(result["sink_failures"], 2)
        self.assertIsNone(result["receipts"][0]["wall_seconds"])
        self.assertEqual(result["phase_summary"]["cleanup"]["unknown_durations"], 1)
        self.assertNotIn("private diagnostic", json.dumps(result))

    def test_identity_does_not_accept_credential_or_digest_shapes(self):
        metrics = RequestMetrics()
        record = metrics.begin()
        record.bind("sk-" + "x" * 32, "a" * 64)
        record.finish("request_rejected")
        result = metrics.snapshot()["receipts"][0]
        self.assertIsNone(result["run_id"])
        self.assertIsNone(result["task_id"])
        self.assertNotIn("x" * 32, json.dumps(result))

    def test_terminal_receipt_waits_for_cleanup_and_active_phases(self):
        metrics = RequestMetrics()
        record = metrics.begin()
        record.flag("reservation_admitted")
        record.finish("cancelled")
        self.assertEqual(metrics.snapshot()["retained_receipts"], 0)
        with record.phase("cleanup"):
            record.flag("cleanup_finished")
            self.assertEqual(metrics.snapshot()["retained_receipts"], 0)
        result = metrics.snapshot()
        self.assertEqual(result["retained_receipts"], 1)
        self.assertEqual(result["receipts"][0]["outcome"], "cancelled")

    def test_repeat_finish_cannot_change_a_deferred_cancellation(self):
        metrics = RequestMetrics()
        record = metrics.begin()
        record.flag("reservation_admitted")
        record.finish("cancelled")
        record.finish("response_not_consumed")
        record.outcome("handler_failed")
        with record.phase("cleanup"):
            record.flag("cleanup_finished")
        record.finish("response_not_consumed")
        result = metrics.snapshot()
        self.assertEqual(result["retained_receipts"], 1)
        self.assertEqual(result["receipts"][0]["outcome"], "cancelled")

    def test_background_first_finish_preserves_known_cancellation_during_cleanup(self):
        metrics = RequestMetrics()
        record = metrics.begin()
        record.flag("reservation_admitted")
        record.outcome("cancelled")
        with record.phase("cleanup"):
            record.finish("response_not_consumed")
            record.flag("cleanup_finished")
            record.finish("cancelled")
            self.assertEqual(metrics.snapshot()["retained_receipts"], 0)
        result = metrics.snapshot()
        self.assertEqual(result["retained_receipts"], 1)
        self.assertEqual(result["receipts"][0]["outcome"], "cancelled")
        self.assertEqual(result["phase_summary"]["cleanup"]["observations"], 1)


class GatewayMetricHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = BudgetService(MemoryStore())
        self.created = await self.service.create("metrics", ["A", "B"], 18, 1800, time.time())
        self.calls = []
        self.logs = []
        self.tick = [1.0]
        self.metrics = RequestMetrics(emit=self.logs.append, monotonic=lambda: self.tick[0])
        self.api_key = "synthetic-private-upstream-key"

    def headers(self, task="A"):
        return {"Authorization": "Bearer " + self.created["tokens"][task],
                "Content-Type": "application/json", "OpenAI-Project": "private-client-header"}

    def web(self, upstream, **kwargs):
        import httpx

        from swarm_control.pilot_gateway import build_web_app

        return build_web_app(self.service, self.api_key, request_metrics=self.metrics,
                             transport=httpx.MockTransport(upstream), **kwargs)

    def client(self, web):
        import httpx

        return httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://metrics.test")

    def assert_private(self):
        text = json.dumps(self.metrics.snapshot()) + json.dumps(self.logs)
        for secret in [self.api_key, "private-client-header", "private prompt content", "private response content",
                       *self.created["tokens"].values(),
                       *(token_hash(token) for token in self.created["tokens"].values())]:
            self.assertNotIn(secret, text)

    async def successful_upstream(self, request):
        import httpx

        self.calls.append(request.url.path)
        if request.url.path.endswith("/input_tokens"):
            self.tick[0] += 0.25
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})
        self.tick[0] += 0.5
        owner = self

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                owner.tick[0] += 0.75
                yield b'data: {"type":"response.output_text.delta","delta":"private response content"}\n\n'
                owner.tick[0] += 1.25
                yield b"data: " + json.dumps(completed()).encode() + b"\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    async def test_success_measures_actual_calls_first_byte_and_single_cleanup(self):
        web = self.web(self.successful_upstream)
        async with self.client(web) as client:
            response = await client.post("/v1/responses", headers=self.headers(), content=payload(input="private prompt content"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, ["/v1/responses/input_tokens", "/v1/responses"])
        snapshot = self.metrics.snapshot("metrics")
        self.assertEqual(snapshot["retained_receipts"], 1)
        row = snapshot["receipts"][0]
        self.assertEqual(row["outcome"], "completion_observed")
        self.assertEqual(row["phase_summary"]["count_http"]["measured_seconds"], 0.25)
        self.assertEqual(row["phase_summary"]["inference_send"]["measured_seconds"], 0.5)
        self.assertEqual(row["inference_call_to_first_byte_seconds"], 1.25)
        self.assertEqual(row["phase_summary"]["settlement"]["observations"], 1)
        self.assertTrue(row["flags"]["completion_event_seen"])
        self.assertTrue(row["flags"]["cleanup_finished"])
        self.assertTrue(all(p["finished_at_utc"] for p in row["phases"]))
        self.assertEqual([r["record_stage"] for r in self.logs], ["started", "finished"])
        self.assertEqual(web.state.request_metrics.snapshot()["retained_receipts"], 1)
        money = await self.service.status("metrics", time.time())
        self.assertEqual(money["tasks"]["A"]["completed"], 1)
        self.assertEqual(money["retained_usd"], 0)
        self.assert_private()

    async def test_failed_log_sink_changes_neither_response_nor_money(self):
        def broken(_):
            raise OSError("private sink failure")

        self.metrics = RequestMetrics(emit=broken)
        async with self.client(self.web(self.successful_upstream)) as client:
            response = await client.post("/v1/responses", headers=self.headers(), content=payload())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.metrics.snapshot()["sink_failures"], 2)
        money = await self.service.status("metrics", time.time())
        self.assertEqual(money["tasks"]["A"]["completed"], 1)
        self.assertFalse(money["tasks"]["A"]["inflight"])
        self.assertGreater(money["committed_usd"], 0)
        self.assertEqual(money["retained_usd"], 0)
        self.assertNotIn("private sink failure", response.text)

    async def test_count_rejection_never_claims_inference_or_completion(self):
        import httpx

        async def upstream(request):
            self.calls.append(request.url.path)
            return httpx.Response(429, json={"error": "private response content"})

        async with self.client(self.web(upstream)) as client:
            response = await client.post("/v1/responses", headers=self.headers(), content=payload())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.calls, ["/v1/responses/input_tokens"])
        row = self.metrics.snapshot()["receipts"][0]
        self.assertTrue(row["flags"]["reservation_admitted"])
        self.assertNotIn("inference_http_attempted", row["flags"])
        self.assertNotIn("completion_event_seen", row["flags"])
        self.assertIsNone(row["inference_call_to_first_byte_seconds"])
        money = await self.service.status("metrics", time.time())
        self.assertGreater(money["retained_usd"], 0)
        self.assertEqual(money["committed_usd"], 0)
        self.assert_private()

    async def test_pacing_cancellation_retains_money_and_closes_metrics(self):
        entered, release = asyncio.Event(), asyncio.Event()
        owner = self

        class Rate:
            calls = 0

            def deadline(self):
                return 9999

            async def acquire(self, **_):
                self.calls += 1
                if self.calls == 2:
                    entered.set()
                    await release.wait()
                owner.tick[0] += 0.1

        async with self.client(self.web(self.successful_upstream, rate_limiter=Rate())) as client:
            operation = asyncio.create_task(client.post("/v1/responses", headers=self.headers(), content=payload()))
            await asyncio.wait_for(entered.wait(), 2)
            self.tick[0] += 2
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
        row = self.metrics.snapshot()["receipts"][0]
        self.assertEqual(row["outcome"], "cancelled")
        self.assertEqual(row["phase_summary"]["inference_pacing"]["measured_seconds"], 2)
        self.assertNotIn("inference_http_attempted", row["flags"])
        self.assertEqual(self.calls, ["/v1/responses/input_tokens"])
        money = await self.service.status("metrics", time.time())
        self.assertGreater(money["retained_usd"], 0)
        self.assertFalse(money["tasks"]["A"]["inflight"])
        self.assert_private()

    async def test_interrupted_stream_does_not_report_a_completed_response(self):
        import httpx

        async def upstream(request):
            self.calls.append(request.url.path)
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'data: {"type":"response.output_text.delta","delta":"private response content"}\n\n'
                    raise httpx.ReadError("private transport failure")

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

        async with self.client(self.web(upstream)) as client:
            response = await client.post("/v1/responses", headers=self.headers(), content=payload())
        row = self.metrics.snapshot()["receipts"][0]
        self.assertEqual(row["outcome"], "stream_interrupted")
        self.assertNotIn("completion_event_seen", row["flags"])
        self.assertIn("first_byte", row["marks"])
        self.assertIn("stream_interrupted", response.text)
        self.assertNotIn("private transport failure", response.text)
        money = await self.service.status("metrics", time.time())
        self.assertGreater(money["retained_usd"], 0)
        self.assertEqual(money["tasks"]["A"]["completed"], 0)
        self.assert_private()

    async def test_capacity_rejection_has_no_reservation_or_inference_observation(self):
        import httpx

        entered, release = asyncio.Event(), asyncio.Event()

        async def upstream(request):
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    entered.set()
                    await release.wait()
                    yield b"data: " + json.dumps(completed()).encode() + b"\n\n"

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

        web = self.web(upstream, max_upstream_requests=1, capacity_wait_seconds=0.01)
        async with self.client(web) as client:
            running = asyncio.create_task(client.post("/v1/responses", headers=self.headers("A"), content=payload()))
            await asyncio.wait_for(entered.wait(), 2)
            try:
                response = await client.post("/v1/responses", headers=self.headers("B"), content=payload())
            finally:
                release.set()
                await running
        self.assertEqual(response.status_code, 429)
        row = next(r for r in self.metrics.snapshot()["receipts"] if r["task_id"] == "B")
        self.assertEqual(row["outcome"], "capacity_rejected")
        self.assertNotIn("reservation_admitted", row["flags"])
        self.assertNotIn("inference_http_attempted", row["flags"])
        money = await self.service.status("metrics", time.time())
        self.assertEqual(money["tasks"]["B"]["requests"], 0)

    async def test_cancelled_stream_finishes_timing_after_retained_settlement(self):
        import httpx

        entered = asyncio.Event()

        async def upstream(request):
            if request.url.path.endswith("/input_tokens"):
                return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 100})

            class Stream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'data: {"type":"response.output_text.delta"}\n\n'
                    entered.set()
                    await asyncio.Event().wait()

            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

        async with self.client(self.web(upstream)) as client:
            operation = asyncio.create_task(client.post("/v1/responses", headers=self.headers(), content=payload()))
            await asyncio.wait_for(entered.wait(), 2)
            operation.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await operation
            for _ in range(10):
                await asyncio.sleep(0)
                if self.metrics.snapshot()["retained_receipts"]:
                    break
        row = self.metrics.snapshot()["receipts"][0]
        self.assertEqual(row["outcome"], "cancelled")
        self.assertTrue(row["flags"]["cleanup_finished"])
        self.assertTrue(row["flags"]["settlement_returned"])
        self.assertTrue(all(p["finished_at_utc"] for p in row["phases"]))
        money = await self.service.status("metrics", time.time())
        self.assertGreater(money["retained_usd"], 0)
        self.assertFalse(money["tasks"]["A"]["inflight"])

    async def test_uncertain_settlement_does_not_become_successful_accounting(self):
        from unittest.mock import AsyncMock, patch

        with patch.object(self.service, "finish", AsyncMock(side_effect=OSError("synthetic uncertain store"))):
            async with self.client(self.web(self.successful_upstream)) as client:
                response = await client.post("/v1/responses", headers=self.headers(), content=payload())
        self.assertEqual(response.status_code, 200)
        row = self.metrics.snapshot()["receipts"][0]
        self.assertEqual(row["outcome"], "completion_observed")
        self.assertTrue(row["flags"]["completion_event_seen"])
        self.assertNotIn("settlement_returned", row["flags"])
        money = await self.service.status("metrics", time.time())
        self.assertTrue(money["tasks"]["A"]["inflight"])
        self.assertGreater(money["reserved_usd"], 0)
        self.assertEqual(money["committed_usd"], 0)


if __name__ == "__main__":
    unittest.main()
