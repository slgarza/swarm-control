# Bounded operations

Every command selects a target with `--project /path/to/target/swarm.toml`.
The examples assume the installed `swarm-control` executable is on PATH.
Live commands require an explicitly enabled, funded policy and existing provider
authentication. Project secret names reference Modal secrets containing CODEX_API_KEY;
real model/GitHub credentials never reach the coding workers.

```sh
swarm-control deploy-gateway --project /path/to/target/swarm.toml
swarm-control probe --project /path/to/target/swarm.toml --gateway-url https://YOUR-GATEWAY.modal.run
swarm-control run A1 A2 --project /path/to/target/swarm.toml --workers 2 --synchronized --model gpt-6.1-sol --model-budget 1.00 --gateway-url https://YOUR-GATEWAY.modal.run
```

Obtain the gateway origin from Modal's deployed application or
`modal.Function.from_name(GATEWAY_APP, "gateway").get_web_url()`.
Deploy uses the configured app, durable gateway ledger, secret name and environment;
keep all three identifiers unchanged when migrating an existing deployment.
Afterimage's profile retains its original identifiers. Extraction deploys nothing.

The probe reserves USD0.50 model and USD1.35 infrastructure allowance. Worker
admission reserves USD0.30 per VM plus USD1 overhead, before provisioning. Models
are priced conservatively with a margin and no cached-input discount. Reservations
are admission controls, not provider invoices. Failures do not refund allocations.
The gateway separately enforces expiring per-worker budgets and durable requests.
See [gateway details](GATEWAY.md).

Each worker gets one task, an exact base, isolated checkout and bounded gateway
token. Synchronized batches wait for all preparations before starting. VM limits
remain 2 CPU/2 GiB maximum, 300 seconds agent time and 1200 seconds total lifecycle.
Patches, hashes, events and final checks are recovered before termination; inspect
any recovery or cleanup failure. Original private artifacts remain under the
target's ignored `.swarm/`, shared with its Git worktrees.

Review each full patch and acceptance tests, then use one integrator:

```sh
swarm-control integrate plan ARTIFACT_DIR --project /path/to/target/swarm.toml
swarm-control integrate prepare ARTIFACT_DIR --project /path/to/target/swarm.toml
swarm-control integrate publish ARTIFACT_DIR --project /path/to/target/swarm.toml --reviewed-patch-sha REVIEWED_SHA256
# Wait for CI on the exact published head.
swarm-control integrate merge ARTIFACT_DIR --project /path/to/target/swarm.toml --reviewed-patch-sha REVIEWED_SHA256
```

Prepare applies the patch on the current configured base branch and runs every
configured command. Publish/merge bind review to the patch hash and tested tree;
merge checks the current base and green exact-head Actions. GitHub atomically
checks the head, not the base, so do not merge through a second client concurrently.
Changed bases need a new validated attempt with old artifacts preserved.

After a cohort, verify sandbox termination, stop gateway runs and stop its deployed
app when no other authorized work needs it. Reconcile billing, recorded repairs,
reverts and full delivery time. A validated patch is not a merged green PR.

## Command reference

For a complete first run, follow the [two-agent walkthrough](TWO_AGENT_WALKTHROUGH.md).
These commands do not automatically move through every stage for you.

| Command | What it does | External effects |
| --- | --- | --- |
| `plan TASK...` | Validate local task structure/ownership, resolve a local base ref and print potential concurrency | No cloud calls, budget reservation or GitHub writes |
| `smoke TASK...` | Create worker environments, clone/install/check the unchanged target and recover artifacts | Paid Modal resources and infrastructure allocation; no coding model calls or delivered feature |
| `deploy-gateway` | Deploy the configured trusted model service | Modal deployment/build costs possible; no coding cohort |
| `probe` | Exercise the real gateway/model/tool round trip in one bounded sandbox | Paid model/Modal usage and durable allowance allocation |
| `run TASK...` | Admit and execute coding tasks, validate/recover patches and attempt cleanup | Paid model/Modal usage and durable claims; no PR publication |
| `integrate plan ARTIFACT_DIR` | Verify local worker artifacts and report the patch hash/scope | Local inspection, including target origin verification |
| `integrate prepare ARTIFACT_DIR` | Fetch the current base, create a candidate worktree, apply the patch and run checks | Local files/worktree and configured commands; GitHub reads |
| `integrate publish ARTIFACT_DIR` | Require patch review and an unchanged tested candidate, commit/push it and create a PR | GitHub branch/PR writes and any CI they trigger |
| `integrate merge ARTIFACT_DIR` | Require review, current base and successful exact-head checks, merge and reconcile the ledger | GitHub merge/issue closure and any CI it triggers |

`smoke` is not the free quickstart. It requires the live policy and infrastructure
allowance even though it does not call the model. It does not claim a task as a
coding delivery. Use `plan` for inspection without cloud resources.

## Runner flags

These flags apply to `plan`, `smoke` and `run` unless stated otherwise:

| Flag | Meaning |
| --- | --- |
| `--project PATH` | Required root target configuration |
| `--workers N` | Maximum simultaneous worker threads, default 1; planning accepts 1–100, live is capped by the project limit (at most ten) |
| `--base REF` | Local commit/ref, default `origin/<base_branch>`; workers need that exact commit available remotely |
| `--timeout SECONDS` | Agent time limit from 30 to 300, default 300; does not include all setup/recovery time |
| `--synchronized` | Require exactly one task per requested worker; real agents start after all worker preparations reach the barrier |
| `--model MODEL` | Required for `run`; only `gpt-6.1-sol` is supported by the current priced gateway |
| `--model-budget USD` | Required for `run`; total cohort model allowance, positive whole cents, at most USD18 and within remaining local allowance |
| `--gateway-url ORIGIN` | Required for `run`; HTTPS `*.modal.run` origin with no `/v1`, credentials, query or port |
| `--environment NAME` | Override the profile's Modal environment for runner operations; all matching apps/secrets must exist there |
| `--retry` | Explicitly retry a reviewed failed claim with remaining allowance; never resets merged/running tasks or budgets |

The model allowance is divided equally among task credentials. For example,
`--model-budget 2.00` for two tasks allocates USD1 per task, not USD2 each.
If there are fewer workers than tasks without synchronization, tasks are queued
locally. The whole live batch still cannot exceed the project's task-count cap.

The probe accepts `--project`, `--gateway-url` and an optional `--environment`;
its amount and task are fixed. Deployment accepts only `--project` and uses the
profile's environment. Avoid mixing different environments across these commands.

Integration accepts `--project`, an artifact directory, `--reviewed-patch-sha`
for publish/merge, and optional `--worktree PATH` for prepare. A worktree override
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

The integrator's saved `integration.json` links the reviewed patch to the tested
base/tree and published head. CI must finish on that exact head, including every
required check name. The integrator checks GitHub Actions check runs; configure
the project and branch protections accordingly rather than assuming arbitrary
external status systems are verified by this code.

Only one integrator should merge into the base at a time. A stale base or failed
check is a reason to recover and revalidate, not to force merge. See
[troubleshooting](TROUBLESHOOTING.md) for preserving attempts and
[evidence](contest/EVIDENCE.md) for the difference between private artifacts and
a public delivery report.
