# Target project configuration

Keep `swarm.toml` at the root of the target Git checkout. Configuration is trusted
operator input; review it before enabling live execution. No credential values
belong here. Repository origin must match the selected target.

```toml
version = 1

[project]
repository = "your-org/your-repo"
base_branch = "main"
tasks_dir = "swarm/tasks"
instructions = ["README.md", "AGENTS.md"]

[validation]
install = [["npm", "ci", "--no-audit", "--no-fund"]]
checks = [["npm", "run", "check"]]
required_checks = ["checks"]

[runtime]
worker_app = "your-project-workers"
gateway_app = "your-project-gateway"
gateway_ledger = "your-project-budget-v1"
gateway_concurrency = 20
openai_secret = "your-project-openai"
environment = "main"
image = "node:24.6.0-bookworm-slim"
setup_commands = ["npm install --global npm@11.16.0 @openai/codex@0.166.0"]
network_domains = ["github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com", "registry.npmjs.org"]

[budget]
live_enabled = false
max_live_agents = 10
model_limit_cents = 0
infra_limit_cents = 0
```

The disabled flag and zero budgets are intentional. Specify an allowance only
when authorized. Recorded limits are immutable without an explicit budget
migration; never delete a ledger to obtain new allowance. The original Afterimage
policy remains USD18 model/USD7 infrastructure with all prior allocations intact.
The gateway hard ceiling is USD180 and 100 tasks per run. Its administrative
default remains USD18, and the profile's default capacity remains ten. Larger
capacity does not increase an existing durable allowance or authorize spending.

Install/check commands are argument arrays executed in order, without shell
interpolation. Choose the actual project's language and tests. A Python target
could install via `python3 -m pip install -e .` and test via `python3 -m unittest
discover -s tests`, with an image and allowed domains that support its dependencies.
All images need Git, Python, Node/npm and Codex. The controller installs
Git/ripgrep/certificates and adds Python; setup commands supply the rest.
The same configuration drives both worker and integration checks.

`required_checks` lists GitHub Actions check names required on the exact PR head.
All returned Actions checks must succeed. Check pages are read with bounded
pagination; missing/inconsistent pages, stale heads and changed bases stop integration.

Each `swarm/tasks/A1.json` needs one GitHub issue and precise acceptance criteria:

```json
{
  "id": "A1",
  "title": "Reject malformed search input",
  "prompt": "Implement the documented issue inside src/search.",
  "ownership": ["src/search"],
  "dependsOn": [],
  "acceptance": ["Malformed input returns the documented error; tests pass."],
  "status": "ready",
  "issue": 123
}
```

Dependencies need merged `pullRequest` metadata and must be included in the base.
Completed tasks remain `merged`; the durable ledger also prevents duplicate work.
Use distinct gateway apps/ledgers for unrelated projects. Model compatibility and
pricing are pinned for `gpt-6.1-sol`; another model needs reviewed compatibility
and pricing changes, not just a different flag.

## Configuration reference

The parser accepts only the documented sections and keys. All paths below are
relative to the target root unless stated otherwise. A configuration file inside
a nested template directory is not a separate target checkout.

| Field | Default / required | Meaning |
| --- | --- | --- |
| `version` | Required: `1` | Configuration format version |
| `project.repository` | Required | GitHub `owner/repository`, with no URL or credentials |
| `project.base_branch` | `main` | Target integration branch; default worker ref is `origin/<branch>` |
| `project.tasks_dir` | `swarm/tasks` | Existing directory within the target containing task JSON |
| `project.instructions` | `["README.md", "AGENTS.md"]` | Nonempty list of existing instruction files inside the target |
| `validation.install` | Required | Nonempty list of command argument arrays for dependency preparation |
| `validation.checks` | Required | Nonempty list of command argument arrays for final validation |
| `validation.required_checks` | `["checks"]` | Nonempty list of required GitHub Actions check-run names |
| `runtime.worker_app` | `swarm-control-workers` | Modal application for worker sandboxes |
| `runtime.gateway_app` | `swarm-control-gateway` | Modal application hosting the trusted model gateway |
| `runtime.gateway_ledger` | `swarm-control-budget-v1` | Durable Modal Dict name; preserve it across deployment/migration |
| `runtime.gateway_concurrency` | `20` | Integer 1–100 limiting concurrent upstream requests in the single gateway container |
| `runtime.openai_secret` | `swarm-control-openai` | Modal secret name containing `CODEX_API_KEY` |
| `runtime.environment` | `main` | Modal environment within the authenticated workspace |
| `runtime.image` | `node:24.6.0-bookworm-slim` | Worker container image; the current build expects apt support |
| `runtime.setup_commands` | Pinned npm/Codex install shown above | Nonempty list of trusted image-build command strings |
| `runtime.network_domains` | The four GitHub hostnames shown above | Exact DNS names permitted for worker egress; add dependency registries as needed |
| `budget.live_enabled` | `false` | Explicit gate for deploy, probe, smoke and run |
| `budget.max_live_agents` | `10` | Integer from 1 to 100; caps both task count and requested live workers |
| `budget.model_limit_cents` | `0` | Cumulative local model allocation limit in whole USD cents |
| `budget.infra_limit_cents` | `0` | Cumulative infrastructure admission reserve in whole USD cents |

Both budgets must be positive when live execution is enabled. Each schema limit
can be at most 1,000,000 cents, but that does not remove the per-run USD180 gateway
ceiling or the original user's authorization. Existing
SQLite policy limits must match the stored policy exactly.

For a synchronized 100-agent stage, the target must explicitly configure both
`budget.max_live_agents = 100` and `runtime.gateway_concurrency = 100`, after
verifying model/API and VM quota. Synchronized live runs exceeding the configured
gateway capacity are rejected before allocation. Plans report that insufficiency.
Deployment carries the
latter through `SWARM_GATEWAY_CONCURRENCY`. The serving container admits 120 HTTP
inputs; provider capacity waits at most 15 seconds before a no-charge 429. Keep
the checked-in tutorial's disabled live policy and zero budgets. Changing gateway
capacity requires redeployment. The runner checks authenticated gateway admission
capacity before synchronized worker launch; cohorts above 20 reject a missing
capacity field. Admission mismatches stop the run with its allocation retained.
The operator must still verify the supplied HTTP URL belongs to that gateway app.

Runtime app/ledger/secret/environment names accept 1–100 letters, digits,
underscores or hyphens. Network entries are hostnames, without URL schemes,
ports, paths or wildcards. The runner adds the selected gateway hostname to the
worker allowlist. Image building is a separate provider operation; these network
settings describe worker sandbox egress, not a universal image-build restriction.

`validation.install` and `validation.checks` do not interpret shell operators.
Use separate arrays for separate commands. `runtime.setup_commands` are trusted
image-build strings and must be reviewed with the rest of the profile. The local
integrator needs the target toolchain too: running a Python-only controller does
not install Node/npm or other project tools on the operator's machine.

The [demo profile](../examples/demo-target/swarm.toml) supplies a working Python
example with no third-party target dependencies. The image still contains the
Node-based Codex CLI. Other language stacks require compatible image setup,
dependency domains and local integration tools.

## Task reference and lifecycle

| Field | Contract |
| --- | --- |
| `id` | Matches its JSON filename; 2–40 characters, first an uppercase letter, then uppercase letters/digits/underscore/hyphen |
| `title` | Nonempty concise change description; integration uses `[ID]` in PR titles |
| `prompt` | Nonempty implementation instructions, scope and relevant constraints |
| `ownership` | Nonempty list of repository-relative file or directory paths |
| `acceptance` | Nonempty list of testable acceptance statements |
| `dependsOn` | List of existing task IDs, or omitted/empty for independent work |
| `status` | Explicitly use `ready` before dispatch and `merged` after delivery; live admission accepts only `ready` (also the default if omitted) |
| `issue` | Positive integer naming a real open issue in this target; unique within the cohort |
| `pullRequest` | Actual merged PR number for a completed dependency |

Ownership includes a path and its descendants. `src/search` therefore conflicts
with `src/search/parser.py`, while separate sibling files can be dispatched
together. Duplicate tasks are always rejected. Overlap is rejected by default;
`plan`/`run --allow-overlap` explicitly permits shared ownership and records the
overlapping pairs in the plan and worker manifests. Paths may not escape the
repository, and every worker remains restricted to its own declared ownership.
Shared-file tasks execute from the same base in isolated checkouts. Integration
validates each combined result; real conflicts produce a repair receipt rather
than an automatic repair worker.

Planning validates local task structure and the selected ownership policy. Live `run` also
checks the issue is open, there is no existing open PR titled with `[ID]`, and
each dependency has a real merged PR whose merge commit is an ancestor of the
selected base. Open PR admission reads up to 100 pages of 100 entries and stops
on invalid/repeated data or an exhausted page bound instead of assuming the first
page is complete.

The controller does not generate a backlog, open issues or update task JSON to
`merged`. Its ledger records integration, and the PR closes the linked issue.
Update `status` and `pullRequest` in a reviewed bookkeeping change afterward so
later tasks can resolve dependencies. Do not count that change as a feature
delivery. Use new task IDs and real issues for new work instead of recycling
completed pilot tickets.

## Connecting an existing larger repository

1. Establish a passing baseline, document architecture/contracts and identify
   the full local/CI toolchain.
2. Add a reviewed root profile with live execution disabled, instructions and
   dependency domains appropriate to that project.
3. Translate a small set of actual issues into bounded tasks with ownership,
   dependencies and independent acceptance tests.
4. Run a local plan, then an authorized compatibility probe and small cohort
   using [the walkthrough](TWO_AGENT_WALKTHROUGH.md).
5. Measure integration and quality before expanding the workload. Use the
   [scaling roadmap](SCALING.md) for larger-cohort capacity and evidence gates.

The repository remains owned by its maintainers; the controller is an installed
tool targeting it. Copying controller source or the author's credential names
into every application is unnecessary. Each operator configures their own service
accounts, and each unrelated project needs distinct gateway/ledger identifiers.
