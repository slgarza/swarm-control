# Bounded integration preparation lanes

The coding dispatcher can run 100 workers when its required independent ready
frontier, capacity, source, provider and budget gates pass. Integration preparation
is a separate local resource. This pipeline accepts up to 1,000 candidate
artifacts and runs one to eight local preparation lanes; its lane count does not reduce worker capacity.

Use it to keep independent work moving while another candidate waits for review
or CI. It prepares local worktrees and checks. Publishing, CI retries, review
approval and merge remain explicit controller operations.

## Select actual reviewed artifacts

Save a private manifest outside tracked source:

```json
{
  "schema_version": 1,
  "items": [
    {
      "artifacts": "/path/to/target/.swarm/modal-runs/batch-ID/T01",
      "reviewed_patch_sha": "REPLACE_WITH_THE_64_CHARACTER_SHA256"
    },
    {
      "artifacts": "/path/to/target/.swarm/modal-runs/batch-ID/T02",
      "reviewed_patch_sha": "REPLACE_WITH_THE_64_CHARACTER_SHA256"
    }
  ]
}
```

Replace each placeholder with the verified original patch digest reviewed for
that task. Relative artifact paths resolve against the manifest's directory.
The pipeline derives IDs, ownership and `dependsOn` from the artifact's task;
free-form queue fields such as `combined_ci_green` are rejected. The shared
dependency analyzer also derives hard producer/consumer artifact prerequisites;
cycles and invalid dependency metadata hold affected candidates. Missing source
or ambiguous relationships are not treated as proof of independence. Repository,
worker base, source/download hashes, scope and worker validation are checked
before preparation. The original source reports and patches remain unchanged.

Each preparation records hashes of the exact manifest, report, original patch,
frozen task, selected project profile and validation commands, required CI checks,
and instruction files at the tested base. Review, publish, merge, and dependency
qualification recheck this binding and the accepted local check receipts. Changing
acceptance commands, dependencies, source reports or the selected profile holds
that candidate; unrelated candidates can still prepare. Existing receipts without
these bindings fail closed. Preserve them and explicitly prepare a new artifact
copy; a queue restart never upgrades an old receipt or reruns an uncertain attempt.

Preview using the currently fetched `origin/main`:

```sh
swarm-control integrate-queue /private/preparation.json --project /path/to/target/swarm.toml --lanes 2
```

The default preview reads local files and Git metadata without creating runtime
state, fetching source, opening a ledger or running project commands. Fetch the
target's base branch yourself first if its local tracking ref is absent or old.

Explicit local execution:

```sh
swarm-control integrate-queue /private/preparation.json --project /path/to/target/swarm.toml --lanes 2 --max-pending 4 --execute
```

Execution fetches the base, preserves a private attempt receipt, and runs the
profile's trusted install/check argument lists in dedicated Git worktrees. Each
command defaults to a 1,200-second limit; `--check-timeout` accepts 1–3,600 seconds.
On expiry the controller stops that command's process group and preserves the
failed attempt. Task-configured acceptance commands also run separately, and
protected acceptance files must match their original frozen source before and
after checks. Passing these commands is not independent product acceptance.

## Bound work and preserve uncertain outcomes

`--max-pending` defaults to twice the lane count and accepts the lane count
through 1,000. It bounds pending task-and-patch candidates across pipeline
invocations on this checkout, including older prepared candidates and interrupted
attempts whose outcome is unknown. Explicit revalidation of an already prepared
task/patch on a new base reuses its logical pending slot while preserving both
attempts. An uncertain older attempt still holds that source candidate. One
pipeline scheduler owns a checkout at a time; its independent
checks run concurrently. A failed check does not prevent another admitted lane
from finishing. The current invocation also bounds total admissions by this
limit, including failed attempts.

The private `.swarm/integration-pipeline.sqlite3` journal reserves one attempt per
repository/task/original patch/worker base/tested base. A repeated manifest or
restarted process cannot silently repeat an uncertain attempt. Copying the same
candidate to another directory does not create another reservation. Preserve
failed and interrupted records; investigate before preparing a separately
reviewed new candidate or new base.

Each invocation retains `.swarm/integration-pipelines/prepare-ID/receipt.json`.
Original exception traces and validation output stay private. These files and
the journal use private permissions and are separate from the durable money
ledger. The pipeline never replenishes, refunds or changes any budget.

Candidate artifact directories retain `integration.json`, the applied patch and logs. A
candidate-level lock covers its preparation; the shared integration lock covers
Git setup and credentialed publish/merge operations. Long install/check commands
run outside that shared lock. A different candidate can therefore reach checks
while another is still checking.

## Dependency and CI holds

Unmerged or unverified dependencies remain held. Include the reviewed artifact
of a merged dependency in the manifest to permit its verification. The default
offline preview deliberately cannot certify a merge from a JSON status alone.
Add `--online` to permit bounded read-only GitHub requests:

```sh
swarm-control integrate-queue /private/preparation.json --project /path/to/target/swarm.toml --lanes 2 --online --execute
```

A dependency must have matching reviewed source/artifact bindings, local head
and merged trees, its recorded applied-diff hash, an ancestor merge commit in
the fetched base, and current successful required GitHub Actions checks on both
the PR head and merged commit. Three-way dependencies additionally need
`reviewed_applied_patch_sha` in their manifest item. Missing artifacts or evidence
hold dependents; unrelated candidates can still prepare.

Read-only verification shares a 200-GET budget per invocation, including failed
requests, with bounded pagination and per-run caching. Change that bound
explicitly with `--github-get-limit` (1–10,000). Exhausting it leaves unresolved
dependencies held; elapsed time and missing checks never turn into green CI.

Waiting-CI and review candidates also hold overlapping ownership. A wait can
be classified as quarantined by the queue planner while independent preparation
continues; quarantine never retries CI, cancels a run, approves a review or
permits a merge. The pipeline performs zero GitHub mutations.

When `main` advances, already prepared candidates keep their original tested
base and tree. Existing publish/merge guards reject stale bases. Preserve that
attempt and explicitly prepare a new artifact copy against the new base; do not
overwrite its `integration.json`. Increasing preparation WIP can create more
revalidation work, so start with a small bound even with 100 coding workers.

## Explicitly reviewed recovery

A failed or interrupted worker may retain useful, clean patch bytes. Such a
worker remains failed. The supervisor can attach a digest-bound
`recovered_candidate` record requiring independent validation. To explicitly
review that candidate for local preparation, add `"recovery_reviewed": true`
alongside its actual `reviewed_patch_sha` in the private manifest.

The pipeline checks the original status/exit/base/digest binding and clean
head/scope/prohibited-file observations, then independently runs current-base
validation. Its new integration record is attributed as
`reviewed_worker_recovery`. Neither the worker outcome nor its original logs and
patch are rewritten, and no replacement VM or model request is created.

Subsequent explicit integration commands use the same review flag:

```sh
swarm-control integrate publish /path/to/recovered/artifacts --project /path/to/target/swarm.toml --recovery-reviewed --reviewed-patch-sha ACTUAL_SHA256
```

Three-way application still requires the separately reviewed applied-diff hash,
and merge still requires current-base and exact-head CI gates. A prepared or
recovered candidate is not an accepted delivery. Historical pilot reports that
lack the new recovery binding need their own separately reviewed recovery
adapter; this command does not manufacture that missing evidence.

## Validation

```sh
python -m unittest tests.test_integration_pipeline tests.test_pilot_integrate tests.test_integration_queue -v
ruff check --isolated swarm_control/pilot_integrate.py swarm_control/integration_pipeline.py tests/test_integration_pipeline.py
python -m compileall -q swarm_control
```

Tests use temporary local Git remotes and real local check processes. They cover
two concurrent lanes, blocked CI, 100 actual local candidate preparations, durable duplicate and
interruption holds, new-base publication rejection, reviewed recovery, protected
acceptance inputs and descendant-process deadline cleanup. GitHub observations
are synthetic; these checks launch no workers, publish no PRs and establish no
live throughput result.
