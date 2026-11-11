"""Modal deployment for S02. No API credential is sent to a coding worker."""

import asyncio
import json
import os
import time

import modal

from .pilot_budget import (
    COUNT_FIELDS,
    MAX_BODY_BYTES,
    BudgetService,
    PolicyError,
    SSEUsage,
    prepare_request,
)
from .provider_rate import (
    ProviderRateError,
    ProviderRateLimiter,
    ProviderRatePolicy,
    admission_rate_settings,
)
from .request_metrics import RequestMetrics


def configured_gateway_concurrency():
    value = os.environ.get("SWARM_GATEWAY_CONCURRENCY", "20")
    if len(value) > 3 or not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 100:
        raise ValueError("SWARM_GATEWAY_CONCURRENCY must be an integer between 1 and 100")
    return int(value)


def configured_provider_rate():
    values = [os.environ.get(name) for name in ("SWARM_GATEWAY_TPM", "SWARM_GATEWAY_RPM")]
    if values == [None, None]:
        return None
    if any(
        value is None or len(value) > 18 or not value.isascii()
        or not value.isdecimal() or int(value) <= 0
        for value in values
    ):
        raise ValueError("SWARM_GATEWAY_TPM and SWARM_GATEWAY_RPM require paired positive integers")
    return ProviderRatePolicy(*(int(value) for value in values))


# Validate deployment settings before constructing Modal resources. The same
# value is embedded into the image so container imports retain the policy.
MAX_UPSTREAM_REQUESTS = configured_gateway_concurrency()
PROVIDER_RATE = configured_provider_rate()
APP_NAME = os.environ.get("SWARM_GATEWAY_APP", "swarm-control-gateway")
DICT_NAME = os.environ.get("SWARM_GATEWAY_LEDGER", "swarm-control-budget-v1")
SECRET_NAME = os.environ.get("SWARM_OPENAI_SECRET", "swarm-control-openai")
UPSTREAM = "https://api.openai.com/v1/responses"
REQUEST_SECONDS = 120
CAPACITY_WAIT_SECONDS = 15
app = modal.App(APP_NAME)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .env({"SWARM_GATEWAY_APP": APP_NAME, "SWARM_GATEWAY_LEDGER": DICT_NAME,
          "SWARM_GATEWAY_CONCURRENCY": str(MAX_UPSTREAM_REQUESTS),
          "SWARM_OPENAI_SECRET": SECRET_NAME,
          **({"SWARM_GATEWAY_TPM": str(PROVIDER_RATE.tokens_per_minute),
              "SWARM_GATEWAY_RPM": str(PROVIDER_RATE.requests_per_minute)}
             if PROVIDER_RATE else {})})
    .pip_install("fastapi==0.115.12", "httpx==0.28.1")
    .add_local_python_source("swarm_control")
)
ledger = modal.Dict.from_name(DICT_NAME, create_if_missing=True)


class ModalStore:
    async def get(self, key):
        return await ledger.get.aio(key)

    async def put(self, key, value, *, skip_if_exists=False):
        return await ledger.put.aio(key, value, skip_if_exists=skip_if_exists)

    async def pop(self, key):
        return await ledger.pop.aio(key, None)


@app.function(image=image, timeout=60, cpu=0.125, memory=256)
async def create_run(
    run_id: str,
    task_ids: list[str],
    model_budget_usd: float = 18,
    ttl_seconds: int = 1800,
):
    """Workspace-authenticated SDK function; caller must never print its tokens."""
    issued = await BudgetService(ModalStore()).create(
        run_id, task_ids, model_budget_usd, ttl_seconds, time.time()
    )
    return {**issued, "gateway_concurrency": MAX_UPSTREAM_REQUESTS,
            **admission_rate_settings(PROVIDER_RATE)}


@app.function(image=image, timeout=60, cpu=0.125, memory=256)
async def run_status(run_id: str):
    return await BudgetService(ModalStore()).status(run_id, time.time())


@app.function(image=image, timeout=60, cpu=0.125, memory=256)
async def stop_run(run_id: str):
    return await BudgetService(ModalStore()).stop(run_id, time.time())


def build_web_app(
    service, api_key, *, transport=None,
    max_upstream_requests=MAX_UPSTREAM_REQUESTS,
    capacity_wait_seconds=CAPACITY_WAIT_SECONDS,
    provider_rate=PROVIDER_RATE,
    rate_limiter=None,
    request_metrics=None,
):
    """Injectable HTTP app; tests may use an in-memory store/mock transport."""
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from starlette.background import BackgroundTask
    from starlette.requests import ClientDisconnect

    if type(max_upstream_requests) is not int or not 1 <= max_upstream_requests <= 100:
        raise ValueError("max_upstream_requests must be between 1 and 100")
    if type(capacity_wait_seconds) not in (int, float) or not 0 < capacity_wait_seconds <= 15:
        raise ValueError("capacity_wait_seconds must be positive and at most 15")
    capacity = asyncio.BoundedSemaphore(max_upstream_requests)
    if rate_limiter is None and provider_rate is not None:
        rate_limiter = ProviderRateLimiter(provider_rate)
    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    metrics = request_metrics if request_metrics is not None else RequestMetrics()
    web.state.request_metrics = metrics

    def error_response(code, status, *, headers=None):
        return JSONResponse(
            {
                "error": {
                    "type": "swarm_gateway_error",
                    "code": code,
                    "message": code,
                }
            },
            status_code=status,
            headers=headers,
        )

    @web.exception_handler(PolicyError)
    async def policy_error(_request, exc):
        return error_response(exc.code, exc.status)

    async def handle_response(request: Request, timing):
        authorization = request.headers.get("authorization", "")
        with timing.phase("authenticate"):
            try:
                if not authorization.startswith("Bearer ") or len(authorization) > 100:
                    raise PolicyError("invalid_token", 401)
                run_id, task_id = await service.authenticate(authorization[7:], time.time())
            except PolicyError:
                timing.outcome("authentication_rejected")
                raise
        timing.bind(run_id, task_id)
        with timing.phase("body"):
            if request.headers.get("content-encoding", "identity") != "identity":
                raise PolicyError("compressed_requests_not_supported", 415)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise PolicyError("json_required", 415)
            raw = bytearray()
            try:
                async with asyncio.timeout(15):
                    async for chunk in request.stream():
                        raw.extend(chunk)
                        if len(raw) > MAX_BODY_BYTES:
                            raise PolicyError("request_too_large", 413)
            except (TimeoutError, ClientDisconnect) as exc:
                raise PolicyError("request_body_interrupted", 408) from exc
            body, upper = prepare_request(bytes(raw))
        # A bounded queue absorbs synchronized cohorts. Capacity rejection makes
        # no provider call and acquires no budget reservation; a retry still
        # uses the same task/run allowance.
        try:
            with timing.phase("capacity"):
                async with asyncio.timeout(capacity_wait_seconds):
                    await capacity.acquire()
        except TimeoutError:
            timing.outcome("capacity_rejected")
            return error_response("gateway_busy", 429, headers={"Retry-After": "1"})
        try:
            with timing.phase("reserve"):
                pending = await service.reserve(
                    run_id, task_id, upper, body["max_output_tokens"], time.time()
                )
            timing.flag("reservation_admitted")
            request_id = pending["id"]
            client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(30, read=REQUEST_SECONDS),
            )
        except BaseException:
            timing.outcome("reservation_rejected")
            capacity.release()
            raise
        headers = {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        upstream = None
        parser = SSEUsage()
        outcome = "unknown_usage"
        closed = False
        count_recorded = False
        rate_deadline = rate_limiter.deadline() if rate_limiter is not None else None

        async def cleanup():
            nonlocal closed
            if closed:
                return
            closed = True
            with timing.phase("cleanup"):
                try:
                    if upstream is not None:
                        await upstream.aclose()
                    await client.aclose()
                finally:
                    # Failed settlement leaves the original durable reservation in
                    # place. Do not print exception bodies or retry uncertain writes.
                    try:
                        with timing.phase("settlement"):
                            await service.finish(
                                run_id,
                                task_id,
                                request_id,
                                parser.event if not parser.failed else None,
                                outcome,
                            )
                        timing.flag("settlement_returned")
                    except Exception:  # noqa: BLE001 - uncertain writes must never be retried/refunded
                        print("gateway_settlement_failed")
                    finally:
                        capacity.release()
                        timing.flag("cleanup_finished")
                        timing.mark("cleanup_finished")

        try:
            # Exact provider token counting handles tool schemas and opaque
            # reasoning. There is no inference request if this preflight fails.
            count_body = {
                key: value for key, value in body.items() if key in COUNT_FIELDS
            }
            if rate_limiter is not None:
                # Count preflights also consume RPM. A permit remains consumed
                # after cancellation/failure; it is independent of money.
                with timing.phase("count_pacing"):
                    await rate_limiter.acquire(tokens=0, deadline=rate_deadline)
            with timing.phase("count_http"):
                timing.flag("count_http_attempted")
                count_response = await client.post(
                    UPSTREAM + "/input_tokens", headers=headers, json=count_body
                )
            await service.rate_limits(run_id, dict(count_response.headers))
            count = None
            if count_response.status_code == 200:
                try:
                    count_data = count_response.json()
                    if count_data.get("object") == "response.input_tokens":
                        count = count_data.get("input_tokens")
                except (ValueError, AttributeError):
                    pass
            try:
                with timing.phase("record_count"):
                    await service.record_count(
                        run_id, task_id, request_id, count, time.time()
                    )
            except PolicyError as exc:
                if count_response.status_code != 200:
                    raise PolicyError(
                        "upstream_count_rate_limit"
                        if count_response.status_code == 429
                        else "upstream_count_failed",
                        429 if count_response.status_code == 429 else 502,
                    ) from exc
                raise
            finally:
                count_recorded = True
            if rate_limiter is not None:
                # Only the provider-verified count may authorize token pacing.
                # Reserve maximum output as well; no cache discount or refund.
                with timing.phase("inference_pacing"):
                    await rate_limiter.acquire(
                        tokens=count + body["max_output_tokens"], deadline=rate_deadline,
                    )
            # Recheck the independent cancellation key immediately before send.
            with timing.phase("recheck"):
                await service.authenticate(authorization[7:], time.time())
            with timing.phase("inference_send"):
                timing.flag("inference_http_attempted")
                upstream = await client.send(
                    client.build_request("POST", UPSTREAM, headers=headers, json=body),
                    stream=True,
                )
            await service.rate_limits(run_id, dict(upstream.headers))
            if upstream.status_code != 200:
                outcome = "upstream_rejected"
                timing.outcome("upstream_rejected")
                status = 429 if upstream.status_code == 429 else 502
                await cleanup()
                return error_response(
                    "upstream_rate_limit" if status == 429 else outcome, status
                )
            if "text/event-stream" not in upstream.headers.get("content-type", ""):
                outcome = "upstream_stream_required"
                timing.outcome("stream_required")
                await cleanup()
                return error_response(outcome, 502)
        except ProviderRateError as exc:
            # The earlier durable money reservation stays conservatively
            # retained even when no inference was sent. Never claim a refund.
            outcome = exc.code
            timing.outcome("pacing_rejected")
            await cleanup()
            return error_response(outcome, 429, headers={"Retry-After": "60"})
        except PolicyError:
            outcome = "preflight_rejected"
            timing.outcome("request_rejected")
            await cleanup()
            raise
        except (httpx.HTTPError, TimeoutError):
            outcome = "upstream_connection_failed"
            timing.outcome("connection_failed")
            if not count_recorded:
                try:
                    await service.record_count(
                        run_id, task_id, request_id, None, time.time()
                    )
                except PolicyError:
                    pass
            await cleanup()
            return error_response(outcome, 502)
        except BaseException:
            # Cancellation while connecting must close sockets and retain cost.
            await asyncio.shield(cleanup())
            raise

        async def stream():
            nonlocal outcome
            try:
                with timing.phase("stream"):
                    try:
                        async with asyncio.timeout(REQUEST_SECONDS):
                            next_check = 0
                            first_byte = True
                            async for chunk in upstream.aiter_bytes():
                                if chunk and first_byte:
                                    timing.mark("first_byte")
                                    first_byte = False
                                if time.monotonic() >= next_check:
                                    with timing.phase("stream_auth"):
                                        await service.authenticate(authorization[7:], time.time())
                                    next_check = time.monotonic() + 1
                                parser.feed(chunk)
                                if parser.event is not None and not parser.failed:
                                    timing.mark("completion_event")
                                    timing.flag("completion_event_seen")
                                    timing.outcome("completion_observed")
                                    # Commit usage before the client sees completion and
                                    # immediately begins its next turn with the same token.
                                    outcome = "completed"
                                    await cleanup()
                                    yield chunk
                                    return
                                yield chunk
                            outcome = (
                                "missing_completed_usage"
                                if parser.event is None
                                else "completed"
                            )
                            timing.outcome("usage_unverified")
                    except (httpx.HTTPError, TimeoutError, PolicyError):
                        outcome = "stream_interrupted"
                        timing.outcome("stream_interrupted")
                        # No upstream body/exception details are copied into errors.
                        yield b'event: error\ndata: {"type":"error","code":"stream_interrupted","message":"stream_interrupted"}\n\n'
                    except asyncio.CancelledError:
                        timing.mark("cancelled")
                        timing.outcome("cancelled")
                        raise
            finally:
                try:
                    await asyncio.shield(cleanup())
                finally:
                    timing.finish()

        async def background_cleanup():
            try:
                await cleanup()
            finally:
                timing.finish("response_not_consumed" if timing.current_outcome == "unknown" else None)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            background=BackgroundTask(background_cleanup),
        )

    @web.post("/v1/responses")
    async def responses(request: Request):
        timing = metrics.begin()
        try:
            response = await handle_response(request, timing)
        except asyncio.CancelledError:
            timing.mark("cancelled")
            timing.finish("cancelled")
            raise
        except PolicyError:
            timing.finish("request_rejected" if timing.current_outcome == "unknown" else None)
            raise
        except BaseException:
            timing.finish("handler_failed")
            raise
        if not isinstance(response, StreamingResponse):
            timing.finish()
        return response

    return web


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    max_containers=1,
    min_containers=0,
    # Keep a paced gateway warm beyond its 60s history during normal idleness.
    # Crashes/redeployments can still reset this process-local limiter.
    scaledown_window=90 if PROVIDER_RATE else 30,
    timeout=900,
    cpu=0.25,
    memory=256,
)
@modal.concurrent(max_inputs=120)
@modal.asgi_app()
def gateway():
    return build_web_app(
        BudgetService(ModalStore()), os.environ["CODEX_API_KEY"],
        request_metrics=RequestMetrics(emit=lambda record: print(json.dumps(record, separators=(",", ":")))),
    )
