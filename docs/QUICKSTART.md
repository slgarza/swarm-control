# Quickstart: Easy Run and a local example

This example creates a tiny independent Python repository with two useful changes
to implement. You can run its baseline tests and inspect a two-worker plan without
Modal login, an OpenAI key, a GitHub account, or a cloud run. Downloading the code
and installing dependencies still require ordinary internet access.

**This is a planning demonstration. It does not run, simulate, or count AI agents.**
For actual coding workers, continue with the [paid two-agent walkthrough](TWO_AGENT_WALKTHROUGH.md).

## 1. Install the controller

Use Linux, macOS or Windows through WSL with Git and Python 3.12 or newer,
including its venv support. Open a terminal; the commands below use a POSIX shell.
On Windows, run them in the WSL terminal rather than PowerShell.

```sh
git clone https://github.com/slgarza/swarm-control.git
cd swarm-control
./easy-run
```

Easy Run creates `.venv`, installs the controller on first use and opens
[http://127.0.0.1:8787](http://127.0.0.1:8787). Keep that terminal open while using
the app. Opening the app starts a local server, with no cloud workers.

If the browser does not open, copy the URL printed in the terminal. Use
`./easy-run --no-open` to skip opening it, or `./easy-run --port 8788` if another
program is using port 8787. To stop the server, press Ctrl+C in its terminal.
Later, run `./easy-run` from this same checkout; it reuses the environment and
saved workspace. To connect a prepared existing target immediately, use
`./easy-run --project /absolute/path/to/target`.

On Debian/Ubuntu, a missing `ensurepip` error usually means the matching Python
venv package is absent. Install that prerequisite using your system's package
manager, then run Easy Run again. See [troubleshooting](TROUBLESHOOTING.md).

For the separate local example below, stop the server with Ctrl+C and activate
the environment it created:

```sh
source .venv/bin/activate
swarm-control --help
```

The editable install keeps the CLI attached to this checkout. Keep the virtual
environment active when changing directories in the following steps.

## 2. Create a separate target repository

From the controller checkout:

```sh
python examples/create_demo.py ../swarm-demo
cd ../swarm-demo
```

The helper copies the tutorial template, initializes a local Git repository,
creates a baseline commit with a local demo identity, and records a placeholder
`origin`. It does not contact that origin, create a GitHub repository, or start
cloud resources. It refuses to overwrite an existing destination; use a different
folder name if `../swarm-demo` already exists.

Do not run the planner directly inside `examples/demo-target`: that directory
belongs to the controller's repository. The helper makes the separate Git root
required by the project configuration.

## 3. Inspect the two tasks

| Task | Current behavior | Requested behavior |
| --- | --- | --- |
| D01 | `normalize_name` preserves its input | Trim and collapse whitespace while preserving case and Unicode |
| D02 | `clamp` handles ordered integer bounds | Also reject inverted bounds with `ValueError` |

For example, D01 should turn `"  Ana   López "` into `"Ana López"`. D02 should
reject `clamp(3, 10, 0)` while retaining normal inclusive clamping.

The source files and tests have disjoint ownership. Each task file in
`swarm/tasks/` states its exact acceptance criteria. Issue numbers 1 and 2 are
placeholders for local planning, not claims that those remote issues exist.

```text
swarm-demo/
  README.md
  AGENTS.md
  swarm.toml
  demo/text.py
  demo/numbers.py
  tests/test_text.py
  tests/test_numbers.py
  swarm/tasks/D01.json
  swarm/tasks/D02.json
  .github/workflows/ci.yml
```

## 4. Check the baseline

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q demo
```

Expected: **2 tests pass**. They cover existing behavior, not the new features.
The tasks require adding regression tests along with their implementations.
Both independent worker copies can therefore start from a passing baseline.

## 5. Try the browser workflow

From the demo folder, open the controller and connect this target:

```sh
../swarm-control/easy-run --project "$PWD"
```

In the browser:

1. Choose **swarm-demo** in **Repository**. The connection uses the demo's local
   `swarm.toml`; its placeholder origin does not need to exist for planning.
2. Click **New run** and select **D01** and **D02**.
3. Set **Concurrent agents** to **2**, then click **Check plan**.
4. Review the plan, then click **Save draft**. Open the saved run in **Run history**.

Expected: the draft contains two tasks and two agents, and execution remains
disabled. Saving does not change that policy, contact a model or create a VM.
The draft's **Run agents** control is unavailable until the real-execution setup
is complete. Use Ctrl+C in the server terminal to stop the demo dashboard.

For another prepared repository, click **Connect repository** and enter its local
folder or `swarm.toml` path. A GitHub URL and a ZIP folder without Git metadata
are not ready targets. A new target needs its own reviewed profile, task files,
instructions and checks; follow [project configuration](PROJECTS.md).

## 6. Inspect the same kind of plan from the CLI

After stopping the server, with the environment active in the demo folder:

```sh
swarm-control plan D01 D02 \
  --project swarm.toml \
  --workers 2 --synchronized --base HEAD
```

The output contains these fields; the commit hash varies:

```json
{
  "repository": "YOUR_GITHUB_USER/swarm-demo",
  "tasks": ["D01", "D02"],
  "task_states": {"D01": "ready", "D02": "ready"},
  "live_execution_enabled": false,
  "requested_workers": 2,
  "maximum_active_agents": 2,
  "enough_tasks_for_100": false,
  "mode": "plan"
}
```

`maximum_active_agents` describes potential concurrency for that task list.
It is not evidence that agents ran. The planner does not query remote issue
readiness or allocate budget. `--base HEAD` lets this local example use its own
baseline without needing a fetched remote branch.

The demo's policy is deliberately disabled:

```toml
[budget]
live_enabled = false
max_live_agents = 2
model_limit_cents = 0
infra_limit_cents = 0
```

You now have a working controller installation, a separate target and a readable
plan. No agent has written code yet. Continue with [real execution](TWO_AGENT_WALKTHROUGH.md)
only when you want to configure your own services and authorize the cost.
