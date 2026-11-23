# Rehearse a 100-agent cohort

Use `swarm-control rehearse` to compare dispatch policies, see where work queues
up and choose what to measure before a paid run. It runs locally with synthetic
durations. A project rehearsal reads the selected task definitions and frozen
Git source to check the ready frontier. Both modes keep provider calls, target
commands and budget accounting outside the rehearsal.

## What the pilot showed

The November 11, 2026 pilot provisioned 100 distinct VMs and observed command and
model activity across 100 coding threads. It returned 63 nonempty patches. All
100 original worker reports ended `needs_repair`. Local recovery and coordinator
repairs were tracked separately, including their time on the original clock.
The disjoint leaf workload also left live shared-file collisions untested. The
next constraint to measure is conversion from active coding sessions to accepted,
integrated work. [Pilot record](SCALING.md)

## Compare dispatch policies

Start with a synthetic cohort of independent tasks.

```sh
swarm-control rehearse --synthetic-tasks 100 --workers 25 50 100 --ramp-start 5
```

Use the same comparison with an explicitly selected project cohort.

```sh
swarm-control rehearse --project /path/to/target/swarm.toml \
  --manifest /path/to/target/swarm/cohort.json --workers 25 50 100 --ramp-start 5
```

The manifest contains a `taskIds` array. Each selected task must belong to the
current conservative ready frontier, even when testing a worker setting smaller
than the task count. Resolve prerequisites and review the diagnosed conflicts
before rehearsing a new cohort. A chain of 100 tasks can be analyzed with
`swarm-control analyze` and divided into successive ready cohorts.
Project rehearsal uses the profile's base ref by default. Set `--base HEAD`
to rehearse a local commit when the configured ref is unavailable.
[Dependency analysis](DEPENDENCIES.md)

The defaults compare 25, 50 and 100 slots, with five agents in the first ramp
wave. `--workers` accepts up to ten distinct limits from 2 to 100. A simulated
limit may exceed the reported project cap. Choose an initial wave smaller than
the smallest tested limit and the task count. `--preset` selects `healthy`,
`worker-fault`, `provider-constrained` or `review-constrained`. Add `--trace` to
see per-task phase times. Each pair of runs
uses the same task durations, validation outcomes and provider, review and CI
capacity assumptions. The modeled path is setup, aggregate provider service,
worker validation, review and CI. Review begins after the worker phase by
default. The `as_validated` scenario policy makes each validated candidate
available to review as soon as it completes.

| Policy | Dispatch |
| --- | --- |
| Fixed | Refill an available worker slot as each prior task finishes |
| Ramp | Complete and evaluate each bounded wave before launching another |

Run the same cohort with a constrained preset to see how extra worker slots
affect throughput and waits. `worker-fault` gives all workers a `needs_repair`
outcome. `provider-constrained` has 10 provider lanes. `review-constrained` has
four review lanes and eight CI lanes. `healthy` has 100 lanes at each stage for
synthetic cohorts. Project rehearsals use the profile's gateway concurrency as
the default provider lane count.

### Read the results

The JSON report includes planned and completed waves, attempted tasks,
validation and delivery counts, peak active workers, time to first acceptance,
and p50 and p95 waits for each queue. `finished_seconds` includes work through
review and CI. `accepted` records a simulated candidate that passed both stages.

The following results use the built-in presets, 100 synthetic tasks, five
initial ramp agents and the default review release.

| Scenario | Fixed dispatch | Ramp |
| --- | --- | --- |
| Healthy, 100 slots | 100 accepted in 310 s, peak 100 workers | 100 accepted in 830 s, peak 40 workers |
| Worker fault, 100 slots | 100 attempted, zero validated | Five attempted, then 95 held, zero validated |
| Provider constrained, 100 slots | 100 accepted in 1,120 s, model queue p95 810 s | 100 accepted in 1,370 s, model queue p95 270 s |
| Review constrained, 100 slots | 100 accepted in 1,750 s, review queue p95 1,380 s | 100 accepted in 2,270 s, review queue p95 1,380 s |

With 10 model lanes, fixed dispatch at 25, 50 and 100 slots finished in the
same 1,120 s. The corresponding model-queue p95 rose from 140 to 360 to 810 s.
This is a concrete way to spot where additional worker slots increase work in
progress without increasing delivery rate in the modeled workload.

### Model your own scenario

Pass `--scenario /path/to/scenario.json` in place of a preset. Omitted settings
use `healthy` defaults. The JSON object can be up to 256 KiB. Each lane count is
an integer from 1 to 100. Task overrides use the exact IDs selected in the
manifest or generated synthetic IDs such as `T001` and `T100`. In this scenario
review can start during the run, and the first task needs repair.

```json
{
  "provider_slots": 20,
  "review_slots": 4,
  "ci_slots": 8,
  "review_release": "as_validated",
  "defaults": {"model_seconds": 120},
  "tasks": {"T001": {"worker_outcome": "needs_repair"}}
}
```

| Supported key in `defaults` and `tasks` | Healthy default | Allowed values |
| --- | --- | --- |
| `setup_seconds` | 20 | Integer 0–86,400 |
| `model_seconds` | 90 | Integer 1–86,400 |
| `validation_seconds` | 20 | Integer 0–86,400 |
| `review_seconds` | 60 | Integer 0–86,400 |
| `ci_seconds` | 120 | Integer 0–86,400 |
| `worker_outcome` | `validated` | `validated` or `needs_repair` |
| `delivery_outcome` | `accepted` | `accepted`, `review_rejected` or `ci_failed` |

The provider phase represents aggregate service time per task. Actual workers
make intermittent requests, and the provider enforces token and request limits.
The live gateway lifetime and operational holds are evaluated by the runner.
Simulated delivery and timing describe the scenario assumptions. Actual billing,
provider quota, GitHub checks, repository environment and semantic independence
require their own verification. [Deployment preflight](DEPLOYMENT_PREFLIGHT.md)

## Use a staged launch

Set `--ramp-start N` in `plan`, in `runs plan` or `runs create` for registered
projects, or in an explicitly authorized `run`. In the browser, enter
**Initial agents for a staged run** in the **New run** form. `N` must be smaller
than both the worker limit and the selected task count. Synchronized start and
ramp are mutually exclusive. Inspect a two-slot plan that starts with one task.

```sh
swarm-control plan A1 A2 --project /path/to/target/swarm.toml --base HEAD --workers 2 --ramp-start 1
```

Each task appears in one wave. The controller checkpoints all results in the
wave before evaluating it. The next wave requires at least 80% of the just
completed wave to have a `validated` outcome and follows the existing operations
holds. When dispatch is held, the remaining tasks are recorded as
`not_dispatched` with the hold reason. Wave size doubles up to the worker cap
and the number of remaining tasks. The direct CLI uses the supplied task order.
Saved workspace runs order tasks by ID. The batch summary records validated,
held and missing checkpoints for each planned wave.
For 100 tasks and 100 available slots starting at five, the planned waves are
5, 10, 20, 40 and 25. The largest planned wave has 40 slots. Use fixed dispatch
for a cohort intended to demonstrate actual 100-agent overlap.

Ramp mode uses one gateway authorization with a finite 3,600-second lifetime.
The controller holds dispatch when the remaining window is shorter than the
worker coding timeout plus 900 seconds for its lifecycle. All tasks use the
ordinary source, issue, capacity and budget gates. Earlier allocations stay in
the ledger when a wave fails or the window runs short. A later paid attempt
needs its own authorization. [Bounded operations](OPERATIONS.md)

## November 23 local validation

This run used [controller commit `4163f86`](https://github.com/slgarza/swarm-control/commit/4163f86a253e44186dc6e1212899a71505049875),
temporary Git checkouts and Python 3.12. The public receipt records the
controller and fixture identities, measured local elapsed times, exact test
names and all 36 simulated trials. Each of the six scenario commands produced
byte-identical JSON on a second execution. [Validation receipt](evidence/scale-rehearsal-validation-2026-11-23.json)

| Part of the run | Observed result |
| --- | --- |
| Public demo | Two baseline tests passed. The two-task plan showed an independent frontier of two and waves of one task each. The demo's disabled policy blocked the smoke command before any runtime state was created. |
| Frozen 100-task fixture | The analyzer found 100 independent ready tasks. With 40 configured gateway slots, fixed preflight flagged insufficient configured capacity. Staged preflight passed local preparation checks, with waves of 5, 10, 20, 40 and 25 and a peak of 40. The fixture kept live execution disabled. |
| Real controller with test doubles | The fixed dispatcher reached 100 simultaneous local callbacks in a concurrency fixture. In the staged lifecycle fixture, three of the first five reports validated and two needed repair. The controller checkpointed 95 tasks as `not_dispatched` with `rollout_validation_hold` and retained the original fixture allocation in SQLite. |
| Local integration | The pipeline prepared 100 synthetic candidates in 100 distinct Git worktrees with eight configured lanes. Additional fixtures verified two overlapping check invocations, compatible same-file changes against the combined tree, durable conflict cleanup and enforcement of frozen acceptance checks. |

### Review queue experiment

In two matched scenarios, 100 tasks shared ten model lanes, four review lanes
and eight CI lanes. The experiment changed when review became eligible. All
cases below used fixed dispatch with 100 worker slots and ended with 100
simulated acceptances.

| Review release | First acceptance | Last acceptance | Model queue p95 | Review queue p95 |
| --- | ---: | ---: | ---: | ---: |
| After all workers finish | 1,120 s | 2,560 s | 810 s | 1,380 s |
| As each worker validates | 310 s | 1,750 s | 810 s | 600 s |

Starting review from validated candidates removed 810 simulated seconds from
the delivery clock under these assumptions. Hold the workload and provider
capacity constant when testing this review timing hypothesis in a live pilot.
The simulator uses an aggregate model phase and fixed service times. Live
provider capacity, useful agent overlap, reconciled cost and accepted target
PRs remain to be measured. [Next pilot](NEXT_PILOT.md)

Run the matched scenario commands from an installed controller. Use the four
built-in presets described above for the other comparisons in the receipt.

```sh
swarm_trial_dir=$(mktemp -d)
for review_release in after_workers as_validated; do
  printf '{"provider_slots":10,"review_slots":4,"ci_slots":8,"review_release":"%s"}\n' \
    "$review_release" > "$swarm_trial_dir/$review_release.json"
  swarm-control rehearse --synthetic-tasks 100 --workers 25 50 100 --ramp-start 5 \
    --scenario "$swarm_trial_dir/$review_release.json"
done
```

Replay the named integration and controller fixtures from this repository with
its virtual environment active. The selected tests use temporary repositories
and test doubles for external worker services.

```sh
GIT_ALLOW_PROTOCOL=file python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path

receipt = json.loads(Path("docs/evidence/scale-rehearsal-validation-2026-11-23.json").read_text())
for section in ("controller_fixture_tests", "local_git_integration_fixture_tests"):
    subprocess.run([sys.executable, "-m", "unittest", "-v", *receipt[section]["tests"]], check=True)
PY
```

## Research behind the design

| Finding | Applied to Swarm Control |
| --- | --- |
| Across 260 agent configurations, results varied substantially with task structure. Centralized verification contained error propagation more effectively than architectures without it. | Start from an explicit dependency and ownership frontier. Verify worker artifacts before treating them as input to downstream work. [Kim et al., 2026](https://arxiv.org/abs/2512.08296) |
| Concurrent coding agents benefit from shared task structure, isolated Git workspaces and executable integration checks. In ParallACT, peer messaging recovered only part of the quality lost during independent edits. | Use repository versions, artifact identifiers and test results for coordination. Route incompatible writes through reviewed integration. [Geng and Neubig, COLM 2026](https://proceedings.colm.cc/paper_files/paper/2026/hash/3b5c10bf5d70533e955cba9fba491bf4-Abstract-Conference.html), [ParallACT](https://arxiv.org/abs/2611.00314) |
| Dependency-level completion and time-to-resolution expose coordination that task completion can hide. | Preserve when each prerequisite is satisfied and its check result in a future multi-cohort delivery trace. [AsynCodeBench](https://arxiv.org/abs/2609.32662) |
| Deterministic simulation makes failures repeatable. Canary rollouts evaluate a small initial exposure before increasing it. | Rehearse the same workload under worker faults and constrained service tiers. Gate additional live waves on observed outcomes and remaining authorization time. [FoundationDB simulation](https://apple.github.io/foundationdb/testing.html), [Google SRE canarying](https://sre.google/workbook/canarying-releases/) |
| Bounded queues and deadline checks contain the impact of overload. Anthropic reports about 15 times the token use of chat in its multi-agent Research setting. | Measure provider queue time and compare delivery per reconciled dollar. Keep inference and downstream capacity separate from the worker ceiling. [Google SRE overload](https://sre.google/sre-book/addressing-cascading-failures/), [Anthropic Research](https://www.anthropic.com/research/multi-agent-research-system) |

## Prove delivery and recover safely

1. **Measure useful overlap and yield on a real workload.** After a small
   repeated trial, use separately authorized 25- then 50-worker cohorts to
   observe per-stage queue age, session overlap supported by command and model
   observations, acceptance rate and review/CI throughput. Exercise shared-file
   collisions, semantic conflicts and reverts before attempting 100.
   [Next pilot](NEXT_PILOT.md)
2. **Follow the work through merge.** Verify combined changes against the actual
   integration base. Record repair ownership, CI queue time, merge and reverts.
   Compare merged green PRs per hour and per reconciled dollar against a strong
   AFK baseline using identical initial source, tasks and acceptance criteria.
   GitHub merge queues illustrate validation of the latest base together with
   already queued changes. [Benchmark protocol](contest/BENCHMARK.md),
   [GitHub merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
3. **Make recovery durable before adding controller hosts.** The current locks
   and budget ledger are shared by worktrees on one host. A multi-host design
   needs a shared transactional store, expiring leases with a monotonic generation
   checked on state writes, and progress heartbeats. Persist launch identity and
   provider receipts so a successor can reconcile an ambiguous launch and reject
   stale completion. A lease expiry changes control ownership. Any replacement
   worker still needs its own authorized attempt. [Temporal event history](https://docs.temporal.io/workflow-execution/event),
   [Temporal heartbeats](https://docs.temporal.io/encyclopedia/detecting-activity-failures),
   [AWS idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/)
