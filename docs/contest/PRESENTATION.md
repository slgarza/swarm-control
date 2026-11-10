# Presenting Swarm Control

## The claim we can support today

Swarm Control is a reusable controller that turns prepared tasks in a public
GitHub repository into isolated coding sessions in Modal, recoverable patches,
and explicitly reviewed PRs. OpenAI provides the model; Modal provides the worker
machines. The target project supplies its own code, backlog, instructions and CI.

The original Afterimage pilot ran ten real coding agents and delivered ten merged
green PRs. One delivery required a coordinator code repair. The standalone
controller has offline regression coverage and a reproducible local tutorial.
Its runner/gateway now accept explicitly configured cohorts up to 100, with
bounded dispatch, completion checkpoints and opted-in shared-file integration.
It has **not yet demonstrated the 100-agent requirement or an efficiency advantage
over the best AFK pipeline**.

This document is a presentation outline, not a completed prize submission.
The requirements below come from the announcement provided by the organizer in
the project discussion. That announcement specifies a December 1 review; confirm
the submission format and applicable review date with the organizer.

## Suggested seven-minute presentation

| Time | Slide / demonstration | What to say or show |
| --- | --- | --- |
| 0:00–0:45 | Problem and goal | A real backlog must become accepted, merged code. More simultaneous agents only help if delivery improves per hour and per dollar. |
| 0:45–1:45 | System architecture | Show the [component diagram](../ARCHITECTURE.md). Explain separate controller, target repository, worker VMs, model gateway and GitHub CI. |
| 1:45–2:45 | Reproducibility | Show the [local tutorial](../QUICKSTART.md): create a separate target, run two baseline tests, inspect a two-task plan. State that this step launches no agents. |
| 2:45–4:00 | Real pilot evidence | Open the original report and one delivered PR. Distinguish generated patch, local validation, semantic review, CI and actual merge. Show the recorded coordinator repair. |
| 4:00–5:00 | Coordination and budget | Explain default disjoint ownership, opt-in shared-file work, three-way application, combined validation, completion checkpoints and bounded gateway capacity. Show that actual conflicts still require repair. |
| 5:00–6:15 | Competition test | Show the requirements table and [comparison protocol](BENCHMARK.md). Explain how useful overlap, delivery rate, cost and interventions will be measured. |
| 6:15–7:00 | Next milestone | Confirm real provider capacity, run a second target, then evaluate 25, 50 and 100 agents with approved budgets and a strong AFK comparison. |

For a short opening:

> Swarm Control separates the orchestration system from the software it changes.
> You bring a repository and a reviewed backlog. Independent agents work in Modal,
> while a controller preserves their patches, budgets and test evidence. Changes
> reach GitHub through review and CI. Our first ten-agent pilot delivered ten PRs;
> the next challenge is proving that this scales and beats a strong AFK workflow.

Do not introduce the current pilot as fully unattended. The operator prepared
tasks, reviewed changes, repaired one delivery and managed integration.

## Current evidence

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

| Requirement from the announcement | Current position | Evidence still needed |
| --- | --- | --- |
| Real codebase and backlog | Afterimage pilot delivered foundation modules | Larger independently reviewed backlog and acceptance criteria |
| Minimum 100 agents live on the same repository | Ten demonstrated; configurable 100-worker implementation tested offline | At least 100 genuine sessions with meaningful overlapping work |
| Useful agents rather than idle containers | Pilot includes commands, patches and tests | Complete per-agent evidence and contribution attribution at 100 |
| Handle collisions and CI contention | Opt-in shared ownership, clean three-way application, combined checks and durable conflict receipts tested offline | Live shared-file scenarios, repair, CI contention and combined validation at scale |
| Merged, passing PRs | Ten observed | Repeatable delivery and post-merge quality on the benchmark workload |
| Beat best available AFK on PRs/hour | No comparison | Fair repeated runs against a strong baseline |
| Beat best available AFK on PRs/dollar | Billing incomplete; no comparison | Reconciled eligible cost for both pipelines under the same rules |
| Replicable system | Standalone controller and local example | Another operator reproduces a paid run using their own accounts |

The announcement excludes internal tokens from the contest denominator. Record
that treatment separately. Do not assume an ordinary OpenAI API invoice is an
exempt internal charge without confirmation of the applicable rules.

## What to demonstrate live

The local example is appropriate for a presentation with no spending approval:
follow [Quickstart](../QUICKSTART.md), show the two tasks and the plan, then open
the historical PR evidence. Label the live screen as **local planning** and the
pilot report as **a previous real run**.

An actual two-agent demo follows the [execution walkthrough](../TWO_AGENT_WALKTHROUGH.md)
using a fresh reviewed target, real issues and an authorized budget. Allow time
for setup, model calls, review and CI. Show a failed result honestly if the run
does not complete; a prepared video or report must be labeled as recorded.

A qualifying final presentation needs the real 100-agent evidence and completed
AFK comparison. A chart drawn from plans, allocated containers or projected costs
cannot replace those results.

## Reviewer package

Publish a controller commit/tag, setup instructions, target/base identifiers,
reviewed task manifest, methodology, sanitized run summaries, PR/check links,
cost reconciliation and known limitations. Keep private logs and credentials
outside the public repository. Use the [evidence checklist](EVIDENCE.md) to build
the package and the [scaling roadmap](../SCALING.md) to track implementation gaps.
