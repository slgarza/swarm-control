# Swarm Control

A controller for coding agents working on independent GitHub repositories.
It assigns tasks, starts isolated Codex workers in Modal, retrieves patches,
enforces budgets and ownership, and integrates reviewed changes through passing
GitHub Actions. OpenAI supplies the model; Modal supplies temporary machines.
The coordinator currently runs on one host.

Extracted from [Afterimage commit 2217882](https://github.com/slgarza/Afterimage/commit/2217882c3d5597bbb92a4ae28bc8a2e02ccedb0b).
Its [first pilot](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot10-2026-11-10.md)
delivered ten reviewed, merged PRs, including one coordinator code repair.
Extraction is tested offline; no new paid cohort is implied.

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

## Install and inspect

Use Python 3.12 with venv support and Git. GitHub CLI and Modal authentication
are needed for operations that access those services.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/swarm-control --help
.venv/bin/swarm-control plan T01 --project ../Afterimage/swarm.toml --base HEAD
```

`plan` reads local metadata and creates no cloud resources. It reports task status
and potential concurrency, not live readiness. Afterimage preserves its completed
tasks and cumulative budget with live execution disabled.

Connect another repository using [project configuration](docs/PROJECTS.md).
Read [operations](docs/OPERATIONS.md) before any paid run. Semantic review remains
explicit; no autonomous backlog generator or dashboard is implemented yet.

## Current limits

Live execution supports at most ten workers. Plans may describe 100 without
launching them. Ownership is disjoint, scheduling uses local SQLite, and one
integrator validates the combined changes. Other clones/machines are outside that
lock. Private target cloning needs a scoped authentication adapter; the current
clone path supports public GitHub targets. This controller repository may itself
be private without changing target behavior.

The next stages are a second real project, improved integration throughput,
durable distributed scheduling and observable conflict repair, then 25 → 50 → 100
with new authorized budgets. See [scaling](docs/SCALING.md). No AFK efficiency
comparison or qualifying 100-agent submission has run.

## Validation

```sh
python -m unittest discover -s tests -p 'test_*.py'
ruff check --isolated swarm_control tests
python -m compileall -q swarm_control
```

The extraction retains 70 pilot regressions and adds tests for repository
isolation, command/check configuration, disabled live execution and preserved
budget accounting. CI runs these without paid model or Modal calls.
