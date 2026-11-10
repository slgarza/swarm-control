# Budgeted model gateway

The pilot gateway holds the OpenAI credential in the trusted Modal function.
Each coding worker receives a random credential scoped to one task, one run,
and an expiry. The hard limit is USD180 for a run of at most 100 tasks, divided
equally between task credentials. `create_run` still defaults to USD18; the runner
requires an explicit amount within the target's remaining durable allowance.
Larger limits do not authorize spending or migrate existing budgets. In the
original Afterimage pilot, USD7 of its USD25 allowance was reserved for
infrastructure. This module does not enforce or measure Modal billing.

## Deployment and controller interface

Use Modal 1.6.3 and Python 3.12. The deployment builds its pinned FastAPI/httpx
image and references the project-configured Modal secret (`runtime.openai_secret`). Only `gateway`
receives `CODEX_API_KEY`; the administrative functions have no model credential.
`runtime.gateway_concurrency` (integer 1–100, default 20) is passed through
`SWARM_GATEWAY_CONCURRENCY` into the deployed gateway image. Changing that setting
requires redeployment and confirmation of actual upstream API capacity.

```sh
swarm-control deploy-gateway --project /path/to/target/swarm.toml
```

The deployed app is selected by `runtime.gateway_app`; the ASGI function is `gateway`.
Obtain its URL with `modal.Function.from_name(APP_NAME, "gateway").get_web_url()` and
append `/v1` for the Codex provider base URL. The only public route is
`POST /v1/responses`, authenticated with `Authorization: Bearer <task token>`.
No health request makes a model call. Unknown paths return 404.

The following functions require normal Modal workspace authentication; there
are no public HTTP administration endpoints:

```python
create = modal.Function.from_name(GATEWAY_APP, "create_run")
status = modal.Function.from_name(GATEWAY_APP, "run_status")
stop = modal.Function.from_name(GATEWAY_APP, "stop_run")
issued = create.remote(run_id, task_ids, model_budget_usd=18, ttl_seconds=1800)
# issued["tokens"][task_id] is passed directly to that worker in memory.
# Do not print issued or persist the token map in manifests/logs.
report = status.remote(run_id)
final_report = stop.remote(run_id)
```

`create_run` returns the safe report plus a `tokens` mapping. Run IDs are unique,
single-use, 1–64 ASCII letters/digits/underscore/hyphen, starting with a letter
or digit. Duplicate IDs fail, including stopped, expired, or partially created
runs. Use one run for the whole pilot; creating a new run is a new budget,
not recovery of the previous allowance. TTL is 60–1800 seconds. Maximum 60
accepted requests per task. Credentials contain 256 bits of randomness and are
stored only as SHA-256 digests in the durable ledger.

`run_status` and `stop_run` return `run_id`, `model`, `state`, `created_at`,
`expires_at`, `budget_usd`, `maximum_charged_usd`, `committed_usd`,
`retained_usd`, `reserved_usd`, `ledger_locked`, `tasks`, `rate_limits`, and
`billing_verified: false`. Per-task data includes the token hash, USD quota and
the same charge categories, request/completion/error counts, count request/error
counts, token usage, last safe error code, and an `inflight` flag. Count requests
are attempted preflights, not productive coding agents. Selected rate-limit
headers expose request/token limits, remaining quota, and reset intervals;
no other upstream headers are retained.

Configure Codex with a custom provider named `swarm`, `wire_api="responses"`,
`env_key="CODEX_API_KEY"`, `supports_websockets=false`, and the gateway `/v1`
base URL. That environment variable holds only the task credential. Use a
bounded context (32768 tokens for this pilot), disable remote compaction and
web search, and send uncompressed JSON. Streaming Responses, function tools,
custom tools, text tool results, and stateless encrypted reasoning replay are
supported. Codex 0.166's `additional_tools` developer input items and one level
of `functions`/`clock` namespaces are accepted, with at most 64 local
function/custom definitions across the entire request. Nested namespaces and
hosted tools remain forbidden at every level. The gateway strips
`client_metadata` telemetry and `collaboration` tool definitions, even if the
CLI advertises them with multi-agent mode disabled; replayed collaboration
calls are rejected. Hosted tools, images, files, conversations, response-ID
chaining, background mode, compressed bodies, and other API endpoints are
rejected before inference. Output is clamped to 4096 tokens including reasoning.

## Budget boundary and failure behavior

The gateway fixes `gpt-6.1-sol`, `service_tier="default"`, `store=false`, and
`truncation="disabled"`. Requests are limited to 200,000 UTF-8 bytes and a
240,000-token reservation ceiling, below the long-context price threshold.
Before forwarding anything it durably reserves the estimated maximum input
cost plus the maximum permitted output cost. Then the trusted gateway calls
OpenAI's `/v1/responses/input_tokens` with the same input, tools, instructions,
reasoning, and text configuration. The exact count must fit the reservation;
otherwise no generation is sent. The estimate alone never authorizes inference.

Rates checked on 2026-11-10 are USD2 input, USD0.10 cached input, USD2.50 cache
write, and USD10 output per million tokens. The gateway charges **all** input
tokens at USD2.50 and adds a 10% allowance to both rates. Cache writes replace
the uncached-input rate; they are not an additional fee. The allowance also
covers the documented 10% regional premium. Fast/pro/ultrafast overrides,
paid built-in tools, and long-context requests are not admitted. These rates
must be reviewed before a later pilot; this is not provider invoice reporting.

A smaller charge is committed only after a valid `response.completed` event
with coherent provider token usage inside the reserved bounds. The streamed
completion is released to Codex after settlement, allowing its next request
immediately. Failed/incomplete/disconnected requests or unverifiable usage
retain the full reservation. This includes a failed token-count preflight:
there is deliberately no optimistic refund based on an uncertain provider
failure. Inflight requests remain reserved after a crash. They are not reclaimed
or silently retried. A task can have only one outstanding reservation.

The durable Modal Dict is selected by `runtime.gateway_ledger`; Afterimage preserves
its original `afterimage-pilot-budget-v1` identifier. A single ASGI serving container
accepts up to 120 HTTP inputs. A semaphore limits concurrent provider requests to
the configured gateway capacity, default 20 and maximum 100. After authentication
and request validation, requests wait at most 15 seconds for a slot. Timeout returns
`429 gateway_busy` with `Retry-After: 1` before a budget reservation or provider call.
Accepted requests retain a slot through counting, generation and settlement/cleanup.
The bounded Codex retries still apply; rejected requests do not acquire fresh funds.

A synchronized live cohort cannot request more workers than the profile's gateway
capacity. For 100, configure `runtime.gateway_concurrency = 100`, verify quota and
redeploy first. The authenticated `create_run` response advertises deployed
`gateway_concurrency`; the runner verifies enough capacity before synchronized
sandbox launch. Missing capacity is accepted only for cohorts of at most 20 to
support older deployments. A failed capacity check stops the run with its local
allocation retained. The operator must still match the supplied HTTP URL to that
gateway app. Capacity alone does not establish provider quota, throughput or
useful agent concurrency.

Every ledger change obtains an atomic persistent
run lock, as well as the local asyncio lock. An ambiguous storage failure leaves
the run locked and rejects further generation instead of guessing whether a
debit or refund succeeded. Never delete or steal the lock while recovering a
pilot. `run_status` remains readable and `stop_run` works independently of it.
Stop is a separate monotonic key, so an older request cannot overwrite it.
Cancellation prevents subsequent calls; a call already being forwarded may
finish within its existing reservation. Streams check cancellation on chunks
and are bounded to 120 seconds; the Modal request timeout is 180 seconds.

Modal Dict records expire after seven inactive days. Export safe final reports
locally at the end of the run. The ledger is not a permanent accounting system;
never reuse an old run ID after that retention window. Any additional run must
deduct previous committed, retained and reserved amounts from the user's total
allowance. Keep the actual OpenAI project and Modal invoices for reconciliation.

| HTTP status/code | Controller action |
| --- | --- |
| 402 `budget_exhausted` / `request_limit` | Stop the task; do not retry or replenish automatically. |
| 403 `run_stopped` / `run_expired` | Recover artifacts and terminate the worker. |
| 409 `task_inflight` | Do not issue concurrent requests with one credential; inspect the durable pending reservation. |
| 429 `gateway_busy` | Capacity waiting expired before reservation/inference; bounded retry uses the same task/run allowance. Inspect configured slots and actual provider capacity. |
| 429 `upstream_rate_limit` / `upstream_count_rate_limit` | Bounded backoff only; each attempted retry still needs budget. |
| 503 `ledger_locked` | Read status, stop the run, recover artifacts; no lock stealing or budget resets. |
| 400 / 413 / 415 policy errors | Fix the request/provider configuration before another run. |
| 502 provider errors | Inspect safe task status; unknown cost is retained. |

The service does not log prompts, model output, credentials, or raw error
bodies. A settlement storage failure emits only `gateway_settlement_failed`.
Worker credentials authorize only the bounded Responses route and do not
provide access to Modal or the real OpenAI key.

## Validation

Install the same pinned HTTP test dependencies in the local or CI Python
environment; the standard-library subset deliberately skips the HTTP
tests when these packages are absent:

```sh
python -m pip install modal==1.6.3 fastapi==0.115.12 httpx==0.28.1
```

```sh
python -m unittest discover -s tests -p 'test_pilot_budget.py' -v
python -m py_compile swarm_control/pilot_budget.py swarm_control/pilot_gateway.py
ruff check --isolated swarm_control/pilot_budget.py swarm_control/pilot_gateway.py tests/test_pilot_budget.py
```

The offline policy tests cover money validation, 100-task quotas,
concurrency, persistent locks, interrupted writes, restarts, cancellation,
single-use runs, request bounds, forbidden remote tools/context, and incremental
SSE accounting. They also cover the initial Codex request and tool-result
replay shapes, stripped telemetry/collaboration definitions, and rejection of
paid tools hidden inside additional tool items or namespaces. Additional
offline HTTP tests run when `modal==1.6.3`,
`fastapi==0.115.12`, and `httpx==0.28.1` are installed. They use a simulated
upstream and in-memory ledger; no tests read an API key or make model calls. They
exercise capacity exhaustion before reservation and slot release after failures.
Codex 0.166 previously
completed a two-request localhost SSE simulation: both real CLI request shapes
passed the policy, the local printf command executed, and Codex returned READY.
The simulation used a dummy credential and made no model calls. The original
Afterimage live pilot is recorded in its delivery report. This extraction is
validated offline and does not deploy a new gateway or start another paid run.

Sources: [Sol pricing](https://developers.openai.com/api/docs/models/gpt-6.1-sol),
[cache-write semantics](https://developers.openai.com/api/docs/guides/prompt-caching),
[exact input token counting](https://developers.openai.com/api/docs/guides/token-counting),
[Responses completion events](https://developers.openai.com/api/reference/resources/responses/streaming-events),
[Modal Dict atomic insertion and retention](https://modal.com/docs/sdk/py/latest/Dict),
[Modal ASGI concurrency](https://modal.com/docs/guide/webhooks).
