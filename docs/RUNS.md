# Repositories, run configuration and history

A repository is a registered local checkout with its own `swarm.toml`. A run is
one saved selection of tasks, parallel agents, source commit and limits. Its
history remains separate from every other run, including failed attempts.
The interface and CLI use English.

## Use the local interface

From the controller checkout:

```sh
./easy-run
```

The launcher installs the local environment on first use, initializes a private
workspace if needed and opens [http://127.0.0.1:8787](http://127.0.0.1:8787).
Keep the terminal open. Press Ctrl+C there to stop the server when no run is in
progress. An existing installation can use `swarm-control start` instead.

1. In **Projects**, use **Add repository** to clone/fork a GitHub URL or inspect
   a local Git folder. Connect its existing profile, or use **Plan work** to
   generate and approve reviewed tasks from your goal. Reconnecting the same
   checkout preserves its run history.
2. Choose that repository in **Project**, then click **New run**. Select actual
   tasks listed by its configuration. Start with one or two independent tasks.
3. Choose **Concurrent agents**. One agent per selected task is a clear first
   trial. Enter the proposed total model reservation if preparing real execution.
4. Click **Check plan** and read any blockers. Click **Save and review run** to preserve
   this exact selection and source. Saving starts no agents. Open the saved entry
   in **Runs** to inspect it later. Evidence, costs and workers belong to that
   run's detail tabs.
5. For real execution, follow the setup described below, then open the draft's
   launch controls, enter your verified HTTPS gateway origin, review the limits,
   explicitly confirm the authorization and click **Run agents**.

To connect a prepared target at startup, use
`./easy-run --project /absolute/path/to/target`. The target and controller remain
separate repositories. A repo with no profile or task files first needs
[AI planning and review](AI_PLANNING.md), or manual [project configuration](PROJECTS.md).
New profiles start with a disabled execution policy and zero allowance.

Concurrent agents limits how many task workers can run at once. For example,
six tasks with two concurrent agents are queued through two execution slots;
they do not mean six simultaneous workers. Each task gets its own temporary VM.
The current runner bounds a run to 100 tasks and 100 concurrent workers, and
the target profile may impose a smaller cap on both. A synchronized run requires
exactly one task per concurrent agent. Increasing a setting does not increase
provider quota, deployed gateway capacity or money authorization.

The explicit **Run agents** action can start paid workers. It requires a confirmed
saved draft, a gateway origin and the same source, issue, task-claim, budget,
operations-hold and capacity gates as the CLI. A plan passing does not satisfy
provider setup or authorize spending. **Settings** provides account and personal
`.env` configuration. **Project settings** provides reviewed cumulative-budget changes, gateway
deployment and a compatibility probe. Advanced reviewed CLI controls expose
integration operations; semantic review remains an operator responsibility.
Follow [browser setup](SETUP.md) or the [two-agent walkthrough](TWO_AGENT_WALKTHROUGH.md).

Only registered repositories can execute runs. Projects can clone or fork an explicitly
chosen GitHub repository into an unused local folder, or inspect one explicit local path; target
settings remain in each target checkout. The default private registry is
`~/.config/swarm-control/workspace.json`; use `--workspace PATH` consistently to
select a different registry. Evidence/run responses omit its filesystem paths,
raw reports, worker commands, customer source and credentials. Setup separately
shows the paths and task drafts the operator explicitly selects; credential values
are never returned. Setup operation results remain in private local history.
Keep the registry and its companion state folders outside Git. The default
location does this; a custom workspace path is not an application source file.

AI planning and existing-task run planning are separate: **Plan work** can send
bounded source context to OpenAI under its own confirmed allowance, then create
issues/tasks after approval. **New run → Check plan**, `plan` and `runs plan`
inspect existing task definitions without a model call.

## Configure and inspect from the CLI

The manual registration commands remain available with the Python environment
active. Skip `init` if the workspace already exists:

```sh
swarm-control workspace init --label "My projects"
swarm-control workspace add /path/to/target/swarm.toml --id my-project --label "My project"
swarm-control dashboard --workspace --port 8787
```

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

Use `--require-parallel 100` with `--agents 100` to require an independent ready
frontier of 100 tasks. The browser proposes this automatically when selecting
100 agents; smaller runs can also set a minimum. **Check plan** shows ready and
independent counts, the longest prerequisite chain, theoretical wave sizes and
resource/import review hints. A sequence of 100 dependent tasks cannot satisfy
that requirement. Future waves need merged prerequisites and a newly reviewed
source base; they are not launched from a stale frozen checkout.

`--no-progress-timeout SECONDS` explicitly opts into stopping a worker after that
many seconds without an observed owned-source change (30 seconds up to the
coding limit). The default records warnings and retains the hard coding limit.
Observed source changes do not establish useful or accepted work. The run view
separates per-task phase, original outcome, observed stop reason and reviewable
recovery candidate. See [supervision](WORKER_SUPERVISION.md) and
[dependency validation](DEPENDENCIES.md).

After source review, actual provider/gateway verification and authorization of
the remaining allowance, explicit execution uses:

```sh
swarm-control runs launch my-project RUN_ID --gateway-url GATEWAY_ORIGIN --execute
```

Replace `GATEWAY_ORIGIN` with the separately verified Modal gateway origin,
`https://NAME.modal.run`, without a port, `/v1`, other paths or credentials. The
browser form accepts that same origin format.
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

Browser launches run in the local server while the UI remains available. Watch
the request status and saved run history; refresh after a lost connection before
taking further action. Do not close the server terminal during execution or
automatically retry an uncertain attempt. A run ending with a validated patch
still needs independent review, passing CI and integration before it is a delivery.

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
viewers with no write methods. Workspace mode adds bounded repository connection,
preview/draft creation and explicit confirmed launch requests, protected by exact
Origin/Host checks and a per-server CSRF token. A server accepts one active browser
launch request at a time; this does not replace the controller's durable dispatch
locks or per-run attempt reservation.

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

Worker reports record controller-observed phase timings, bounded event
counters, receipt times and explicit process outcomes. A `-1` or `137` exit alone
never establishes a timeout or OOM cause. Coding runs with empty patches skip the
full helper benchmark after scope/head inspection and remain unvalidated; smoke
checks are unchanged. Recovery and termination still run after failures. A
progress-receipt failure cannot turn partial work into a validated delivery.

The shipped trusted gateway code emits bounded private request
timing receipts for authentication, capacity, reservation, token counting,
pacing, HTTP send, first response byte, streaming and cleanup/settlement.
They contain fixed categories, generated observation IDs and authenticated
run/task identities, without request bodies, headers, tokens or credential
hashes. This adds observations to existing calls and never adds provider calls,
retries or monetary changes. Observer/sink failures remain separate from request
outcomes. The in-process window is bounded and resets on restart; its aggregates
are not a complete run, invoices, inference CPU time or useful-agent overlap.
Durations can overlap and are not additive. Older gateway deployments need an
explicit reviewed redeployment to use this instrumentation; reading history
does not retrofit it. Original unknown timings remain unknown. Raw timing logs
remain private.

See [the next-pilot protocol](NEXT_PILOT.md), [operational feedback](OPERATIONS_SAFETY.md)
and [the dashboard gallery](DASHBOARD.md#screenshots).
The dated [November 11 workspace validation receipt](evidence/run-workspace-validation-v2-2026-11-11.json)
records that revision's offline checks, hashes, durations and limitations. It is
historical evidence, not validation of every later change; see the
[evidence archive](archive/README.md).
