# Documentation

Swarm Control is a project-configured controller for coding agents. This guide
explains the current implementation, how to operate it, and how to evaluate it
against the Telnyx X Prize requirements. It does not assume access to the author's
accounts or knowledge of the development conversation.

## Choose a reading path

| Reader | Start here | Continue with |
| --- | --- | --- |
| First-time user | [Quickstart: a local two-task example](QUICKSTART.md) | [Architecture](ARCHITECTURE.md) |
| Operator launching real agents | [Two-agent walkthrough](TWO_AGENT_WALKTHROUGH.md) | [Project settings](PROJECTS.md), [operations](OPERATIONS.md), [troubleshooting](TROUBLESHOOTING.md) |
| Competition reviewer | [Presentation and current evidence](contest/PRESENTATION.md) | [Target assessment](contest/TARGET-ASSESSMENT.md), [benchmark protocol](contest/BENCHMARK.md), [evidence requirements](contest/EVIDENCE.md) |
| Contributor | [Architecture](ARCHITECTURE.md) | [Repository contract](../AGENTS.md), [scaling roadmap](SCALING.md), [gateway policy](GATEWAY.md) |

## What is available today

The controller can plan work, dispatch up to 100 independent Codex workers in Modal
under an explicitly configured project capacity, retrieve patches, enforce a budget,
and integrate reviewed changes through GitHub Actions. Defaults remain ten workers
and disabled execution. Shared ownership is opt-in; clean three-way integration
reruns combined checks, while actual conflicts require a separate repair attempt.
The target is a public GitHub repository with prepared tasks and validation commands.
Review and integration still require an operator; there is no automatic backlog
generator or dashboard.

The original Afterimage pilot ran ten real workers and merged ten green PRs.
One delivery needed a coordinator code repair. It did not run 100 agents, test
shared-file conflict repair, or compare against the strongest AFK pipeline.
The larger-cohort dispatcher, gateway backpressure and collision handling have
offline coverage. Useful 100-agent operation and AFK efficiency remain unproven;
a real run is a separate, budgeted operation.

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
