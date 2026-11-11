"""Bounded, process-local provider pacing for the single serving gateway.

Permits conservatively reserve a full sliding minute of provider capacity. They
are never refunded on failure or cancellation; this limiter does not account for
money, grant spending authority, or attest to an account's actual provider quota.
"""

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass


class ProviderRateError(Exception):
    """A safe, fixed rejection code suitable for an HTTP 429 response."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProviderRatePolicy:
    tokens_per_minute: int
    requests_per_minute: int
    max_wait_seconds: float = 300
    max_waiters: int = 100

    def __post_init__(self):
        for name in ("tokens_per_minute", "requests_per_minute"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            type(self.max_wait_seconds) not in (int, float)
            or not math.isfinite(self.max_wait_seconds)
            or not 0 < self.max_wait_seconds <= 300
        ):
            raise ValueError("max_wait_seconds must be positive and at most 300")
        if type(self.max_waiters) is not int or not 1 <= self.max_waiters <= 100:
            raise ValueError("max_waiters must be between 1 and 100")


def admission_rate_settings(policy):
    """Safe deployment configuration, rather than a claim about provider quota."""
    return {
        "provider_rate_tpm": policy.tokens_per_minute if policy else None,
        "provider_rate_rpm": policy.requests_per_minute if policy else None,
        "provider_rate_queue_seconds": policy.max_wait_seconds if policy else 0,
    }


class ProviderRateLimiter:
    """FIFO weighted admission respecting both rolling 60-second ceilings.

    Count preflights acquire one RPM permit without tokens. Inference acquires
    one RPM permit and exact input plus maximum output tokens. A request holds
    no limiter lock while waiting. The gateway's existing semaphore separately
    bounds the complete HTTP/count/inference lifecycle.
    """

    def __init__(self, policy, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.policy = policy
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._waiters = deque()
        self._history = deque()
        self._tokens = 0
        self._requests = 0

    def _signal(self):
        self._changed.set()
        self._changed = asyncio.Event()

    def _expire(self, now):
        while self._history and self._history[0][0] + 60 <= now:
            _, tokens, requests = self._history.popleft()
            self._tokens -= tokens
            self._requests -= requests

    async def _wait(self, changed, delay):
        notice = asyncio.create_task(changed.wait())
        timer = asyncio.create_task(self._sleep(delay))
        try:
            done, _ = await asyncio.wait((notice, timer), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            notice.cancel()
            timer.cancel()
            await asyncio.gather(notice, timer, return_exceptions=True)

    async def _remove(self, ticket):
        async with self._lock:
            self._waiters.remove(ticket)
            self._signal()

    def deadline(self):
        """One bounded deadline shared by a count and its inference permit."""
        return self._clock() + self.policy.max_wait_seconds

    async def acquire(self, *, tokens, requests=1, deadline=None):
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens must be a nonnegative integer")
        if type(requests) is not int or requests <= 0:
            raise ValueError("requests must be a positive integer")
        if tokens > self.policy.tokens_per_minute or requests > self.policy.requests_per_minute:
            raise ProviderRateError("gateway_request_exceeds_rate_limit")

        ticket = object()
        own_deadline = self.deadline()
        if deadline is not None:
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                raise ValueError("deadline must be a finite monotonic timestamp")
            own_deadline = min(own_deadline, deadline)
        deadline = own_deadline
        async with self._lock:
            if len(self._waiters) >= self.policy.max_waiters:
                raise ProviderRateError("gateway_rate_queue_full")
            self._waiters.append(ticket)
            self._signal()
        queued = True
        try:
            while True:
                async with self._lock:
                    now = self._clock()
                    self._expire(now)
                    first = self._waiters[0] is ticket
                    if now >= deadline:
                        raise ProviderRateError("gateway_rate_wait_expired")
                    if (
                        first
                        and self._tokens + tokens <= self.policy.tokens_per_minute
                        and self._requests + requests <= self.policy.requests_per_minute
                    ):
                        self._waiters.popleft()
                        queued = False
                        self._history.append((now, tokens, requests))
                        self._tokens += tokens
                        self._requests += requests
                        self._signal()
                        return
                    delay = deadline - now
                    if first and self._history:
                        delay = min(delay, self._history[0][0] + 60 - now)
                    changed = self._changed
                await self._wait(changed, delay)
        finally:
            if queued:
                # Cancellation must not strand the FIFO head or return permits
                # already consumed by this request's earlier count preflight.
                await asyncio.shield(self._remove(ticket))
