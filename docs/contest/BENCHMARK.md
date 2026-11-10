# Comparing the swarm with an AFK pipeline

This is a proposed evaluation protocol. No completed comparison is included in
this repository. The controller supports explicitly configured cohorts up to 100,
but offline load tests do not establish a qualifying live 100-agent system.

## 1. Define the experiment before running it

Freeze a target repository commit, a useful backlog, acceptance criteria, allowed
tools, required checks and a quality observation window. Give each backlog item
a stable identity and a reviewed size/scope. Agree on when a change counts as an
accepted delivery before seeing either system's output.

Use independent target copies/branches starting at the same commit. Keep the
language/toolchain, test fixtures, dependency locks, API access, CI capacity and
network conditions comparable. Record any differences. Neither pipeline gets
solutions, repaired patches or changed acceptance criteria learned from the
other's run. Use authorized data and repositories.

Keep the backlog dependency graph identical. Include realistic shared-file work
and dependent changes rather than making every task artificially independent.
Review how each system handles those dependencies and conflicts. Use the explicit
`--allow-overlap` mode for reviewed shared-file work; per-worker path restrictions
still apply, and integration must check the combined result. Do not manufacture
small tasks merely to fill a 100-agent launch.

## 2. Choose a strong baseline

The baseline is the best practical AFK workflow available at evaluation time,
selected or accepted by the organizers. It may itself use multiple workers,
queues, retries and automated review. A deliberately weak single-agent loop does
not establish superiority over the announcement's baseline.

Record baseline software/model versions, configuration, resource limits, prompts
and tuning effort. Give both systems comparable preparation and permitted
information. Separate engineering/setup effort from execution, but report it.
An organizer-controlled final baseline supersedes preliminary local comparisons.

Run at least three paired repetitions when budget and workload permit. Alternate
order to reduce timing effects. Report each run, failures and spread; do not
publish only the best swarm run and worst baseline run. A single run can be a
pilot observation, but is weak evidence of a repeatable advantage.

## 3. Use an explicit delivery clock

For the primary end-to-end measure, start when the prepared workload is released
to a pipeline. Stop at the predeclared deadline or after the final accepted merge
and required checks complete. Include dispatch, environment startup, model calls,
integration, review, queueing, CI, failures and retries during that interval.

Record setup and compatibility checks outside that clock separately, including
their cost. For repeated operation, state whether those costs are charged in full
or amortized and use the same rule for both systems. Also publish a full
setup-inclusive view. Never silently switch the start time to the first successful
worker or exclude a slow integration tail.

The original pilot's 30-minute-37-second interval starts at first worker creation,
not workload release. Its 19.596 PRs/hour is a historical batch observation, not
a result produced under this proposed full benchmark protocol.

## 4. Count accepted deliveries consistently

A qualifying delivery must implement a frozen backlog item, pass independent
acceptance review, merge into the designated branch, and pass the agreed checks
on its exact reviewed head and combined result. Define treatment of subsequent
reverts or regressions using the same observation window for both systems.

Report both accepted backlog items and merged green PRs. Fix the intended PR
granularity before dispatch. Splitting one change into many cosmetic PRs, opening
duplicates, or counting documentation/bookkeeping as feature work must not inflate
the delivery count. Report partial work, unmerged patches, failed tasks, repaired
deliveries and reverts separately.

Use independent acceptance tests or review beyond tests written by the same
worker. A passing test suite can miss a wrong implementation, as the pilot's
accessibility repair demonstrated.

## 5. Measure time and cost

Let `D` be accepted merged green deliveries, `T` the measured duration in seconds,
and `C` the reconciled eligible cost in USD:

```text
merged_green_prs_per_hour = D * 3600 / T
merged_green_prs_per_dollar = D / C
cost_per_delivery_usd = C / D
time_rate_ratio = swarm_prs_per_hour / afk_prs_per_hour
cost_rate_ratio = swarm_prs_per_dollar / afk_prs_per_dollar
```

These ratios require valid denominators and comparable runs. Report a missing or
zero denominator as undefined with an explanation, not infinite efficiency. If
`D = 0`, cost per delivery is undefined. If cost has not been reconciled, publish
it as unknown and leave the dollar-rate fields null.

Collect model usage/billing, Modal worker and gateway compute, image builds,
storage, network, GitHub CI and any other paid services attributable to the run.
Include failed attempts and repair work. Record credits, shared resources and
amortization explicitly; show gross usage cost as well as the amount actually
billed where they differ. Budget reservations and gateway ceilings remain separate
fields and must not be substituted for invoices.

The announcement's internal-token exemption belongs in a separate cost treatment:
record which charges qualify, who confirmed that classification and the excluded
amount. Report both total economic cost and contest-eligible cost. Do not classify
all OpenAI usage as exempt simply because the pipeline calls OpenAI.

## 6. Establish genuine concurrency

For every worker retain a unique session/sandbox ID, task ID, common base SHA,
start/end timestamps, command events, model activity, final diff, checks and
integration outcome. Record timestamp source and precision. Monitor queued,
preparing, working, waiting for model/tool results, checking and completed states
separately where instrumentation permits.

Compute interval overlap from evidence, not requested worker count. Report peak
and sustained session counts, duration at or above 100, and the distribution of
useful contributions. A worker can legitimately wait on a model or tool during
active work; an idle container with no task activity does not prove a coding
agent. Report sampled useful activity separately from process lifetime overlap.

At least 100 agents must perform meaningful work during an overlapping interval
on the same repository. Hundreds of sequential tasks, idle VMs or three real
agents surrounded by placeholders do not meet that requirement. Preserve missing
events as evidence gaps and use conservative bounds rather than inventing times.

## 7. Record interventions and failure behavior

Log each human/coordinator intervention with timestamp, reason, duration and
affected tasks. Include task rewrites after dispatch, code repairs, manual conflict
resolution, CI fixes and merge decisions. Declare which actions were preconfigured
automation and which needed runtime judgment.

The current workflow requires explicit review and integration commands. Report it
as assisted while those steps remain. Do not call an assisted run AFK, and do not
hide its intervention time while comparing it to an unattended baseline. Agree
with the organizers how required human approval is treated for both systems.

Exercise failed tests, overlapping edits, API throttling, lost workers, duplicate
claims, changed bases and cleanup. Measure recovery, correctness and cost. A
failure that safely stops a cohort is different from successful autonomous repair.

## 8. Report the result

Publish a paired table with deliveries, full duration, eligible and gross cost,
both rates, interventions, regressions and observed concurrency. Include every
attempt and the exact versions used. Report uncertainty and exclusions alongside
the numbers, with links to the underlying sanitized evidence.

An efficiency win requires improvement on **both** announcement metrics while
meeting scale and quality requirements. If either metric is unknown or the
baseline is not comparable, the conclusion remains unproven. Use the
[evidence template](EVIDENCE.md) and keep projections separate from measurements.

## Offline measurement commands

`swarm-control evidence collect` exports allowlisted aggregates and original
artifact hashes. It requires explicit delivery-clock timestamps for hourly rates,
reconciled cost receipts for dollar rates, and independent acceptance receipts
before counting accepted merged green deliveries. Missing measurements stay null.
`swarm-control evidence compare` recomputes rates and checks matching repository,
base, frozen task manifest and comparison protocol. It checks clock and delivery
totals against the report's timestamps and receipts; it cannot authenticate local
receipts or independently approve a baseline. See [the input formats](EVIDENCE.md).

The exporter separates process overlap from sampled command/model spans attached
to validated contributions. Neither is continuous activity instrumentation. It
always leaves the live scale requirement unverified and the comparison never
certifies a contest win. Review original evidence and the organizers' baseline.
