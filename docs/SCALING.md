# Larger projects and 100 agents

The ten-agent pilot exercised small useful tasks on one repository with disjoint
ownership. Large existing codebases need their own baseline, architecture map,
useful backlog and acceptance criteria. The extraction is not evidence of 100-agent
operation or autonomous end-to-end review.

1. Validate a small cohort on a second real public repository. Private targets
   first need a scoped authentication/clone adapter.
2. Add durable task leases, heartbeats and checkpoints before multiple controller
   hosts. The current SQLite/host lock cannot coordinate separate clones.
3. Improve the integration queue and assign repair owners. Keep checks on combined
   changes; faster patch generation alone does not establish delivery velocity.
4. Measure model API and Modal capacity, add backpressure, and preserve budget
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
| 25 then 50 | Implemented/tested larger-cohort support, scheduling, backpressure and conflict recovery | Meaningful overlap, combined correctness, failure recovery and measured delivery rates |
| 100 | Sufficient real work, repaired scale limits, auditable concurrency and a strong AFK comparison plan | At least 100 useful simultaneous sessions plus superior merged green PRs/hour and PRs/dollar |

The 25/50/100 rows require implementation work, not just a different CLI flag.
The current gateway caps runs at ten tasks, has a single serving container, and
uses pilot request/budget limits. The runner caps live task counts and timeouts;
admission/integration also stop at conservative GitHub pagination boundaries.
Review and test those constraints together with scheduling and integration.
Raising one limit without addressing the others is insufficient.

Stop expansion when integration queues, quality, cost or recovery cannot meet the
agreed criteria. Preserve failed attempts and prior allocations. A new approved
budget requires an explicit migration design for an existing ledger; no general
migration CLI is provided yet.

Use the [benchmark protocol](contest/BENCHMARK.md) and
[evidence checklist](contest/EVIDENCE.md) throughout, and keep the
[presentation's claims](contest/PRESENTATION.md) aligned with measured results.
