# Repositories, run configuration and history

A repository is a registered local checkout with its own `swarm.toml`. A run is
one saved selection of tasks, parallel agents, source commit and limits. Its
history remains separate from every other run, including failed attempts.
The interface and CLI use English.

## Use the local interface

```sh
swarm-control workspace init --label "My projects"
swarm-control workspace add /path/to/target/swarm.toml --id my-project --label "My project"
swarm-control dashboard --workspace --port 8787
```

If the workspace already exists, skip `init`. Open
[http://localhost:8787](http://localhost:8787). Choose a repository, click
**New run**, select its actual tasks and choose **Concurrent agents**. Click
**Check plan**, review its preparation and admission observations, then **Save
draft**. Open the saved entry in **Run history** to inspect its settings later.

Concurrent agents limits how many task workers can run at once. For example,
six tasks with two concurrent agents are queued through two execution slots;
they do not mean six simultaneous workers. Each task gets its own temporary VM.
The current runner bounds a run to 100 tasks and 100 concurrent workers, and
the target profile may impose a smaller cap on both. A synchronized run requires
exactly one task per concurrent agent. Increasing a setting does not increase
provider quota, deployed gateway capacity or money authorization.

The main workflow creates local plans and drafts. **The GUI does not launch
workers.** It has no deployment endpoint, budget toggle, credential entry or
merge control. The CLI inspection command shown in a draft does not execute it.
An intentional CLI `--execute` operation remains the launch boundary.

Only registered repositories are selectable. The server does not scan your disk,
clone arbitrary URLs or accept target configuration paths from the browser.
Target settings remain in each target checkout. The default private registry is
`~/.config/swarm-control/workspace.json`; use `--workspace PATH` consistently to
select a different registry. Public HTTP responses omit its filesystem paths,
raw reports, worker commands, customer source and credentials.

## Configure and inspect from the CLI

Replace the task IDs with existing IDs from the selected repository:

```sh
swarm-control runs plan my-project T01 T02 --agents 2 --model-budget 1.00
swarm-control runs create my-project T01 T02 --agents 2 --model-budget 1.00 --label "Two-task trial"
swarm-control runs list my-project
swarm-control runs show my-project RUN_ID
swarm-control runs launch my-project RUN_ID
```

`plan` reads source and existing accounting without creating a run or financial
ledger. `create` saves a separate immutable draft. `show` presents that saved
record, rather than silently recomputing its historical readiness. `launch`
without `--execute` only inspects the selected draft. The ordinary runner also
accepts `--agents` as an alias for `--workers`.

Run options include `--agents`, `--timeout` (30–1800 seconds), `--base`,
`--model-budget`, `--context-mode full|task`, `--synchronized` and
`--allow-overlap`. The model reservation is a **total run limit in USD**, not a
per-agent amount, bill or account balance. Infrastructure admission is reported
separately. Unknown remaining allowance and unknown actual billing stay unknown.

After source review, actual provider/gateway verification and authorization of
the remaining allowance, explicit execution uses:

```sh
swarm-control runs launch my-project RUN_ID --gateway-url GATEWAY_ORIGIN --execute
```

Replace `GATEWAY_ORIGIN` with the separately verified HTTPS gateway origin.
This command can create paid resources. Every existing live-policy, task claim,
issue/dependency, budget, operations-hold and deployed-capacity gate still
applies. Source, task, profile, instruction and controller hashes must match the
saved draft. Changed inputs need a new reviewed draft; they do not silently
change an old run. A durable reservation permits only one launch attempt per
saved run, including across processes and restarts. An uncertain or interrupted
attempt is retained for investigation, not automatically retried.

Launch time records the request to start the runner. Actual coding start remains
unknown unless a matching batch supplies an aware agent-start observation.
**Runner finished** describes a subprocess result, not merged or accepted work.
Worker outcome counts remain separate from review, CI, integration and invoices.
Worker processes still receive no credentialed GitHub integration access.

## Preserve past evidence

Import an explicit curated snapshot into a registered project:

```sh
swarm-control workspace import-snapshot my-project /path/to/curated.json --run-id previous-trial --label "Previous trial"
```

The workspace copies the original bytes into private history and binds their
hash, repository and original run ID. It rejects changed copies or cross-project
sources. Updating the source file later does not rewrite this past observation.
The saved timestamp is the import time; source/run times remain distinct.
Imported evidence does not invent missing configuration, cost, quality or T1.

Existing strict dashboard catalogs can be copied with
`workspace import-catalog /path/to/catalog.json`. Projects without a registered
target config remain available for history viewing. Register their config using
`workspace add` before preparing new runs. Duplicate imports are rejected rather
than overwriting old history.

Legacy `dashboard --snapshot PATH` and `dashboard --catalog PATH` remain evidence
viewers with no write methods. The new workspace mode adds only bounded local
preview/draft creation, protected by exact Origin/Host checks and a per-server
CSRF token. It does not add remote execution to HTTP.

## Worker context and measurements

`full` preserves the original instruction workflow. `task` additionally delivers
explicit contract excerpts from the selected Git base. Add selectors to a new,
reviewed task definition before selecting that mode:

```json
{
  "context_files": [
    {"path": "docs/FEATURE_CONTRACT.md", "start_line": 10, "end_line": 35}
  ]
}
```

Required global instructions remain mandatory in both modes. Full source remains
available on demand. Selectors are bounded, regular documents from the exact Git
commit; missing selectors, symlinks or invalid ranges fail before worker
admission. Plans retain prompt/context hashes and sizes, without publishing
contract text. Context mode is an experiment setting, not a demonstrated speedup.

Worker reports now record controller-observed phase timings, bounded event
counters, receipt times and explicit process outcomes. A `-1` or `137` exit alone
never establishes a timeout or OOM cause. Coding runs with empty patches skip the
full helper benchmark after scope/head inspection and remain unvalidated; smoke
checks are unchanged. Recovery and termination still run after failures. A
progress-receipt failure cannot turn partial work into a validated delivery.

Future deployments of the trusted gateway also emit bounded private request
timing receipts for authentication, capacity, reservation, token counting,
pacing, HTTP send, first response byte, streaming and cleanup/settlement.
They contain fixed categories, generated observation IDs and authenticated
run/task identities, without request bodies, headers, tokens or credential
hashes. This adds observations to existing calls and never adds provider calls,
retries or monetary changes. Observer/sink failures remain separate from request
outcomes. The in-process window is bounded and resets on restart; its aggregates
are not a complete run, invoices, inference CPU time or useful-agent overlap.
Durations can overlap and are not additive. These are new instruments for future
runs; original unknown timings remain unknown. Raw timing logs remain private.

See [the next-pilot protocol](NEXT_PILOT.md), [operational feedback](OPERATIONS_SAFETY.md)
and [the dashboard gallery](DASHBOARD.md#screenshots).
The [validation receipt](evidence/run-workspace-validation-v2-2026-11-11.json)
records full offline checks, source hashes, measured durations and their limits.
