# Read-only deployment preparation

`swarm-control preflight` inspects a proposed cohort without reserving allowance,
initializing a ledger, deploying a gateway, building an image or starting agents.
It reports what was checked and what remains unknown. Its successful exit covers
**checked source preparation only**; it never grants launch authorization.

Keep target-specific release packets, task IDs, budgets and provider evidence in
the target project. The controller supplies this generic inspection command.
Read the target's own acceptance/benchmark policy before approving live work.

## Inspect the proposed cohort

With the installed controller environment active, select actual target tasks:

```sh
swarm-control budget status --project /path/to/target/swarm.toml --json
swarm-control preflight A1 A2 --project /path/to/target/swarm.toml \
  --workers 2 --synchronized --base HEAD --model-budget 1.00
```

These commands are offline. The model amount is a proposed **total cohort
reservation**, not predicted spend or permission. `preflight` checks local
task/ownership/dependency metadata, origin, clean checkout and selected base,
existing durable task claims, configured worker/gateway capacity, the original
ledger's retained/unreserved allowance and cached spend provenance. It also
reports resource requests/caps and bounded runtime requirements. It does not
run target install/tests or verify actual provider credentials/quota.

For a deliberately longer candidate, add `--timeout 1800`. The unchanged default
is 300 seconds; the accepted range is 30–1800. The report shows the selected
coding limit, sandbox lifecycle (`timeout + 900`) and gateway TTL
(`max(1800, timeout + 900)`). An 1800-second selection therefore needs a 2700-second
lifecycle/TTL. The independent gateway TTL ceiling is 3600. Selecting these
values changes no live flag, budget policy or deployed service.

Only this explicit option permits GitHub GETs:

```sh
swarm-control preflight A1 A2 --project /path/to/target/swarm.toml \
  --workers 2 --synchronized --base HEAD --model-budget 1.00 --online
```

It checks open issues and absence of conflicting open task PRs, real merged
dependencies in the base, reachability from the configured remote branch and
required checks on the exact base. It does not push branches, mutate issues,
create PRs or call Modal/OpenAI. GitHub access failure or incomplete pagination
stays an explicit failed source check. A historical result does not prove issue
state when a later live batch starts.

## Interpret the result correctly

| Output | Meaning |
| --- | --- |
| `preparation_checks_passed` | Local source checks, plus GitHub source checks when requested, passed |
| Exit `0` | The checked source-preparation scope passed; local financial/live gates can still fail |
| Exit `1` | Checked preparation failed; invalid arguments or inspection errors can also produce nonzero exits |
| `local_workload_blockers` | Source/configuration/claim problems requiring reviewed correction |
| `local_admission_blockers` | Existing allowance, active work or proposed-reservation gaps; these are separate from provider funds |
| `launch_blockers` | The combined remaining gaps, including unverified provider quota/deployment/owner release |
| `launch_ready: false` | The command cannot attest the external gates or authorize execution |
| `configured_provider_pacing` | Proposed profile values and bounded queue, with deployed match still unverified |
| `resource_plan` | Configured worker requests/caps and logical workers + gateway/helper inventory, not verified provider quota |

A clean source can produce exit zero while execution is disabled or the ledger
cannot admit the proposal. Conversely, passing local financial admission does
not prove provider credit, actual quota, current deployed capacity or owner
consent. The report's billing evidence may be aggregate, old or unassigned to the
run; retain its source/scope/unknowns. Keep raw reports and account details private
and publish only reviewed aggregates. SQLite read-only inspection does not change
SQL state, although SQLite can manage WAL coordination sidecars; see
[budget migration](BUDGET_MIGRATION.md).

## Resolve live gates in separate stages

1. Freeze a clean pushed source, exact task objects/issue identities, ownership,
   baseline checks, independent acceptance, PR granularity and full delivery
   clock/deadline. Name actual reviewers, integrator and repair owners. Retain a
   fresh preparation receipt; no task substitution is implicit.
2. Obtain the actual owner's bounded setup/run budget and release. Refresh
   [spend status](BILLING.md), distinguishing retained reservations from invoices
   and unknown credit. If required, use a separately authorized
   [cumulative migration](BUDGET_MIGRATION.md); preserve all allocations and keep
   live disabled during it. A proposal or screenshot never grants more allowance.
3. Verify current worker/gateway/admin quota and the selected model/account's
   actual RPM/TPM and applicable limits. Resource requests and caps can consume
   different provider quota pools; verify rather than assuming one shared pool.
   A batch-queue label alone is not an interactive daily token cap.
4. Only within authorized setup, deploy the reviewed trusted gateway and verify
   its actual origin, app/environment/ledger, capacity and exact optional pacing
   settings. Complete the authorized compatibility probe and preserve its
   allocations, failures, timing and cleanup. Builds/probes are paid stages.
5. Notify the owner with the concrete release, ceilings, quota/deployment/probe
   evidence and remaining limitations before dispatch. Obtain any still-required
   run release; silence is not approval. Dispatch only the frozen cohort and
   record workload release before admission/startup. Workers hold no real provider
   or GitHub key; review/integration stays in the trusted credentialed lane.
6. Observe real contributions, failed attempts, pacing/CI queues, independent
   acceptance, assisted integration, repairs, quality window, actual billing and
   cleanup. There is no automatic replacement worker, allowance refill or refund.

Local admission precedes authenticated gateway compatibility checks, so a
deployed-settings mismatch can retain allocations without starting workers.
Preflight exposes configuration but cannot replace the independent live proof.
See [operations](OPERATIONS.md), [project fields](PROJECTS.md),
[gateway policy](GATEWAY.md) and [scaling](SCALING.md) for the exact controls.
An unpaired scale pilot can provide useful evidence without an AFK run; a contest
efficiency claim still requires the agreed strong paired AFK comparison and
reconciled eligible cost.
