"""Offline provider pacing checks with deterministic monotonic time."""

import asyncio
import unittest

from swarm_control.provider_rate import (
    ProviderRateError,
    ProviderRateLimiter,
    ProviderRatePolicy,
    admission_rate_settings,
)


class FakeClock:
    def __init__(self):
        self.now = 0
        self.sleepers = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        item = (self.now + seconds, future)
        self.sleepers.append(item)
        try:
            await future
        finally:
            self.sleepers.remove(item)

    def advance(self, seconds):
        self.now += seconds
        for deadline, future in self.sleepers[:]:
            if deadline <= self.now and not future.done():
                future.set_result(None)


async def settle():
    # Event notification, timer cancellation and the next FIFO head each need
    # event-loop turns. No test waits for a real rate-limit minute.
    for _ in range(30):
        await asyncio.sleep(0)


class RatePolicyTests(unittest.TestCase):
    def test_configuration_is_explicit_positive_and_bounded(self):
        for values in ((0, 10), (10, 0), (True, 10), (10, 1.5)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ProviderRatePolicy(*values)
        for wait in (0, -1, 301, float("inf"), float("nan"), True):
            with self.subTest(wait=wait), self.assertRaises(ValueError):
                ProviderRatePolicy(100, 10, max_wait_seconds=wait)
        for size in (0, 101, True):
            with self.subTest(size=size), self.assertRaises(ValueError):
                ProviderRatePolicy(100, 10, max_waiters=size)

    def test_admission_reports_settings_without_claiming_provider_quota(self):
        self.assertEqual(admission_rate_settings(None), {
            "provider_rate_tpm": None, "provider_rate_rpm": None,
            "provider_rate_queue_seconds": 0,
        })
        self.assertEqual(admission_rate_settings(ProviderRatePolicy(800_000, 4000)), {
            "provider_rate_tpm": 800_000, "provider_rate_rpm": 4000,
            "provider_rate_queue_seconds": 300,
        })


class ProviderRateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()

    def limiter(self, tokens=100, requests=100, **kwargs):
        return ProviderRateLimiter(
            ProviderRatePolicy(tokens, requests, **kwargs),
            clock=self.clock, sleep=self.clock.sleep,
        )

    async def test_weighted_tokens_expire_at_their_own_rolling_boundaries(self):
        rate = self.limiter()
        await rate.acquire(tokens=60)
        self.clock.advance(10)
        await rate.acquire(tokens=40)
        waiting = asyncio.create_task(rate.acquire(tokens=41))
        await settle()
        self.clock.advance(49)
        await settle()
        self.assertFalse(waiting.done())
        self.clock.advance(1)
        await settle()
        await waiting
        # The 40 tokens admitted at t=10 remain through t=70.
        waiting = asyncio.create_task(rate.acquire(tokens=20))
        await settle()
        self.assertFalse(waiting.done())
        self.clock.advance(10)
        await settle()
        await waiting

    async def test_count_and_inference_both_obey_request_ceiling(self):
        rate = self.limiter(requests=2)
        await rate.acquire(tokens=0)
        await rate.acquire(tokens=100)
        waiting = asyncio.create_task(rate.acquire(tokens=0))
        await settle()
        self.assertFalse(waiting.done())
        self.clock.advance(60)
        await settle()
        await waiting

    async def test_fifo_does_not_allow_a_small_request_to_overtake_head(self):
        rate = self.limiter()
        await rate.acquire(tokens=80)
        order = []

        async def acquire(name, tokens):
            await rate.acquire(tokens=tokens)
            order.append(name)

        head = asyncio.create_task(acquire("head", 50))
        await settle()
        tail = asyncio.create_task(acquire("tail", 10))
        await settle()
        self.assertEqual(order, [])
        self.clock.advance(60)
        await settle()
        await asyncio.gather(head, tail)
        self.assertEqual(order, ["head", "tail"])

    async def test_cancelling_head_unblocks_tail_and_keeps_consumed_permits(self):
        rate = self.limiter()
        await rate.acquire(tokens=80)
        head = asyncio.create_task(rate.acquire(tokens=50))
        await settle()
        tail = asyncio.create_task(rate.acquire(tokens=10))
        await settle()
        head.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await head
        await settle()
        await tail
        waiting = asyncio.create_task(rate.acquire(tokens=11))
        await settle()
        self.assertFalse(waiting.done())
        self.clock.advance(60)
        await settle()
        await waiting

    async def test_queue_is_bounded_and_rejecting_does_not_strand_it(self):
        rate = self.limiter(max_waiters=2)
        await rate.acquire(tokens=100)
        waiting = [asyncio.create_task(rate.acquire(tokens=1)) for _ in range(2)]
        await settle()
        with self.assertRaisesRegex(ProviderRateError, "gateway_rate_queue_full"):
            await rate.acquire(tokens=1)
        self.clock.advance(60)
        await settle()
        await asyncio.gather(*waiting)
        await rate.acquire(tokens=98)

    async def test_timeout_removes_waiter_without_refunding_existing_capacity(self):
        rate = self.limiter(max_wait_seconds=5)
        await rate.acquire(tokens=100)
        waiting = asyncio.create_task(rate.acquire(tokens=1))
        await settle()
        self.clock.advance(5)
        await settle()
        with self.assertRaisesRegex(ProviderRateError, "gateway_rate_wait_expired"):
            await waiting
        another = asyncio.create_task(rate.acquire(tokens=1))
        await settle()
        self.assertFalse(another.done())
        self.clock.advance(55)
        await settle()
        # Even a delayed event-loop resume cannot extend a caller's deadline.
        with self.assertRaisesRegex(ProviderRateError, "gateway_rate_wait_expired"):
            await another
        await rate.acquire(tokens=100)

    async def test_count_and_inference_share_one_request_deadline(self):
        rate = self.limiter(requests=1, max_wait_seconds=5)
        deadline = rate.deadline()
        await rate.acquire(tokens=0, deadline=deadline)
        self.clock.advance(4)
        waiting = asyncio.create_task(rate.acquire(tokens=10, deadline=deadline))
        await settle()
        self.clock.advance(1)
        await settle()
        with self.assertRaisesRegex(ProviderRateError, "gateway_rate_wait_expired"):
            await waiting

    async def test_invalid_or_impossible_weights_never_enter_the_queue(self):
        rate = self.limiter()
        for weight in (-1, 1.5, True):
            with self.subTest(weight=weight), self.assertRaises(ValueError):
                await rate.acquire(tokens=weight)
        for weight in (0, 1.5, True):
            with self.subTest(requests=weight), self.assertRaises(ValueError):
                await rate.acquire(tokens=0, requests=weight)
        with self.assertRaisesRegex(ProviderRateError, "gateway_request_exceeds_rate_limit"):
            await rate.acquire(tokens=101)
        with self.assertRaisesRegex(ProviderRateError, "gateway_request_exceeds_rate_limit"):
            await rate.acquire(tokens=0, requests=101)
        await rate.acquire(tokens=100)

    async def test_hundred_weighted_sessions_stay_inside_every_rolling_minute(self):
        rate = self.limiter(tokens=800_000, requests=4000)
        events = []

        async def session(index):
            deadline = rate.deadline()
            await rate.acquire(tokens=0, deadline=deadline)
            events.append((self.clock(), 0, index, "count"))
            # Simulated verified 32K context plus maximum 4096 output.
            await rate.acquire(tokens=36_864, deadline=deadline)
            events.append((self.clock(), 36_864, index, "inference"))

        workers = [asyncio.create_task(session(index)) for index in range(100)]
        await settle()
        for _ in range(4):
            self.clock.advance(60)
            await settle()
        await asyncio.gather(*workers)
        self.assertEqual(len(events), 200)
        self.assertEqual(len([event for event in events if event[3] == "inference"]), 100)
        for at, *_ in events:
            rolling = [event for event in events if at - 60 < event[0] <= at]
            self.assertLessEqual(sum(event[1] for event in rolling), 800_000)
            self.assertLessEqual(len(rolling), 4000)
        self.assertEqual(max(event[0] for event in events), 240)
