"""Modal deployment for S02. No API credential is sent to a coding worker."""

import asyncio
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


def configured_gateway_concurrency():
    value = os.environ.get("SWARM_GATEWAY_CONCURRENCY", "20")
    if len(value) > 3 or not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 100:
        raise ValueError("SWARM_GATEWAY_CONCURRENCY must be an integer between 1 and 100")
    return int(value)


# Validate deployment settings before constructing Modal resources. The same
# value is embedded into the image so container imports retain the policy.
MAX_UPSTREAM_REQUESTS = configured_gateway_concurrency()
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
          "SWARM_OPENAI_SECRET": SECRET_NAME})
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
    return {**issued, "gateway_concurrency": MAX_UPSTREAM_REQUESTS}


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
    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

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

    @web.post("/v1/responses")
    async def responses(request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 100:
            raise PolicyError("invalid_token", 401)
        run_id, task_id = await service.authenticate(authorization[7:], time.time())
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
            async with asyncio.timeout(capacity_wait_seconds):
                await capacity.acquire()
        except TimeoutError:
            return error_response("gateway_busy", 429, headers={"Retry-After": "1"})
        try:
            pending = await service.reserve(
                run_id, task_id, upper, body["max_output_tokens"], time.time()
            )
            request_id = pending["id"]
            client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(30, read=REQUEST_SECONDS),
            )
        except BaseException:
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

        async def cleanup():
            nonlocal closed
            if closed:
                return
            closed = True
            try:
                if upstream is not None:
                    await upstream.aclose()
                await client.aclose()
            finally:
                # Failed settlement leaves the original durable reservation in
                # place. Do not print exception bodies or retry uncertain writes.
                try:
                    await service.finish(
                        run_id,
                        task_id,
                        request_id,
                        parser.event if not parser.failed else None,
                        outcome,
                    )
                except Exception:  # noqa: BLE001 - uncertain writes must never be retried/refunded
                    print("gateway_settlement_failed")
                finally:
                    capacity.release()

        try:
            # Exact provider token counting handles tool schemas and opaque
            # reasoning. There is no inference request if this preflight fails.
            count_body = {
                key: value for key, value in body.items() if key in COUNT_FIELDS
            }
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
            # Recheck the independent cancellation key immediately before send.
            await service.authenticate(authorization[7:], time.time())
            upstream = await client.send(
                client.build_request("POST", UPSTREAM, headers=headers, json=body),
                stream=True,
            )
            await service.rate_limits(run_id, dict(upstream.headers))
            if upstream.status_code != 200:
                outcome = "upstream_rejected"
                status = 429 if upstream.status_code == 429 else 502
                await cleanup()
                return error_response(
                    "upstream_rate_limit" if status == 429 else outcome, status
                )
            if "text/event-stream" not in upstream.headers.get("content-type", ""):
                outcome = "upstream_stream_required"
                await cleanup()
                return error_response(outcome, 502)
        except PolicyError:
            outcome = "preflight_rejected"
            await cleanup()
            raise
        except (httpx.HTTPError, TimeoutError):
            outcome = "upstream_connection_failed"
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
                async with asyncio.timeout(REQUEST_SECONDS):
                    next_check = 0
                    async for chunk in upstream.aiter_bytes():
                        if time.monotonic() >= next_check:
                            await service.authenticate(authorization[7:], time.time())
                            next_check = time.monotonic() + 1
                        parser.feed(chunk)
                        if parser.event is not None and not parser.failed:
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
            except (httpx.HTTPError, TimeoutError, PolicyError):
                outcome = "stream_interrupted"
                # No upstream body/exception details are copied into errors.
                yield b'event: error\ndata: {"type":"error","code":"stream_interrupted","message":"stream_interrupted"}\n\n'
            finally:
                await asyncio.shield(cleanup())

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            background=BackgroundTask(cleanup),
        )

    return web


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    max_containers=1,
    min_containers=0,
    scaledown_window=30,
    timeout=180,
    cpu=0.25,
    memory=256,
)
@modal.concurrent(max_inputs=120)
@modal.asgi_app()
def gateway():
    return build_web_app(BudgetService(ModalStore()), os.environ["CODEX_API_KEY"])
