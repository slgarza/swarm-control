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
The gateway caps each run at USD18 and ten tasks in this release even when a
project's cumulative policy is larger.

Install/check commands are argument arrays executed in order, without shell
interpolation. Choose the actual project's language and tests. A Python target
could install via `python3 -m pip install -e .` and test via `python3 -m unittest
discover -s tests`, with an image and allowed domains that support its dependencies.
All images need Git, Python, Node/npm and Codex. The controller installs
Git/ripgrep/certificates and adds Python; setup commands supply the rest.
The same configuration drives both worker and integration checks.

`required_checks` lists GitHub Actions check names required on the exact PR head.
All returned Actions checks must succeed. Missing checks, pagination, stale heads
and changes to the base stop integration.

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
