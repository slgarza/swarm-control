# Operational feedback and recovery

The controller records worker outcomes, holds new admission after repeated
failures, classifies CI observations and reserves integration actions durably.
These controls do not launch replacement workers or authorize additional spending.
Worker execution remains separate from credentialed GitHub integration.

## Failure brakes

After each completed coding worker, the dispatcher stores a bounded category in
the target checkout's private `.swarm/operations.sqlite3`. It keeps task/batch IDs,
the controller's observation time and a fixed outcome category, without copying
worker output, exception messages or customer code. Replaying a checkpoint is
idempotent; changing an already recorded outcome is rejected.

The default brake needs three failures sharing a cause across three distinct
tasks within five minutes, representing at least half the observed tasks.
Unknown or inconsistent observations hold admission conservatively. An exit of
`-1` is classified as incomplete, without inventing a timeout cause.

Optional settings live in the target's `swarm.toml`:

```toml
[operations]
window_seconds = 300
minimum_failures = 3
minimum_tasks = 3
minimum_share = 0.5
```

A hold is checked before provider lookup or budget initialization and before
each new dispatch. Already admitted workers finish through their existing
recovery paths. Tasks that were not dispatched are recorded as `not_dispatched`,
not failed worker attempts. A synchronized cohort's barrier is aborted if
admission closes before every worker can enter.

The hold persists across restarts and beyond the detection window. Inspect and,
after addressing the cause, explicitly clear it:

```sh
swarm-control ops status --project /path/to/target/swarm.toml
swarm-control ops clear-hold --project /path/to/target/swarm.toml \
  --reason "Reviewed and fixed the worker contract"
```

Status is read-only and creates no state. Clearing records a control event and
starts a new observation boundary; original observations remain stored. The
operations database is separate from `control.sqlite3`. No budget reservation
is refunded, reset or increased. A subsequent run still needs every ordinary
live-execution, budget, source, issue and capacity gate.

## CI classification

```sh
swarm-control ops inspect-ci observation.json --project /path/to/target/swarm.toml
```

This command reads local metadata only. The input identifies `repository`,
`head_sha`, a complete `check_payload` with `total_count`/`check_runs`, and
`workflow_runs`, optionally including complete job inventories. Source timestamps
must be aware and valid; incomplete, duplicate, mismatched, stale or future
metadata is not accepted as green. No source object or raw log is printed.
The input must include `fetched_at_utc`, binding when its API metadata was read.
This is separate from each event's creation/update/completion time: an older
green check freshly observed on the same exact head is not stale merely because
the check completed earlier. Missing, future or stale fetch observations remain
unverified. The local classifier does not authenticate a claimed fetch timestamp.

| Observation | Recommendation |
| --- | --- |
| Existing queued workflow, no checks yet | Wait; do not create another run |
| No workflow event found | Inspect metadata |
| Completed failed workflow with explicitly zero jobs | Propose a manual rerun |
| Jobs actually ran and failed | Review the failure and repair if appropriate |
| Successful steps with a pending aggregate | Inspect unresolved metadata; no green claim |
| Every required trusted check genuinely succeeded on the exact head | CI evidence is green; normal review/base/tree gates still apply |

Recommendations perform no GitHub action and grant no mutation or spending
authority. The existing integrator remains responsible for source/review,
ownership, tested-tree, current-base and exact-head checks.

## Action reservations

Immediately before a merge PUT, integration atomically reserves one attempt for
that PR and head in the operations database. Concurrent callers and restarts
cannot reserve a second attempt for the same subject. Missing or ambiguous
responses remain unresolved; elapsed cooldown never turns uncertainty into
permission to retry. If a later GET confirms the actual merge, the existing
merge command can reconcile it without another PUT.

The journal also supports bounded, cooldown-controlled action reservations for
future recovery adapters. There is no automatic rerun/rebase/repair adapter in
this release. Clearing an admission hold does not clear action history.

## Scaling advice

```sh
swarm-control ops advise-scale measured-window.json --project /path/to/target/swarm.toml
```

Inputs are explicit `current_workers`, `merged_prs`, `agent_hours`,
`previous_merges_per_agent_hour`, `previous_rate_measured`, `comparison_matched`,
`queue_pressure` and `failure_share`. Missing, nonfinite, unmatched or zero-hour
data holds growth. A healthy, comparable window can advise at most one additional
worker within the configured capacity. A VM/heartbeat count does not establish
useful concurrent work. Advice never changes fleet size, allowances or provider
resources, and is not an AFK efficiency result.

## Integration scheduling

`swarm-control ops queue-plan queue.json --project /path/to/target/swarm.toml`
plans independent preparation lanes while other items wait for CI. It holds
unqualified dependencies and overlapping in-flight ownership, and recommends
quarantine after an explicitly observed wait limit. It does not rerun workflows,
repair code or mutate GitHub. Every changed base still needs validation before
integration. See [the next-pilot protocol](NEXT_PILOT.md) for the input schema.

## Offline verification

Synthetic tests cover queued and contradictory CI, missing usage-like metadata,
action uncertainty, concurrent reservations, process restart, deduplication,
brakes during dispatch and preservation of the separate financial ledger:

```sh
python -m unittest tests.test_health tests.test_ci_health tests.test_operations -v
```

These tests make no paid model, cloud or GitHub calls.
The [November 11 validation receipt](evidence/controller-operations-2026-11-11.json)
records the full 332-test controller run, Ruff, Python compilation, JavaScript
syntax validation and source hashes. Local results do not replace GitHub checks
on the published commit.
