# Presenting Swarm Control

Status reviewed: November 13, 2026. This is a presentation outline, not a
completed competition submission or a statement of current organizer rules.

## The claim supported by the current product

Swarm Control is a local dashboard and CLI for taking a GitHub repository and a
goal through a reviewed plan, actual issue/task preparation, isolated coding
sessions in Modal, recoverable patches and explicitly reviewed PRs. Existing
prepared backlogs can skip AI planning. Private repositories can use a frozen
source bundle without giving workers GitHub credentials. The target supplies its
own code, instructions and CI.

The interface separates Projects, Plan work, New run, Runs, Settings and Project
settings. Generating a plan is an explicitly authorized model call; applying it
separately authorizes issue and file creation. Saving a run starts no agents.
Worker execution, allowances, deployment, review and integration have their own
gates. Follow [AI planning](../AI_PLANNING.md), [run configuration](../RUNS.md) and
[private repository transport](../PRIVATE_REPOSITORIES.md) for the current workflow.

The original Afterimage pilot ran ten real coding agents and delivered ten merged
green PRs. One delivery required a coordinator code repair. The standalone
controller has offline regression coverage and a reproducible local tutorial.
Its runner/gateway now accept explicitly configured cohorts up to 100, with
bounded dispatch, completion checkpoints and opted-in shared-file integration.
The November 11 run then observed 100 distinct worker VMs, coding threads,
command activity and known model completions. All 100 original reports ended
`needs_repair`; 63 patches contained code and 37 were empty. Its separately
attributed local recovery is documented in the
[hundred-worker report](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot100-2026-11-11.md).
It has **not established 100 simultaneously useful contributors, live collision
handling at that scale or an efficiency advantage over the best AFK pipeline**.

The benchmark objectives below reflect the project's working interpretation of
an organizer announcement previously supplied in discussion. No current public
primary-source rulebook, submission deadline, prize amount or exemption policy
has been verified for this guide. Confirm those terms directly with the organizer
before planning a submission; this outline supplies no deadline or eligibility
guarantee.

## Suggested seven-minute presentation

| Time | Slide / demonstration | What to say or show |
| --- | --- | --- |
| 0:00–0:45 | Problem and goal | A real backlog must become accepted, merged code. More simultaneous agents only help if delivery improves per hour and per dollar. |
| 0:45–1:45 | System architecture | Show the [component diagram](../ARCHITECTURE.md). Explain separate controller, target repository, worker VMs, model gateway and GitHub CI. |
| 1:45–2:45 | Reproducibility | Show the [local tutorial](../QUICKSTART.md), select the example in Projects and inspect a two-task run. State that local checks and a saved draft launch no agents. Explain that optional AI generation is a separate paid request. |
| 2:45–4:00 | Real pilot evidence | Open both cohort reports. Show the hundred-worker original failures, recovery attribution and a PR's reviewed head, combined checks and merge. A corrective PR is not an extra feature. |
| 4:00–5:00 | Coordination and budget | Explain default disjoint ownership, opt-in shared-file work, three-way application, combined validation, completion checkpoints and bounded gateway capacity. Show that actual conflicts still require repair. |
| 5:00–6:15 | Benchmark test | Show the working objectives and [comparison protocol](BENCHMARK.md). Explain how useful overlap, delivery rate, cost and interventions will be measured; distinguish these from unverified organizer terms. |
| 6:15–7:00 | Next milestone | Explain the measured worker and integration failure modes, then propose bounded experiments, a live collision workload and a strong paired AFK comparison with approved budgets. |

For a short opening:

> Swarm Control separates the orchestration system from the software it changes.
> You bring a repository and a goal or reviewed backlog. Independent agents work
> in Modal,
> while a controller preserves their patches, budgets and test evidence. Changes
> reach GitHub through review and CI. We ran ten workers and then one hundred.
> The larger run exposed failed handoffs and assisted recovery. The evidence
> separates physical scale from useful delivery; an AFK advantage is still unproven.

Do not introduce either pilot as fully unattended. The operator prepared tasks,
invoked independent review, made documented repairs and managed integration.

## Historical live evidence: November 10–11, 2026

The authoritative project records are the [pilot report](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot10-2026-11-10.md)
and its [sanitized JSON evidence](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot10-2026-11-10.json).
They describe batch `batch-8d3aa952ff1f`, starting from Afterimage commit
`a190f7544729655b881f500bba0b6f47fddba34a` on November 10, 2026.

| Measure | Observed value | Interpretation |
| --- | --- | --- |
| Independent workers | 10 | Ten Codex processes in ten Modal VMs |
| Delivered PRs | 10 | [PRs 21–30](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot10-2026-11-10.md#delivered-code); nine without coordinator code repair, one with repair |
| Worker phase | 153.094 seconds | Includes worker checks and recovery, not full delivery |
| First worker start to final feature merge | 30 minutes 37 seconds | Includes review, combined validation and CI; excludes earlier preparation, probes and admission |
| Rate over that interval | 19.596 merged green PRs/hour | One small batch, not a sustained throughput result |
| Ten-process overlap | 78.243 seconds | Process lifetime evidence |
| Ten live sessions after a recorded completed command | At least 12.430 seconds | Conservative session evidence; incomplete early event timestamps |
| Completed shell commands / changed files | 72 / 21 | Supporting activity evidence, not independent measures of engineering value |
| Cohort model accounting ceiling | USD5.367561 | Conservative gateway accounting, not an invoice |
| Model ceiling including two probes | USD5.422174 | Still not reconciled provider billing |
| Total billed cost / cost per delivery | Unknown | Gateway, builds, network, storage and other charges need reconciliation |
| Comparison against AFK | Not run | No efficiency advantage established |

The earlier report's VM CPU/memory estimate excludes other charges. Do not use it
as total infrastructure cost. Process or session overlap also does not establish
that every worker performed useful computation continuously.

## Requirements and remaining proof

For the hundred-worker run, use its dated
[machine-readable evidence](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot100-2026-11-11.json)
and linked methods, rather than carrying the ten-agent rates forward. It preserves
all original outcomes, local repair attempts and the original T0 clock. Sampled
VM counts and process envelopes contain observation or idle gaps; model-request
latencies and actual billing remain unavailable. Dollar rates cannot be inferred
from admission reserves or conservative accounting ceilings. Recovery counts
are not upgraded by this guide: any completed quality observation needs its own
dated receipt.

| Working benchmark objective | Recorded position | Evidence still needed |
| --- | --- | --- |
| Real codebase and backlog | Afterimage pilot delivered foundation modules | Larger independently reviewed backlog and acceptance criteria |
| Minimum 100 agents live on the same repository | Hundred-worker VM/process and per-task command/model activity observed | Stronger evidence of simultaneous useful work; process envelopes alone are insufficient |
| Useful agents rather than idle containers | All 100 had command activity and known model completions; 63 produced nonempty patches, all original reports failed validation | Useful contribution and recovery attribution, quality observation and a repeatable successful cohort |
| Handle collisions and CI contention | Opt-in shared ownership, clean three-way application, combined checks and durable conflict receipts tested offline | Live shared-file scenarios, repair, CI contention and combined validation at scale |
| Merged, passing PRs | Ten-agent deliveries plus separately reported assisted hundred-worker recoveries | Completed quality observation and repeatable delivery on the benchmark workload |
| Beat best available AFK on PRs/hour | No comparison | Fair repeated runs against a strong baseline |
| Beat best available AFK on PRs/dollar | Billing incomplete; no comparison | Reconciled eligible cost for both pipelines under the same rules |
| Replicable system | Standalone controller and local example | Another operator reproduces a paid run using their own accounts |

Apply an internal-token exemption only if the organizer confirms that rule and
the specific qualifying charges. Record it separately; an ordinary OpenAI API
invoice is not automatically exempt.

## What to demonstrate live

The local example is appropriate for a presentation with no spending approval:
follow [Quickstart](../QUICKSTART.md), show the two tasks and the plan, then open
the historical PR evidence. Label the live screen as **local planning** and the
pilot report as **a previous real run**.

The [current interface gallery](../DASHBOARD.md) uses controlled example data.
Its reviewed-plan screen is a rendered illustration, not evidence of a model
request, published issues, live workers or accepted deliveries.

An actual two-agent demo follows the [execution walkthrough](../TWO_AGENT_WALKTHROUGH.md)
using a fresh reviewed target, real issues and an authorized budget. Allow time
for setup, model calls, review and CI. Show a failed result honestly if the run
does not complete; a prepared video or report must be labeled as recorded.

Evidence for the stated scale and efficiency objectives needs real 100-agent
evidence and a completed AFK comparison. A chart drawn from plans, allocated
containers or projected costs cannot replace those results.

## Reviewer package

Publish a controller commit/tag, setup instructions, target/base identifiers,
reviewed task manifest, methodology, sanitized run summaries, PR/check links,
cost reconciliation and known limitations. Keep private logs and credentials
outside the public repository. Use the [evidence checklist](EVIDENCE.md) to build
the package and the [scaling roadmap](../SCALING.md) to track implementation gaps.
