# Swarm Control

A controller for coding agents working on independent GitHub repositories.
It assigns tasks, starts isolated Codex workers in Modal, retrieves patches,
enforces budgets and ownership, and integrates reviewed changes through passing
GitHub Actions. OpenAI supplies the model; Modal supplies temporary machines.
The coordinator currently runs on one host.

## Open the app

With Git and Python 3.12 or newer installed, run these commands in a terminal
on Linux, macOS or Windows through WSL:

```sh
git clone https://github.com/slgarza/swarm-control.git
cd swarm-control
./easy-run
```

Easy Run creates a local Python environment, installs the controller on first use
and opens the browser. Keep the terminal open; press Ctrl+C there to stop the
server. Next time, run `./easy-run` from the same folder. No cloud workers start
when you open the app. Use `./easy-run --no-open` to print the URL without opening
a browser, or `./easy-run --port 8788` if the default port is busy.

**[Quickstart with a local example](docs/QUICKSTART.md)** ·
**[Set up your accounts and project in the browser](docs/SETUP.md)**

Open **Settings** to configure your own accounts and private local `.env`.
In **Projects**, add a GitHub URL to clone or fork, or select a local Git folder.
Open **Plan work**, inspect the selected repository, describe your goal and
authorize a bounded OpenAI planning request. Review the proposed commands,
ownership, acceptance criteria and dependency analysis before approving creation
of real GitHub issues and local task files. Existing prepared projects also work.

Use **Project settings** to check and publish preparation, configure execution
allowances, create the worker gateway secret and deploy. **New run** selects the
reviewed tasks and saves a draft; its separate **Run agents** action uses the
source, gateway and budget gates. **Runs** contains history. Workers, changes,
costs and evidence are tabs inside a selected run. See [browser setup](docs/SETUP.md),
[AI planning](docs/AI_PLANNING.md) and [repositories and runs](docs/RUNS.md).

Each person supplies their own credentials on their own computer. The personal
`.env` has private file permissions outside every Git repository. Its values stay
out of browser storage, operation history and preparation commits. Uploading an
OpenAI key to a named Modal Secret is a separate explicit action. Saving settings
starts no coding agents.

![Repository selection and recorded run history](docs/images/dashboard-run-history-2026-11-11.png)

Workspace captured on November 11, 2026, at 17:24 Mexico City time (23:24 UTC),
showing sealed historical evidence from 16:23 Mexico City time (22:23 UTC):
100 original failed workers, 56 provisional product recoveries and two separate
support recoveries. Final quality, billed cost and AFK comparison remain unknown.

**Start with the [local quickstart](docs/QUICKSTART.md)** to understand the system
without credentials or cloud resources. Then follow the
[two-agent walkthrough](docs/TWO_AGENT_WALKTHROUGH.md) to run real workers using
your own GitHub, Modal and OpenAI accounts and an authorized budget.

This project is being developed toward the Telnyx X Prize: at least 100 useful
agents working on one real repository, beating a strong AFK pipeline on merged
green PRs per hour and per dollar. The [first ten-agent pilot](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot10-2026-11-10.md)
delivered ten reviewed, merged PRs, including one coordinator code repair.
**The 100-agent requirement and AFK efficiency comparison remain unproven.**

## Documentation

Open a [local evidence dashboard](docs/DASHBOARD.md) with
`swarm-control dashboard --snapshot /path/to/curated-evidence.json`.
It shows worker outcomes, separately attributed recovery, PR/CI links and financial
unknowns. The loopback viewer makes no provider calls or execution/accounting writes.

| Guide | What it explains |
| --- | --- |
| [Documentation index](docs/README.md) | Reading paths, terminology and current capabilities |
| [Browser setup](docs/SETUP.md) | Personal accounts/.env, clone/fork, task preparation, local CLI controls and reviewed deployment |
| [AI planning](docs/AI_PLANNING.md) | Repository and goal to a budgeted model request, dependency review and approved real issue/task preparation |
| [Connections](docs/CONNECTIONS.md) | Credential storage, explicit verification and Modal Secret boundaries |
| [Repository setup](docs/REPOSITORY_SETUP.md) | Clone/fork, imported issues, protected task history and exact preparation publication |
| [Quickstart](docs/QUICKSTART.md) | Easy Run, a tiny local target, baseline tests and a local plan |
| [Architecture](docs/ARCHITECTURE.md) | Who writes code, where it runs, how patches reach GitHub and where state lives |
| [Two-agent walkthrough](docs/TWO_AGENT_WALKTHROUGH.md) | Own-account setup, real issues, budget, execution, review, PRs, merge and cleanup |
| [Project reference](docs/PROJECTS.md) | Every configuration field and the task format |
| [Operations](docs/OPERATIONS.md) | Commands, side effects, flags and integration gates |
| [Repositories and runs](docs/RUNS.md) | Connect targets, choose concurrent agents, save drafts and explicitly launch through the browser or CLI |
| [Dependency and parallelism analysis](docs/DEPENDENCIES.md) | Detect prerequisite chains, inferred resource relationships and a conservative independent frontier for 100 workers |
| [Worker supervision](docs/WORKER_SUPERVISION.md) | Observe source changes, preserve termination reasons and run frozen task acceptance checks |
| [Integration preparation pipeline](docs/INTEGRATION_PIPELINE.md) | Prepare independent reviewed candidates in bounded local lanes while other PRs wait for CI |
| [Next pilot](docs/NEXT_PILOT.md) | Small completion experiment, scoped context, CI scheduling and matched AFK gates |
| [Operational feedback](docs/OPERATIONS_SAFETY.md) | Failure brakes, durable action reservations, CI diagnosis and conservative scaling advice |
| [Deployment preflight](docs/DEPLOYMENT_PREFLIGHT.md) | Read-only source, ledger, GitHub and resource checks before a separately authorized deployment |
| [Spend reporting](docs/BILLING.md) | Provider cost evidence, source freshness and local reservations without invented account balances |
| [Budget migration](docs/BUDGET_MIGRATION.md) | Read-only proposals and separately authorized cumulative increases preserving every allocation |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Setup, budget, worker and integration failures |
| [Presentation](docs/contest/PRESENTATION.md) | Seven-minute outline, real pilot results and remaining contest requirements |
| [Target assessment](docs/contest/TARGET-ASSESSMENT.md) | Why Afterimage suits the next collision pilot and what the final benchmark target needs |
| [Benchmark and evidence](docs/contest/BENCHMARK.md) | Fair AFK comparison; [reporting checklist and template](docs/contest/EVIDENCE.md) |

The interface, guides and example use English. The standalone extraction and
tutorial are tested offline; their documentation does not imply a new paid
cohort ran.

## What lives where

| Location | Responsibility |
| --- | --- |
| This repository | Runner, budget gateway, state, integration, tests and operational docs |
| Target repository | Application code, root `swarm.toml`, tasks, instructions and CI |
| Target checkout's ignored `.swarm/` | Durable budget ledger, claims, patches and private logs |
| Modal | Temporary worker VMs and trusted model gateway |
| GitHub | Issues, source history, reviewed PRs and CI |

Afterimage is the first client project. Its source, tasks and private run evidence
are not copied into this controller. Each target uses a separate controller
process. Git worktrees share their original checkout's ledger and dispatcher lock.

## Try the local example

Use Python 3.12 with venv support and Git. GitHub CLI and Modal authentication
are needed for operations that access those services.

```sh
git clone https://github.com/slgarza/swarm-control.git
cd swarm-control
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python examples/create_demo.py ../swarm-demo
cd ../swarm-demo
python3 -m unittest discover -s tests -v
swarm-control plan D01 D02 --project swarm.toml --workers 2 --synchronized --base HEAD
```

Expected: two baseline tests pass and the plan shows two ready tasks with
`live_execution_enabled: false`. No agent has written code at this stage.
The helper creates a separate local Git repository and refuses an existing folder.
`plan` reads local metadata and creates no cloud resources. It reports potential
concurrency, not live readiness. See [Quickstart](docs/QUICKSTART.md) for details.

Use `swarm-control preflight D01 D02 --project swarm.toml --workers 2 --synchronized --base HEAD`
for a read-only preparation report that also inspects existing local claims,
admission allowance and cached spend. Add `--online` only to permit GitHub GETs.
Exit zero covers the checked source-preparation scope; `launch_ready` remains
false and budget/provider/authorization blockers remain explicit. See the
[deployment preflight guide](docs/DEPLOYMENT_PREFLIGHT.md).

Connect another repository using [project configuration](docs/PROJECTS.md).
Read [operations](docs/OPERATIONS.md) before any paid run. Semantic review remains
explicit. The local dashboard can prepare run drafts, explicitly request execution
and inspect historical evidence. Backlog preparation, gateway deployment and
reviewed integration still use the controller workflow.

## Capacity and remaining proof

Validate an independent frontier before planning a 100-worker cohort:

```sh
swarm-control analyze --project /path/to/target/swarm.toml --manifest /path/to/target/swarm/cohort.json --workers 100 --require-parallel 100
```

The explicit cohort manifest contains a `taskIds` array. Without task IDs or a
manifest, analysis inspects the whole backlog (up to 10,000 definitions) and
suggests at most 100 ready tasks for a separate reviewed run. A chain of 100 tasks
has only one initially ready slot; a graph with 100 independent leaves can fill
100 slots. The report explains cycles, prerequisites, resource/import review
hints, theoretical waves and scan limits. Static preparation does not establish
semantic independence, provider capacity or successful live work.

**Check plan** in the browser shows dependency-frontier and context/acceptance
readiness. Selecting 100 agents automatically proposes a minimum frontier of 100;
the saved requirement is rechecked before resource admission. Workers preserve
observed deadline/source-progress stop reasons and recovery candidates. Optional
task acceptance checks complement the baseline suite, and independent local
integration preparation can proceed in separate lanes. See the guides above for
the exact contracts and remaining review gates.

The runner accepts cohorts of up to 100 workers when the target explicitly sets
that capacity and has an authorized allowance. Defaults remain ten workers and
disabled live execution. Dispatch is bounded and checkpoints each completed task.
`--allow-overlap` permits declared shared-file work in isolated checkouts; the
integrator applies patches on the current base, uses three-way application when
needed, and validates the combined tree. Actual conflicts produce a durable repair
receipt; they still need an assigned repair and review.

Targets can optionally configure paired token/request pacing in the trusted
gateway. It uses verified input counts plus maximum output and a shared bounded
queue; it does not verify provider quota or refund retained money. Coding sessions
default to 300 seconds; `--timeout 1800` is an explicit opt-in with longer bounded
gateway expiry. These settings require reviewed deployment and allowance, and
cannot establish useful live overlap by themselves.

Scheduling uses local SQLite. Dispatcher, candidate and short shared integration
locks cover Git worktrees sharing one checkout, not other clones or hosts.
Profiles can use frozen [source bundles](docs/PRIVATE_REPOSITORIES.md) for private
targets without forwarding GitHub credentials to coding workers. Review,
publication and merge remain explicit; the preparation pipeline does not merge PRs.

Offline tests cover the new capacity, backpressure and collision paths. Real provider
capacity, useful 100-agent overlap and efficiency remain unproven. The next steps
are a second real target, measured 25 → 50 → 100 cohorts and the strong AFK comparison
with separately authorized budgets. See [scaling](docs/SCALING.md).

Before discussing budget, run `swarm-control budget status --project /path/to/target/swarm.toml`.
It distinguishes retained local admission reservations from provider-reported
spend and unknown account credit. Explicit billing synchronization can read the
OpenAI Costs API; imported observations keep their source and scope. See
[spend reporting](docs/BILLING.md). A spent local allocation is not a provider bill.

## Validation

From the controller checkout with its virtual environment active and Node.js 24
available for the dependency-free browser routing contract tests:

```sh
python -m unittest discover -s tests -p 'test_*.py'
ruff check --isolated swarm_control tests examples
python -m compileall -q swarm_control examples
```

Tests cover worker recovery, budget accounting, integration gates, repository
isolation and configuration. CI also creates the documented demo in a temporary
directory, runs its baseline and planner, and verifies disabled execution.
These checks make no paid model or Modal calls.

## Origin

The controller was extracted from [Afterimage commit 2217882](https://github.com/slgarza/Afterimage/commit/2217882c3d5597bbb92a4ae28bc8a2e02ccedb0b).
Afterimage remains a separate client project with its original completed tasks,
private runtime evidence and cumulative budget. The tutorial contains original,
small Python examples rather than copied application code.
