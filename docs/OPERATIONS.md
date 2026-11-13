# Bounded operations

Target execution and integration commands select a profile with
`--project /path/to/target/swarm.toml`. Offline `evidence` commands read artifact
directories or reports directly; see [evidence input formats](contest/EVIDENCE.md).
The examples assume the installed `swarm-control` executable is on PATH.
Live commands require an explicitly enabled, funded policy and existing provider
authentication. Project secret names reference Modal secrets containing CODEX_API_KEY;
real model/GitHub credentials never reach the coding workers.

The browser's **Plan work** is a separate workflow: repository inspection is
local, **Generate work plan** makes an explicitly budgeted model request, and
**Approve and prepare tasks** creates reviewed issues and local preparation.
It does not launch coding workers. The CLI `plan` command below inspects existing
task definitions without calling a model. See [AI planning](AI_PLANNING.md).

Inspect preparation before paid operations:

```sh
swarm-control preflight A1 A2 --project /path/to/target/swarm.toml --workers 2 --synchronized --base HEAD
# Optional read-only GitHub issue/dependency/base/CI checks:
swarm-control preflight A1 A2 --project /path/to/target/swarm.toml --workers 2 --synchronized --base HEAD --online
```

`preflight` reads existing claims/ledger and cached spend without initializing
state, reserving allowance or contacting a provider. Only `--online` permits
GitHub GETs. Exit zero means the checked source-preparation scope passed; it can
coexist with insufficient local allowance, disabled execution and unverified
provider capacity/authorization. `launch_ready` always remains false. Keep the
raw financial report private. See [deployment preflight](DEPLOYMENT_PREFLIGHT.md).

The following commands are distinct paid stages requiring actual authorization:

```sh
swarm-control deploy-gateway --project /path/to/target/swarm.toml
swarm-control probe --project /path/to/target/swarm.toml --gateway-url https://YOUR-GATEWAY.modal.run
swarm-control run A1 A2 --project /path/to/target/swarm.toml --workers 2 --synchronized --model gpt-6.1-sol --model-budget 1.00 --gateway-url https://YOUR-GATEWAY.modal.run
```

Obtain the gateway origin from Modal's deployed application or
`modal.Function.from_name(GATEWAY_APP, "gateway", environment_name=ENVIRONMENT).get_web_url()`.
Use the app and environment from the selected project profile.
Deploy uses the configured app, durable gateway ledger, secret name and environment;
keep the app, ledger, secret and environment identifiers unchanged when migrating
an existing deployment. Extraction deploys nothing.

The probe reserves USD0.50 model and USD1.35 infrastructure allowance. Worker
admission reserves USD0.30 per VM plus USD1 overhead, before provisioning. Models
are priced conservatively with a margin and no cached-input discount. Reservations
are admission controls, not provider invoices. Failures do not refund allocations.
The gateway separately enforces expiring per-worker budgets and durable requests.
See [gateway details](GATEWAY.md).

Each worker gets one task, an exact base, isolated checkout and bounded gateway
token. Synchronized batches wait for all preparations before starting. VM limits
remain 2 CPU/2 GiB maximum, requested at 0.5 CPU/512 MiB. Coding time defaults to
300 seconds; explicit `--timeout` accepts 30–1800 seconds. Lifecycle is coding
timeout plus 900 seconds, at most 2700. Gateway TTL is `max(1800, timeout + 900)`,
within its independent 3600-second ceiling. Thus opting into 1800-second sessions
uses TTL 2700; the default keeps TTL 1800. Longer sessions do not grant allowance.
Patches, hashes, events and final checks are recovered before termination; inspect
any recovery or cleanup failure. Original private artifacts remain under the
target's ignored `.swarm/`, shared with its Git worktrees.

The dispatcher submits at most `--workers` tasks at once and checkpoints each
completion to the task ledger before submitting another. A checkpoint failure
stops new submissions while submitted workers complete their recovery paths.
There is no automatic worker replacement or budget replenishment.

Profiles may opt into up to 100 workers; omitted profile fields and manual
initialization default to ten. An approved AI plan for a new project explicitly
sets both capacities to the reviewed desired concurrency, leaving execution
disabled and allowances zero. Gateway provider slots default to 20 and are
configured independently with
`runtime.gateway_concurrency` (1–100). A synchronized 100-worker run requires
capacity 100, verified API/VM quota and gateway redeployment. Synchronized live
runs exceeding the profile's gateway capacity are rejected before allocation;
plans report insufficiency. Before worker launch, authenticated gateway admission
must also advertise enough capacity; a missing field is accepted only up to 20
workers. A mismatch stops the gateway run with the local allocation retained.
The operator must still match the supplied HTTP URL to that gateway app. The single
gateway accepts 120 HTTP inputs; capacity waiting stops after 15 seconds with
`429 gateway_busy`, no provider call and no budget reservation. This bounds local
pressure; it does not prove provider capacity or useful live overlap.

Optional paired token/request pacing is configured in the target profile and
requires redeployment. It counts token preflights toward RPM, then reserves
verified input plus maximum output token permits for inference. Count and
inference share a 300-second deadline **after** money reservation, whereas the
15-second capacity wait above precedes reservation. Pacing failure/cancellation
retains money and consumed permits; no optimistic refund or refill occurs.
The runner checks exact authenticated deployed TPM/RPM/queue settings against
the profile. Pacing keeps the idle gateway warm for 90 seconds (otherwise 30),
but its history is process-local and resets on crash/redeployment/container restart;
verify actual provider quota and other traffic independently. The HTTP handler
and worker stream idle timeout are 900 seconds (`900000` ms for Codex). Include
all waits/throttles in elapsed time and compute cost. See [gateway policy](GATEWAY.md).

Review each full patch and acceptance tests. Keep one integrator per target.

The trusted controller's native Git must authenticate publication `push` operations,
including for public repositories; private targets also need authenticated `fetch`.
Browser clone/publication configures a credential helper
only for those individual commands; bare integration Git does not gain a helper
from `GH_TOKEN` alone. After authenticating the intended GitHub CLI account,
`gh auth setup-git --hostname github.com` explicitly configures the controller's
helper. See [private repository prerequisites](PRIVATE_REPOSITORIES.md#controller-git-authentication).
Never place account tokens in Git URLs or worker environments.

```sh
swarm-control integrate plan ARTIFACT_DIR --project /path/to/target/swarm.toml
swarm-control integrate prepare ARTIFACT_DIR --project /path/to/target/swarm.toml
swarm-control integrate publish ARTIFACT_DIR --project /path/to/target/swarm.toml --reviewed-patch-sha REVIEWED_SHA256
# Wait for CI on the exact published head.
swarm-control integrate merge ARTIFACT_DIR --project /path/to/target/swarm.toml --reviewed-patch-sha REVIEWED_SHA256
```

Prepare applies the patch on the current configured base branch and runs every
configured command. With a worker manifest produced by `run --allow-overlap`, it
can attempt `git apply --3way --index` if strict application fails and the worker
base is an ancestor. A clean merge reruns combined validation and saves the actual
diff as `integration.patch`, its digest, both bases and the tested tree.

Review both patches for a three-way candidate. Its publish and merge commands
also require `--reviewed-applied-patch-sha APPLIED_SHA256`, matching the saved
`applied_patch_sha256`. The original `--reviewed-patch-sha` remains required.
Actual conflicts save `needs_repair`, conflicting paths and the original patch/apply
log, then clean partial application from the dedicated candidate worktree. Assign
a separate repair attempt and preserve the failed receipt; no agent is launched.

Preparation uses a candidate lock plus a short shared lock for Git worktree
setup; installation and checks run outside the shared lock. Publish/merge retain
the shared local integration lock. Merge checks the current base and green exact-head Actions
across all bounded pages. GitHub atomically checks the head, not the base; other
clones and clients still need coordination. Changed bases need a new validated
attempt with old artifacts preserved.

Publish and merge also read an existing ledger's stored budget policy before
starting and immediately before branch push, PR creation or remote merge. The
selected profile must match both cumulative limits. This prevents an old profile
from causing a local accounting failure after a successful remote merge. The
check opens SQLite read-only and never initializes a ledger, changes allowance or
refunds allocations. A missing ledger preserves integration without local
accounting; an existing ledger with an unreadable or unrecorded policy fails
closed for reconciliation. Select the matching profile instead of resetting the
ledger. These checks do not authorize paid work or replace coordination with
other writers and budget migrations.

After a cohort, verify sandbox termination, stop gateway runs and stop its deployed
app when no other authorized work needs it. Reconcile billing, recorded repairs,
reverts and full delivery time. A validated patch is not a merged green PR.

## Command reference

For a complete first run, follow the [two-agent walkthrough](TWO_AGENT_WALKTHROUGH.md).
These commands do not automatically move through every stage for you.

| Command | What it does | External effects |
| --- | --- | --- |
| `workspace init/add/list/import-snapshot/import-catalog` | Register target configs and preserve curated historical copies in a private local workspace | Local registry/history files only; no allowance or provider changes |
| `runs plan/create/list/show` | Configure agents/tasks and inspect saved run settings and history | Read-only preview, or explicit local draft creation; no cloud calls |
| `runs launch PROJECT RUN` | Inspect a saved draft; `--execute` explicitly invokes the existing runner | Inspection by default; paid resources possible only with `--execute` and ordinary gates |
| `plan TASK...` | Validate local task structure/ownership, resolve a local base ref and print potential concurrency | No cloud calls, budget reservation or GitHub writes |
| `budget status/inspect/plan` | Read local accounting/cached spend or propose a reviewed cumulative increase | Offline inspection; no allowance change or model/provider calls |
| `budget apply` | Apply a separately owner-authorized cumulative increase while execution remains disabled | Local accounting/audit writes; preserves allocations and creates no resources |
| `budget sync-openai` / `budget import-spend` | Explicitly fetch administrative cost evidence or import a private observation | Provider GETs for sync, local evidence writes for either; no spending approval |
| `preflight TASK...` | Inspect prepared source, existing claims/allowance, cached spend and configured resource/runtime requirements | Offline by default; `--online` adds GitHub GETs only; no admission/deployment or launch approval |
| `analyze [TASK...]` | Inspect explicit/inferred dependencies, cycles, source hints and a conservative ready frontier; up to 10,000 backlog definitions | Local metadata and frozen Git source reads only; `--require-parallel 100` fails an insufficient frontier |
| `smoke TASK...` | Create worker environments, clone/install/check the unchanged target and recover artifacts | Paid Modal resources and infrastructure allocation; no coding model calls or delivered feature |
| `deploy-gateway` | Deploy the configured trusted model service | Modal deployment/build costs possible; no coding cohort |
| `probe` | Exercise the real gateway/model/tool round trip in one bounded sandbox | Paid model/Modal usage and durable allowance allocation |
| `run TASK...` | Admit and execute coding tasks, validate/recover patches and attempt cleanup | Paid model/Modal usage and durable claims; no PR publication |
| `integrate plan ARTIFACT_DIR` | Verify local worker artifacts and report the patch hash/scope | Local inspection, including target origin verification |
| `integrate prepare ARTIFACT_DIR` | Fetch the current base, create a candidate worktree, apply the patch and run checks | Local files/worktree and configured commands; GitHub reads |
| `integrate-queue MANIFEST` | Preview reviewed candidates or explicitly execute bounded independent local preparation lanes | Read-only preview; `--execute` runs local checks and fetches the base; optional `--online` uses bounded GitHub GETs; no publication/merge |
| `integrate publish ARTIFACT_DIR` | Require patch review and an unchanged tested candidate, commit/push it and create a PR | GitHub branch/PR writes and any CI they trigger |
| `integrate merge ARTIFACT_DIR` | Require review, current base and successful exact-head checks, merge and reconcile the ledger | GitHub merge/issue closure and any CI it triggers |
| `evidence collect BATCH` | Export sanitized activity, delivery and billing aggregates with original evidence hashes | Offline reads; writes only through explicit stdout redirection |
| `evidence compare SWARM_REPORT AFK_REPORT` | Check a matched workload/protocol and recompute efficiency ratios | Offline reads; no provider or GitHub calls |

`smoke` is not the free quickstart. It requires the live policy and infrastructure
allowance even though it does not call the model. It does not claim a task as a
coding delivery. Use `plan` for inspection without cloud resources.

Plans include a `local_admission` summary. It distinguishes requested potential
concurrency from tasks whose local status, issue number, declared blockers and
dependency metadata permit work. Dependencies need local `merged` status and a
positive `pullRequest` number; the paid admission still verifies their real GitHub
merge and inclusion in the selected base. The summary also checks the configured
worker cap and minimum infrastructure admission reserve. A 100-task cohort needs
a USD31 infrastructure admission reserve before earlier allocations/setup; that
is a local control amount, not actual provider cost.

This summary does not open the budget ledger or contact GitHub/providers. Remaining
allowances stay unknown, and `launch_readiness_verified` stays false even when all
local checks pass. A configured total covering the reserve does not prove remaining
funds or authorize a policy migration. Live execution, durable budget admission,
open issues, deployed gateway capacity and provider quota still need their real
gates before launch. Preparation can be complete while execution remains disabled.

Use [budget migration](BUDGET_MIGRATION.md) to inspect an existing cumulative
ledger and propose a larger ceiling without changing it. Its separately gated
apply operation requires real owner authorization, locks both controller paths
and preserves every prior allocation. It never enables execution or launches a
provider resource.

## Runner flags

For financial context, use `swarm-control budget status --project /path/to/target/swarm.toml`.
Its local reservation amounts, cached provider spend and unknown account credit
are separate fields. Only explicit `budget sync-openai` queries the provider;
status and `plan` make no network calls. See [spend reporting](BILLING.md).

These flags apply to `plan`, `smoke` and `run` unless stated otherwise:

| Flag | Meaning |
| --- | --- |
| `--project PATH` | Required root target configuration |
| `--workers N` | Maximum submitted workers, default 1; accepts 1–100, live is capped by the project limit (default ten) |
| `--agents N` | Alias for `--workers`; concurrent workers are distinct from total selected tasks |
| `--run-id ID` | Optional bounded identity binding a workspace draft to the actual batch and worker reports |
| `--context-mode full\|task` | Preserve full instructions, or additionally provide reviewed task contract excerpts from the frozen base; global rules remain mandatory |
| `--require-parallel N` | Require N conservatively independent ready tasks before admission; 100-agent run/preflight/workspace defaults to 100 |
| `--no-progress-timeout SECONDS` | Explicitly stop after 30–coding-limit seconds without an observed owned-source change; default warnings only |
| `--base REF` | Local commit/ref, default `origin/<base_branch>`; freezes the exact source commit for the selected transport |
| `--source-transport github\|bundle` | Use the profile's transport unless explicitly overridden; `github` clones public source, `bundle` uploads one bounded local frozen-history bundle without account tokens |
| `--timeout SECONDS` | Agent coding time from 30 to 1800, default 300; longer sessions require explicit selection and do not include all setup/recovery time |
| `--synchronized` | Require exactly one task per requested worker; real agents start after all worker preparations reach the barrier |
| `--allow-overlap` | `plan`/`preflight`/`run` only: inspect or permit declared shared ownership in isolated checkouts; worker scope checks remain enforced |
| `--model MODEL` | Required for `run`; only `gpt-6.1-sol` is supported by the current priced gateway |
| `--model-budget USD` | Required for `run`; total cohort model allowance, positive whole cents, at most USD180 and within remaining local allowance |
| `--gateway-url ORIGIN` | Required for `run`; HTTPS `*.modal.run` origin with no `/v1`, credentials, query or port |
| `--environment NAME` | Override the profile's Modal environment for runner operations; all matching apps/secrets must exist there |
| `--retry` | Explicitly retry a reviewed failed claim with remaining allowance; never resets merged/running tasks or budgets |

The gateway's administrative default remains USD18; `run` requires its own explicit
allowance. Raising a capacity field does not migrate the cumulative budget policy.
The CLI `plan` reports dependency/frontier blockers; it does not reject a reported
shortfall as live admission would. Use `analyze --require-parallel N` or
`preflight --require-parallel N` for an explicit preparation exit-status gate.
The model allowance is divided equally among task credentials. For example,
`--model-budget 2.00` for two tasks allocates USD1 per task, not USD2 each.
If there are fewer workers than tasks without synchronization, tasks are queued
locally. The whole live batch still cannot exceed the project's task-count cap.
The `github` transport needs the frozen commit available to its public clone.
The `bundle` transport uses complete local committed history, including private
targets; it excludes dirty and untracked files. Neither transport publishes
preparation or bypasses issue, dependency, source or budget gates. See
[private repositories](PRIVATE_REPOSITORIES.md).

The probe accepts `--project`, `--gateway-url` and an optional `--environment`;
its amount and task are fixed. Deployment accepts only `--project` and uses the
profile's environment. Avoid mixing different environments across these commands.

Integration accepts `--project`, an artifact directory, `--reviewed-patch-sha`
for publish/merge, the additional `--reviewed-applied-patch-sha` for three-way
candidates, and optional `--worktree PATH` for prepare. A worktree override
changes the candidate location; it does not waive ownership, review or validation.

Inspect installed help without launching resources:

```sh
swarm-control --help
swarm-control probe --help
swarm-control deploy-gateway --help
swarm-control integrate --help
```

## Recognizing completion

The runner prints the batch evidence path and each task's final report. Review
the report, recovered patch, final checks, recovery errors and termination result.
Do not infer that every task succeeded merely because artifacts were created.
`smoke_passed`, `probe_passed`, `validated` and `merged` describe different stages.

The integrator's saved `integration.json` links the original and applied patch
digests to the tested base/tree and published head. CI must finish on that exact head, including every
required check name. The integrator checks GitHub Actions check runs; configure
the project and branch protections accordingly rather than assuming arbitrary
external status systems are verified by this code.

Only one integrator should merge into the base at a time. A stale base or failed
check is a reason to recover and revalidate, not to force merge. See
[troubleshooting](TROUBLESHOOTING.md) for preserving attempts and
[evidence](contest/EVIDENCE.md) for the difference between private artifacts and
a public delivery report.
