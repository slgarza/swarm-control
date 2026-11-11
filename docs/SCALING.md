# Larger projects and 100 agents

The ten-agent pilot exercised small useful tasks on one repository with disjoint
ownership. Large existing codebases need their own baseline, architecture map,
useful backlog and acceptance criteria. The extraction is not evidence of 100-agent
operation or autonomous end-to-end review. The runner and gateway now accept up to
100 tasks per cohort, with offline tests for bounded dispatch, shared ownership,
three-way integration and accounting at that size. This is implementation capacity;
the qualifying live run and AFK comparison have not happened.

## Implemented controls

| Control | Behavior |
| --- | --- |
| Cohort admission | `budget.max_live_agents` accepts 1–100, defaults to ten; disabled/funding gates still apply |
| Dispatch | At most the requested number of submitted workers; completion checkpoints happen before replacement submissions |
| Shared ownership | `--allow-overlap` on `plan`/`run` records overlapping pairs; each worker still edits only its declared paths |
| Integration | Strict application first; opted-in candidates can use three-way application, full combined validation and an additional applied-diff review hash |
| Conflicts | Durable `needs_repair` receipt and cleaned partial application; no automatic repair or replacement spend |
| Gateway capacity | One serving container, 120 HTTP inputs; configurable 1–100 provider slots, default 20, with at most 15 seconds of capacity waiting |
| Provider pacing | Optional paired token/request rolling-minute permits, verified input plus maximum output weight, FIFO queue of at most 100 waiters and one shared 300-second deadline |
| Coding duration | Default 300 seconds; explicit 30–1800 seconds, lifecycle `timeout + 900`, gateway TTL `max(1800, timeout + 900)` within the 3600-second ceiling |
| Preparation inspection | Read-only `preflight`; optional GitHub GETs, separate source/admission/live blockers and no launch approval |
| Pagination | Open PRs and exact-head checks are read across bounded pages; incomplete/inconsistent responses stop admission/merge |

An exhausted gateway queue returns `429 gateway_busy` before reserving budget or
calling the provider. The Codex retry bounds still apply. For a synchronized cohort
of 100, configure `runtime.gateway_concurrency = 100` only after confirming actual
API capacity, then redeploy. Synchronized live runs exceeding the profile's gateway
capacity are rejected before allocation; plans report capacity insufficiency.
Before sandbox launch the runner also checks the capacity returned by authenticated
gateway admission. Cohorts above 20 require that deployed capacity field. A failed
check stops the run with allocation retained; the operator must still verify the
HTTP URL belongs to that gateway app.
Provisioning, model quota and CI throughput must all be measured in live cohorts.

Token-count preflights and inference each consume an RPM permit; inference also
reserves verified input plus maximum output token permits. This optional shared
300-second pacing deadline includes count elapsed time and occurs **after** money
reservation. Pacing failures/cancellation retain the reservation, unlike the
15-second pre-reservation gateway-slot rejection. The runner verifies exact
configured/deployed pacing values before launch. Pacing keeps an idle container
warm for 90 seconds (otherwise 30) to cover its 60-second window. History is
process-local and resets on crash/redeployment/container restart; it does not
coordinate other account traffic or prove provider quota. See
[gateway policy](GATEWAY.md).

Choose a longer session only with its actual allowed budget and expected request
profile. Explicit `--timeout 1800` uses a 2700-second sandbox lifecycle/gateway
TTL, while default 300 keeps lifecycle 1200 and TTL 1800. The HTTP handler and
worker stream idle timeout are 900 seconds. Longer coding sessions and queued
requests increase occupied compute and can still time out or exhaust allowance;
they do not authorize refunds, replacement workers or automatic replenishment.
Record all waits, failures and the review/integration tail in time and cost.

All claimed coding contributors need real assigned work and actual command/model
and patch evidence. A model queue can coexist with real agents, but waiting/idle
containers do not demonstrate simultaneous useful model activity. Report measured
overlap and observation gaps separately from requested capacity. A batch-queue
limit is not an interactive daily token cap without authoritative confirmation.
Use [deployment preflight](DEPLOYMENT_PREFLIGHT.md) to expose preparation and
admission gaps before a separately authorized deployment; exit zero is not launch
readiness or provider admission.

## Remaining operational work

1. Validate a small cohort on a second real public repository. Private targets
   first need a scoped authentication/clone adapter. The
   [target assessment](contest/TARGET-ASSESSMENT.md) keeps Afterimage for the next
   collision pilot and defines the gate for a more mature final benchmark target.
2. Add distributed leases and heartbeats before multiple controller hosts. Existing
   local completion checkpoints and locks cannot coordinate separate clones.
3. Improve the integration queue and assign repair owners. Keep checks on combined
   changes; faster patch generation alone does not establish delivery velocity.
4. Measure model API and Modal capacity under the implemented backpressure, and preserve budget
   history when explicitly authorizing each new stage.
5. Exercise shared-file conflicts, repair, CI contention and reverts. Disjoint
   ownership alone does not satisfy the prize's collision requirement.
6. Scale 25 → 50 → 100 with enough useful unblocked tasks and evidence of each
   active agent's commands, patch, tests and delivery. Idle containers do not count.

Compare the same initial commit, backlog and acceptance criteria against an AFK
baseline. Measure merged green PRs per hour including reviews, CI and retries, and
per dollar with reconciled billing. Current accounting ceilings are not invoices.

A persistent control service and shared database can later replace the local
coordinator. A dashboard should show actual tasks, workers, patches, CI, PRs,
failures and budget from that service. No dashboard is implemented yet.

## Gates for each stage

| Stage | Before starting | Evidence needed before expanding |
| --- | --- | --- |
| Local tutorial | Controller installed, separate target generated | Passing baseline, readable plan and live execution disabled |
| Two real agents | Own service accounts, useful tasks, passing CI and authorized cumulative allowance | Reviewed merged changes, preserved artifacts and confirmed cleanup |
| Ten agents on another real project | Enough independent ready work and confirmed API/VM capacity | Delivery duration, actual cost, quality and integration bottlenecks |
| 25 then 50 | Reviewed capacity profile, verified provider quota, assigned repair owners and authorized remaining budget | Meaningful overlap, combined correctness, failure recovery and measured delivery rates |
| 100 | At least 100 useful unblocked tasks, gateway capacity/quota for the cohort, auditable concurrency and a strong AFK comparison plan | At least 100 useful simultaneous sessions plus superior merged green PRs/hour and PRs/dollar |

The gateway has a USD180 hard per-run model ceiling. Its administrative default
remains USD18, and an existing project's durable policy may be much smaller. This
does not authorize a new allowance or migrate one. The runner retains bounded
timeouts, one local coordinator and assisted serial integration. A larger worker
count can increase an integration backlog without improving delivered PRs/hour.
Clean three-way application handles compatible edits; semantic conflicts, failed
combined checks and CI contention still need observed recovery at each stage.

Stop expansion when integration queues, quality, cost or recovery cannot meet the
agreed criteria. Preserve failed attempts and prior allocations. A new approved
budget requires the [explicit migration workflow](BUDGET_MIGRATION.md) for an
existing ledger. Read-only proposals do not authorize increases; separately
approved apply preserves all existing allocations and keeps execution disabled.

Use the [benchmark protocol](contest/BENCHMARK.md) and
[evidence checklist](contest/EVIDENCE.md) throughout, and keep the
[presentation's claims](contest/PRESENTATION.md) aligned with measured results.
