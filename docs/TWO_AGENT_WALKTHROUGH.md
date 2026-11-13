# Run two real coding agents

This walkthrough continues the [local quickstart](QUICKSTART.md). It turns D01 and
D02 into real coding work in two Modal VMs, then integrates the resulting PRs.
These are small teaching tasks, not the competition workload or proof of scale.

**This part uses paid services.** You provide your own accounts, permissions and
approved allowance. Nothing in this document authorizes spending from someone
else's account. The checked-in example remains disabled; this walkthrough was
validated through the local example and offline controller tests, not a new paid
cohort. The original ten-worker run is documented separately.
This page uses the CLI path. For the same account, preparation and execution
boundaries in the browser, follow [browser setup](SETUP.md).

## 1. Prepare your accounts and target

Requirements: the installed controller with its virtual environment active, Git,
GitHub CLI, a public target repository you can push to, a Modal workspace, and an
OpenAI API project with access to `gpt-6.1-sol` and sufficient quota. A ChatGPT
session is not the credential used by this controller.
The tutorial deliberately uses direct public GitHub source transport. Private
repositories use the separately reviewed [bundle transport](PRIVATE_REPOSITORIES.md);
they still require actual GitHub issues and the ordinary PR/CI gates.

```sh
gh auth login --hostname github.com
gh auth setup-git --hostname github.com
modal setup
modal profile current
```

Confirm the workspace in Modal's dashboard, especially if you have multiple
profiles. The selected workspace and API key determine whose resources are used.
The controller does not connect to the author's workspace by downloading the code.
The Git helper is needed for the integrator's native fetch/push operations.
Saving a GUI GitHub token alone does not install it; browser clone/preparation
publication use their own per-command helper. Keep the controller CLI account,
target permissions and any saved/session GUI token aligned. Never put a token
in a remote URL or copy it into a worker.

Create an empty public GitHub repository, such as `YOUR_USER/swarm-demo`. In the
local demo folder, set `project.repository` in `swarm.toml` to that exact owner/name
and update its Git remote. The following values are placeholders to replace:

```sh
cd ../swarm-demo
SWARM_DEMO_REPO='YOUR_USER/swarm-demo'
git remote set-url origin "https://github.com/$SWARM_DEMO_REPO.git"
git config user.name 'Your Name'
git config user.email 'YOUR_COMMIT_EMAIL'
git add swarm.toml
git commit -m "Configure the tutorial target repository"
git push -u origin main
```

The helper's initial commit uses a local demo identity; the last two configuration
commands set your identity for subsequent commits. Wait for the repository's
`checks` GitHub Actions job to pass. No model key is needed by that CI workflow.

## 2. Create actual work items

Create two issues in your repository using the descriptions and acceptance
criteria in `swarm/tasks/D01.json` and `D02.json`. Record the actual issue numbers
in those JSON files. Do not assume they are 1 and 2 merely because the template
used those placeholders. Both issues must be open, both tasks must be `ready`,
and their paths must remain disjoint. The existing baseline should still pass.

A real project follows the same pattern with useful backlog items, architecture
instructions and its own commands. It must not reuse Afterimage's completed tasks
or fabricate trivial tickets to inflate a benchmark.

## 3. Store your model credential

In your Modal workspace's Secrets panel, create `swarm-demo-openai` in environment
`main`. Add a variable named **`CODEX_API_KEY`** containing your own OpenAI API key.
This variable name is required by the current gateway. The profile's
`runtime.openai_secret` refers to the secret name, not the key value.

Use Modal's Secrets page, or **Project settings → Execution allowance → Create
Modal Secret**, so the key does not enter project files or a
copyable shell command. The gateway receives the real key; workers receive only
expiring per-task gateway credentials.

Primary references: [Modal workspaces](https://modal.com/docs/guide/workspaces),
[Modal Secrets](https://modal.com/docs/guide/secrets),
[OpenAI API keys](https://developers.openai.com/api/docs/guides/api-keys),
[configured model](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

## 4. Set the authorized policy before the first paid operation

For a **fresh tutorial ledger**, this example allocates USD5 to model admission
and USD5 as infrastructure reserve. These are example settings to adopt only if
you authorize that allowance, not a prediction or guarantee of the provider bill:

```toml
[budget]
live_enabled = true
max_live_agents = 2
model_limit_cents = 500
infra_limit_cents = 500
```

One probe reserves 50 model cents and 135 infrastructure cents. The two-agent run
below reserves 200 model cents and 160 infrastructure cents. Those allocations
fit this policy and remain recorded after completion or failure. Deployment,
image building and other provider costs still require billing reconciliation;
this is not an account-wide spending cap.

If this target already has a ledger, do not change its limits, delete it or create
a new clone to obtain fresh allowance. Follow [budget troubleshooting](TROUBLESHOOTING.md)
and use the [explicit budget-migration workflow](BUDGET_MIGRATION.md) only after
review of a proposed increase and the owner's actual authorization. It preserves
all reservations; it does not start workers or enable execution.

Commit and push the task IDs and settings, then confirm CI is green:

```sh
git add swarm.toml swarm/tasks
git commit -m "Prepare reviewed two-agent tutorial tasks and policy"
git push origin main
git fetch origin main:refs/remotes/origin/main
swarm-control plan D01 D02 --project swarm.toml --workers 2 --synchronized
```

The template omits `source_transport`, so it uses the parser's `github` default.
Workers in this tutorial clone the selected remote commit; unpushed code or
instructions are not available through that transport. Browser-created profiles
instead default to `bundle`, which can deliver an exact local commit.
Local CLI planning shows the requested shape; paid admission separately
checks issue readiness, dependencies, ownership and remaining allowance.

## 5. Deploy your gateway and verify compatibility

These operations can incur costs. Run them once for this prepared cohort:

```sh
swarm-control deploy-gateway --project swarm.toml
python -c 'import modal; print(modal.Function.from_name("swarm-demo-gateway", "gateway", environment_name="main").get_web_url())'
```

Copy the returned origin into the variable below. Replace the placeholder with
your own deployed URL, without adding `/v1`:

```sh
SWARM_DEMO_GATEWAY='https://REPLACE-WITH-YOUR-GATEWAY.modal.run'
swarm-control probe --project swarm.toml --gateway-url "$SWARM_DEMO_GATEWAY"
```

A passing probe reports `probe_passed`, a completed command, a clean workspace and
confirmed cleanup. It performs a real model/tool round trip. If it fails, inspect
the saved evidence and remaining allowance before retrying; repeated probes are
not free and do not release their allocations automatically.

## 6. Launch two independent workers

```sh
swarm-control run D01 D02 \
  --project swarm.toml \
  --workers 2 --synchronized \
  --model gpt-6.1-sol --model-budget 2.00 \
  --gateway-url "$SWARM_DEMO_GATEWAY"
```

Both checkouts start from the same base. Each agent owns one task, calls the model,
edits its allowed files and runs checks. The controller recovers artifacts and
terminates workers. The command prints an evidence directory under
`.swarm/modal-runs/batch-...`.

Inspect each task's `report.json`, `changes.patch` and `checks.log`. A good outcome
has `status: validated`, zero final check errors, a verified patch and confirmed
sandbox termination. It still has not delivered a merged PR. A failure or missing
artifact must not be counted as completed work.

## 7. Review and integrate one delivery at a time

Set the batch path printed by the controller; this is another placeholder:

```sh
SWARM_DEMO_BATCH='/absolute/path/to/swarm-demo/.swarm/modal-runs/batch-REPLACE'
swarm-control integrate plan "$SWARM_DEMO_BATCH/D01" --project swarm.toml
less "$SWARM_DEMO_BATCH/D01/changes.patch"
```

Read the complete implementation and tests against D01's acceptance criteria.
Then prepare it on the latest target branch, including all earlier merged work:

```sh
swarm-control integrate prepare "$SWARM_DEMO_BATCH/D01" --project swarm.toml
```

Review the candidate and validation logs. Copy the `patch_sha256` from the verified
plan **after** review; the hash acknowledges a specific patch, not a substitute
for checking its behavior:

```sh
SWARM_DEMO_REVIEWED_SHA='REPLACE-WITH-THE-REVIEWED-SHA256'
swarm-control integrate publish "$SWARM_DEMO_BATCH/D01" \
  --project swarm.toml --reviewed-patch-sha "$SWARM_DEMO_REVIEWED_SHA"
```

Wait for the returned PR's checks. Replace `PR_NUMBER` with its actual number:

```sh
gh pr checks PR_NUMBER --repo "$SWARM_DEMO_REPO" --watch
swarm-control integrate merge "$SWARM_DEMO_BATCH/D01" \
  --project swarm.toml --reviewed-patch-sha "$SWARM_DEMO_REVIEWED_SHA"
```

Repeat this review/prepare/publish/merge sequence for **D02**, using D02's own
patch hash, after D01 has merged. This makes the second candidate include the
first delivery. Do not have another client merge concurrently. If the base,
patch or tested tree changes, preserve the original attempt and revalidate; do
not edit the saved hash or status to make it appear successful.
If a prepared base advances, use a new private copy of the original worker
artifacts, without the old `integration.json`, for the fresh preparation attempt.
Preserve the original candidate and all receipts. Publish/merge also require
review of the actual applied patch when three-way application was used; see
[integration preparation](INTEGRATION_PIPELINE.md).

## 8. Verify the actual requested behavior

After both PRs merge, update the original demo checkout and run its full suite:

```sh
git switch main
git pull --ff-only
python3 -m unittest discover -s tests -v
```

You can independently check the two acceptance examples:

```sh
python3 - <<'PY'
from demo.text import normalize_name
from demo.numbers import clamp
assert normalize_name("  Ana\t López\n") == "Ana López"
assert normalize_name(" \t\n") == ""
assert clamp(-5, -2, 3) == -2
assert clamp(8, 4, 4) == 4
try:
    clamp(3, 10, 0)
except ValueError:
    pass
else:
    raise AssertionError("D02 did not reject inverted bounds")
print("Both requested behaviors verified")
PY
```

These assertions should fail on the untouched baseline and pass only after the
requested behaviors are implemented. Record each task's `status: merged` and its
actual `pullRequest` number in a separate bookkeeping change; exclude bookkeeping
from the two feature deliveries. The ledger and closed issues also block reuse.

## 9. Confirm cleanup and record the result

The runner normally terminates worker sandboxes and stops the gateway run. Check
those fields in the reports and verify that this cohort has no remaining workers
in Modal. The deployed gateway app is a separate resource. If no other authorized
work needs this demo app, identify and stop **that app only**:

```sh
modal app list
modal app stop YOUR_DEMO_GATEWAY_APP_ID --yes
```

Retain private evidence and budget records. Record PR links, checks, elapsed time,
repairs and provider billing. This tutorial is complete when both useful changes
are merged green and cleanup is confirmed. It proves a small workflow, not the
100-agent contest requirement or superiority over AFK.
