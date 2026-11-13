# Next pilot: prove completion before repeating scale

This is a preparation protocol, not a record of another live experiment.
No paid run, new allowance, AFK win or improved completion rate is implied.
The original hundred-worker outcomes remain immutable.

Protocol reviewed: November 13, 2026, against the current dashboard and CLI.
No new live result is reported here. Organizer deadlines, eligibility and any
cost exemption must be confirmed independently before a submission.

The scalability target remains at least **100 useful concurrent workers**.
Read-only dependency analysis validates a conservative independent frontier for
the selected cohort; total task count alone is insufficient. For a reviewed
100-task selection, use its actual manifest:

```sh
swarm-control analyze --project /path/to/target/swarm.toml --manifest /path/to/target/swarm/cohort.json --workers 100 --require-parallel 100 --base HEAD
```

This command makes no provider calls and starts no workers. The manifest needs a
`taskIds` array of real tasks; do not run completed historical tasks again.
Small representative canaries are preparation experiments, not a reduction of
the 100-worker target or proof of its runtime capacity. Any new live experiment
still requires its own authorized remaining allowance and frozen source.

## Freeze a representative small workload

In **Projects**, select a local repository. If starting from a goal, **Plan work**
can propose up to 100 tasks through one explicitly authorized model call and
source upload. Review commands, acceptance, ownership and dependencies, then
separately authorize actual issue/profile/task creation. Planning has its own
retained reservation; applying a plan grants no worker allowance. Missing or
uncertain outcomes must be inspected without automatic regeneration or repeated
issue creation. Existing reviewed task files can skip model planning.

Select ten new, independently reviewable tasks from a clean, pushed target base.
Include UI and domain work, one complete browser interaction and a controlled
shared-file scenario. Prepare semantic acceptance and browser checks before
inspecting agent output. Record dependencies, ownership, exact issue/task IDs,
required checks and each task's contract excerpts. Do not reuse completed work or
make the baseline consume solutions from the swarm.

Review and run the prepared project's checks, publish the exact reviewed files,
then freeze the resulting clean pushed source base. Use **Project settings** for
profile/tasks/checks/budget preparation and **Settings** for personal accounts.
The planner's structural analysis and written acceptance are not independent
semantic approval or evidence that its proposed commands pass.

Use the [run workflow](RUNS.md) to save the task selection, parallel-agent count,
source hashes, timeout, context mode and total proposed model reservation. Keep
target configuration outside the controller. All local accounting remains
cumulative; a new experiment needs its own authorized remaining allowance.

**New run** checks and saves a draft; **Run agents** separately authorizes actual
execution after provider, deployment, source and budget gates pass. Use **Runs**
to select the recorded attempt, then its Workers, Changes, Costs and Evidence
tabs. Navigation, a saved draft and a displayed 100-worker selection are not
evidence that any worker started.

## Test one change at a time

1. Establish a small completion baseline with the new phase/termination receipts.
   Distinguish unsuccessful execution, empty output, valid recovered code and
   accepted delivery. Record failed attempts and repairs instead of relabeling
   failed workers.
2. Compare `full` and `task` context on equivalent, separately initialized cases.
   Keep global rules, functional contracts, model policy, acceptance, deadline
   and resource treatment fixed. Measure reading/repeated output, time to first
   change, completion, semantic quality and the complete integration tail.
3. Exercise independent integration candidates while one PR waits for CI. The
   offline `ops queue-plan queue.json --project /path/to/target/swarm.toml`
   classifies bounded waits and selects independent preparation lanes. The explicit
   local [integration pipeline](INTEGRATION_PIPELINE.md) can execute bounded
   preparation lanes from reviewed artifacts. Neither command merges, reruns CI
   or launches paid repairs. An integration operator must still
   recheck review, base, tested tree and exact head before every write.
4. Run the strongest agreed AFK configuration against the same frozen workload,
   without sharing generated patches. Define its adapter, versions and policy
   before execution; these remain unselected until recorded. Alternate order and
   retain repetitions and variance. Apply the same review/intervention rules.

Queue input is explicit local metadata, for example:

```json
{
  "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "lanes": 2,
  "ci_wait_seconds": 600,
  "items": [
    {"task_id": "T01", "stage": "waiting_ci", "ownership": ["src/one"],
     "waiting_since_utc": "2026-01-01T12:00:00+00:00"},
    {"task_id": "T02", "stage": "ready", "ownership": ["src/two"], "depends_on": []}
  ]
}
```

The SHA and time above are synthetic placeholders. Replace them with actual
observations. Quarantine is a scheduling recommendation; a missing/pending check
never becomes green. Dependencies and overlapping in-flight ownership remain
held. A changed base requires revalidation. Source claims are not authenticated
by this offline planner, and its output grants no execution authority.

## Decide using delivery evidence

A proposed initial gate is at least eight of ten independently accepted items
without coordinator code/test repair. Observe the frozen quality window as well
as exact-head and combined CI. This threshold is a proposal for the next
experiment, not a result, contest rule or automatic launch permission.

Record complete T0-to-delivery duration, original autonomous versus assisted
outcomes, preparation, review, CI, failed attempts, repairs and cleanup. Keep
provisional deliveries separate until quality observation is complete. Calculate
dollar efficiency only from reconciled attributable costs under the agreed
cost policy, applying only independently confirmed exemptions; retained
reservations are not invoices. Include AI planning/preparation calls under the
same declared setup-cost treatment for both pipelines.

Escalate through 25, 50 and 100 only after repeatable completion, useful measured
activity and comparative delivery justify it. The existing failure brake acts
on completed checkpoints and cannot prevent an already admitted synchronized
cohort from failing together. Phase logging and context packets improve diagnosis;
they do not themselves prove successful autonomous operation or useful 100-way
concurrency. The first live validation of these changes remains outstanding.
