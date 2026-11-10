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
