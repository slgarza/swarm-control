# Documentation

Swarm Control is a project-configured controller for coding agents. This guide
explains the current implementation, how to operate it, and how to evaluate it
against the Telnyx X Prize requirements. It does not assume access to the author's
accounts or knowledge of the development conversation.

## Choose a reading path

| Reader | Start here | Continue with |
| --- | --- | --- |
| First-time user | [Easy Run and a local two-task example](QUICKSTART.md) | [Browser setup](SETUP.md), [repositories and runs](RUNS.md), [architecture](ARCHITECTURE.md) |
| Operator preparing deployment | [Deployment preflight](DEPLOYMENT_PREFLIGHT.md) | [Project settings](PROJECTS.md), [spend reporting](BILLING.md), [operations](OPERATIONS.md) |
| Operator viewing a run | [Local evidence dashboard](DASHBOARD.md) | [Spend reporting](BILLING.md), [evidence requirements](contest/EVIDENCE.md) |
| Operator configuring runs | [Repositories and runs](RUNS.md) | [Next-pilot protocol](NEXT_PILOT.md), [deployment preflight](DEPLOYMENT_PREFLIGHT.md) |
| Operator preparing 100 workers | [Dependencies and independent frontiers](DEPENDENCIES.md) | [Worker supervision](WORKER_SUPERVISION.md), [integration preparation lanes](INTEGRATION_PIPELINE.md) |
| Operator investigating failures | [Operational feedback](OPERATIONS_SAFETY.md) | [Operations](OPERATIONS.md), [troubleshooting](TROUBLESHOOTING.md) |
| Operator launching real agents | [Two-agent walkthrough](TWO_AGENT_WALKTHROUGH.md) | [Deployment preflight](DEPLOYMENT_PREFLIGHT.md), [operations](OPERATIONS.md), [troubleshooting](TROUBLESHOOTING.md) |
| Competition reviewer | [Presentation and current evidence](contest/PRESENTATION.md) | [Target assessment](contest/TARGET-ASSESSMENT.md), [benchmark protocol](contest/BENCHMARK.md), [evidence requirements](contest/EVIDENCE.md) |
| Contributor | [Architecture](ARCHITECTURE.md) | [Repository contract](../AGENTS.md), [scaling roadmap](SCALING.md), [gateway policy](GATEWAY.md) |
| GUI contributor | [GUI design](GUI_DESIGN.md) | [Local dashboard](DASHBOARD.md), [browser setup](SETUP.md) |

## What is available today

The controller can plan work, dispatch up to 100 independent Codex workers in Modal
under an explicitly configured project capacity, retrieve patches, enforce a budget,
and integrate reviewed changes through GitHub Actions. Defaults remain ten workers
and disabled execution. Shared ownership is opt-in; clean three-way integration
reruns combined checks, while actual conflicts require a separate repair attempt.
The target is a GitHub repository with prepared tasks and validation commands.
Private source can use a frozen [bundle transport](PRIVATE_REPOSITORIES.md)
without forwarding GitHub credentials to coding workers.
Review and integration still require an operator. The local dashboard presents
curated evidence and links, with explicit project and run selection. Workspace
mode adds local repository connection, configurable concurrent agents, saved
drafts, confirmed launch through the existing controller gates and sealed history.
Easy Run installs and opens this interface. [Browser setup](SETUP.md) configures personal
accounts and `.env`, clones/forks repositories, prepares task drafts, publishes
reviewed preparation and exposes CLI operations. [AI planning](AI_PLANNING.md)
generates dependency-validated work proposals from a repository and goal, with
explicit model budget and human approval before creating real issues and files.
Semantic review still requires operator judgment. The [dashboard gallery](DASHBOARD.md#screenshots)
contains actual English desktop, mobile, task, budget and empty-state captures.

Read-only dependency analysis handles larger backlogs while preserving the
100-worker cohort capacity. It reports a conservative ready frontier instead of
equating total task count with concurrent work. Worker supervision preserves
observed stop reasons, and configured task acceptance runs beside baseline
validation. The explicit local integration pipeline can prepare independent
reviewed candidates concurrently; publication and merge remain separate gates.

The original Afterimage pilot ran ten real workers and merged ten green PRs.
One delivery needed a coordinator code repair. It did not run 100 agents, test
shared-file conflict repair, or compare against the strongest AFK pipeline.
The November 11 hundred-worker pilot observed 100 distinct machines, but its
original reports were all failures: zero validated deliveries. The screenshot
gallery records 56 provisional product recoveries and two separate support
recoveries after operator assistance. Final acceptance, attributable invoices
and the AFK comparison remain unknown. Machine concurrency does not establish
100 useful agents or an efficiency advantage.

Persistent operational brakes now hold new admission after shared failures;
already admitted workers drain normally. Merge actions have durable attempt
reservations, and CI diagnosis and scaling advice are read-only. These controls
have offline coverage and were added after that worker cohort; they receive no
delivery credit in its benchmark and authorize no additional spending.

Read-only `preflight` separates checked source preparation from local admission,
current provider capacity and actual authorization. Its successful exit never
grants a launch. Optional paired gateway token/request pacing and opt-in longer
coding sessions have bounded queues/expiry; the default session remains 300
seconds. Pacing waits and retained reservations are reported separately from
actual provider spend and useful concurrent contribution.

## Terminology

| Term | Meaning here |
| --- | --- |
| Agent | A Codex process using a model, instructions, context and tools to complete one task |
| Worker / sandbox | The temporary Modal machine hosting that process and its project checkout |
| Controller | The local program assigning tasks, reserving budget and collecting results |
| Gateway | The trusted Modal service holding the real OpenAI key and enforcing worker model quotas |
| Task / ticket | A bounded change with ownership, acceptance criteria and a GitHub issue |
| Batch / cohort | A set of tasks dispatched together from one base commit |
| Patch | The exact source changes returned by a worker, before integration |
| PR | A GitHub pull request proposing those changes |
| Green | The required checks passed on the exact commit being reviewed |
| Merged green PR | An accepted delivery incorporated into the target branch after checks |
| AFK pipeline | A competing workflow that completes the defined workload without ongoing operator help |

## Where information lives

Controller code is in `swarm_control/`. A target repository contains its own
`swarm.toml`, instructions and task JSON. Its ignored `.swarm/` directory retains
private runtime evidence and the cumulative SQLite ledger. Git worktrees share
that original checkout's state. Modal and OpenAI use credentials configured by
the operator; downloading this repository grants no access to the author's accounts.

The tutorials label local-only commands and paid operations separately. The
checked-in demo always starts with live execution disabled and zero allowance.
