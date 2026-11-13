# Documentation

These guides describe the shipped Swarm Control dashboard, CLI and controller.
The documentation audit on November 13, 2026 checked instructions against runtime
source at `491d09e`. This audit updates guides and inline help without changing
controller behavior.
Historical screenshots and validation totals are kept in the [archive](archive/README.md)
and are not current operating instructions.

## Start with your project

Run `./easy-run` from the controller checkout, then use **Settings → Projects →
Plan work → Project settings → New run → Runs**. A saved run has Summary, Workers,
Changes, Costs and Evidence tabs. Opening a page or selecting a repository does
not start a model request or coding workers.

| Reader | Start here | Continue with |
| --- | --- | --- |
| First-time user with a real repository | [Browser setup](SETUP.md) | [AI planning and approval](AI_PLANNING.md), [run configuration](RUNS.md) |
| User learning without service accounts | [Local quickstart](QUICKSTART.md) | [Architecture](ARCHITECTURE.md), [browser setup](SETUP.md) |
| User connecting GitHub or personal keys | [Connections](CONNECTIONS.md) | [Repository clone/fork and preparation](REPOSITORY_SETUP.md), [private source](PRIVATE_REPOSITORIES.md) |
| Operator preparing a profile | [Project reference](PROJECTS.md) | [Preflight](DEPLOYMENT_PREFLIGHT.md), [budgets and billing](BILLING.md) |
| Operator launching workers | [Run configuration](RUNS.md) | [Two-agent walkthrough](TWO_AGENT_WALKTHROUGH.md), [operations](OPERATIONS.md) |
| Operator reviewing dependencies or capacity | [Dependencies and independent frontiers](DEPENDENCIES.md) | [Scaling limits](SCALING.md), [worker supervision](WORKER_SUPERVISION.md) |
| Operator reviewing returned patches | [Integration preparation](INTEGRATION_PIPELINE.md) | [Operations](OPERATIONS.md), [operational holds](OPERATIONS_SAFETY.md) |
| Operator investigating a failure | [Troubleshooting](TROUBLESHOOTING.md) | [Operational holds](OPERATIONS_SAFETY.md), [budget migration](BUDGET_MIGRATION.md) |
| Benchmark reviewer | [Current capability and evidence overview](contest/PRESENTATION.md) | [Benchmark protocol](contest/BENCHMARK.md), [evidence schema](contest/EVIDENCE.md), [target assessment](contest/TARGET-ASSESSMENT.md) |
| Contributor | [Architecture](ARCHITECTURE.md) | [Repository contract](../AGENTS.md), [gateway policy](GATEWAY.md), [GUI design](GUI_DESIGN.md) |
| Reader viewing the interface | [Current screenshot gallery](DASHBOARD.md#screenshots) | [Dashboard and read-only viewer](DASHBOARD.md) |

## Current capabilities and boundaries

| Work | Shipped behavior | Separate requirement |
| --- | --- | --- |
| Acquire source | Clone/fork a GitHub repository or inspect a local Git checkout | Own GitHub access; an existing local checkout needs a matching GitHub origin |
| Prepare a work plan | Inspect bounded tracked source, send one explicitly budgeted OpenAI request, validate tasks and dependencies | Review the selected context, commands, ownership, acceptance and plan |
| Materialize a plan | Explicit approval creates real issues and local task/profile files | Captured verified GitHub account, unchanged source and exact review binding |
| Execute tasks | Save a draft, then explicitly launch up to 100 workers within the project and gateway limits | Published source where required, own provider setup, authorized model/infrastructure allowance and current capacity |
| Integrate patches | Prepare reviewed candidates, run checks and publish/integrate through GitHub | Authenticated native Git on the controller, independent semantic review and passing checks on the actual candidate/base |
| Inspect outcomes | Selected-run configuration, curated evidence, separate cost/reservation fields and private attempt history | Real source observations; missing activity, invoices and acceptance stay unknown |

Static dependency analysis can reject cycles, ownership conflicts and an
insufficient independent frontier. It cannot prove semantic independence or
useful live concurrency. Supporting 100-task plans is not a measurement of 100
useful live agents. The controller does not implement an unattended semantic
reviewer or an AFK comparison product.

The project loader and manual profile initializer default to ten live-agent
slots; their gateway concurrency default is twenty. The AI planner sets a new
profile's two capacity fields to the reviewed worker count. New initialized
profiles have zero execution allowance and live execution disabled. Existing
profiles and cumulative accounting are preserved. See [project configuration](PROJECTS.md)
for the exact defaults and required explicit changes.

## Where state lives

| Location | Responsibility |
| --- | --- |
| Controller checkout | Program, interface, CLI, examples and these guides |
| Target Git checkout | Application code, `swarm.toml`, instructions and task JSON |
| Personal workspace registry | Connected projects and saved runs; default `~/.config/swarm-control/workspace.json` |
| Registry companion folders | Connection settings, planning reservations, setup jobs and preparation records |
| Target's ignored `.swarm/` | Worker accounting, claims, returned patches and private run evidence |
| Modal | Worker machines and the trusted model gateway |
| GitHub | Issues, source history, PRs and CI |

Keep the registry and its companion folders outside Git; the default location
does so. Personal credential storage and AI planning enforce their private
storage checks. A custom registry location does not make every companion file
safe to commit. Each operator supplies their own keys on their own computer;
downloading this repository grants no access to the author's accounts. See
[credential boundaries](CONNECTIONS.md) and [private source transport](PRIVATE_REPOSITORIES.md).

Git worktrees share the original checkout's durable accounting and controller
locks. Separate clones or hosts do not share those locks automatically.

## Maintaining these guides

All current prose, interface instructions, examples and templates use English.
Proper names, Unicode test inputs and recorded external identifiers are data,
not translated instructions. Check documented commands against the CLI help and
actual source before changing them. Match browser control names to the shipped
interface. Keep examples visibly separate from real run evidence and provider
invoices.

Use the root [validation commands](../README.md#validation) before publication.
The [archive](archive/README.md) preserves dated observations and source hashes;
its earlier workflows must not be copied into a current setup guide.
