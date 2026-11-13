# Troubleshooting and recovery

Start with the exact error and the affected stage. Worker/integration logs and
accounting stay in the Git common checkout's ignored `.swarm/` directory. Browser
setup, planning and repository-preparation records instead accompany the private
workspace registry. Keep that registry and its companion folders outside Git.
Never change recorded hashes, validation
states, budget totals or original logs to manufacture a successful run.

## Installation and target setup

| Symptom | Meaning and action |
| --- | --- |
| `ensurepip is not available` when creating a venv | Install the matching Python venv support for your OS. The controller requires Python 3.12 or newer; the documented/tested setup uses 3.12. |
| `swarm-control: command not found` | Activate the controller's `.venv`, or invoke its absolute `.venv/bin/swarm-control` path. The target need not contain the controller source. |
| `project config must live at the target checkout root` | Move the config to that project's Git root. Use the demo creator rather than planning inside the nested template directory. |
| `origin does not match project.repository` | Check both the configured owner/repo and `git remote get-url origin`. The target and origin must refer to the same repository. |
| Unknown project setting | Check field spelling and the [schema](PROJECTS.md). Unknown fields are rejected rather than silently ignored. |
| Missing base ref or worker checkout failure | Fetch the configured tracking branch when it is absent or stale. Direct `github` transport needs the selected commit on GitHub. `bundle` can deliver an exact local commit, but issue/dependency and later GitHub integration gates still apply. Review the base before another live attempt. |
| Public clone fails for a private target | Use reviewed `runtime.source_transport = "bundle"` with controller access to the target. Browser-created profiles already choose bundle transport; direct `github` worker cloning supports public source. See [private source delivery](PRIVATE_REPOSITORIES.md). |
| Bundle rejects shallow history or size | Bundle creation requires complete local history and bounded private source. Review the error and source limits; obtaining full history is a separate authorized Git operation. Do not copy credentials into the worker or a clone URL as a workaround. |
| GUI clone succeeds but integration Git fetch/push cannot authenticate | Clone/preparation publication use a per-command helper; integration uses normal controller Git authentication. Authenticate the intended CLI account and configure its helper, for example `gh auth setup-git --hostname github.com`. A GUI token or `GH_TOKEN` by itself does not install that helper. Keep tokens out of URLs and worker files. |

CLI `plan` and **New run → Check plan** inspect existing task metadata locally.
They are different from **Plan work → Generate work plan**, which makes an
explicitly authorized, budgeted model request. Local inspection does not prove
access to Modal, API model quota,
remote issue readiness, or available cumulative budget.

## Browser setup and AI planning

| Symptom or code | Meaning and action |
| --- | --- |
| No project is available for configuration | Open **Projects → Add repository**, inspect or clone the target, then connect its profile. A new checkout can use **Plan work for this repository** or **Configure manually**. Historical evidence alone does not supply a usable profile. |
| `planning_key_required` | Save your OpenAI key under **Settings → Your personal local .env**. Planning uses the local saved/session key, not a ChatGPT login or the gateway's unreadable Modal Secret. Saving it makes no model request. |
| `planning_github_required` or `planning_github_token_required` | Save or supply your own captured GitHub token and explicitly verify it in **Settings**. Token-less ambient GitHub CLI login cannot seal a plan's issue-creating account. Verification does not prove every repository permission. |
| `planning_budget_insufficient` | Review the conservative call bound and explicitly authorize a sufficient per-request allowance. This planning reservation is separate from the worker ledger and is not provider credit or an invoice. |
| `planning_unsafe_source` | A repository identity contains credential-looking text. Select a safe identity; never put keys in origins or filenames. Credential-bearing filenames are excluded from outbound context. |
| `planning_invalid_response` or `planning_call_unknown` | Inspect **Previous planning requests** or **Inspect retained plan**. Rejected/incomplete or uncertain calls retain their reservation. They do not create target files or issues, and are not automatically retried. |
| `planning_source_changed` or `planning_accounts_changed` | The selected code or account differs from the reviewed binding. Inspect the retained result and current source/account before a new explicit review; do not edit saved hashes. |
| `planning_validation_blocked` | Review dependency errors and the conservative frontier. A chain of 100 tasks cannot satisfy 100 requested independent agents. Change the proposed work or requested concurrency; do not pad the backlog. |
| `planning_apply_held` or `planning_attempt_held` | Inspect the retained attempt and known issue/task progress. Some remote writes may already exist. Application is not replayed automatically; preserve the original evidence for manual recovery. |
| `setup_evidence_unavailable` or an interrupted setup job | The result could not be durably confirmed. A retained planning ID can still be inspected. Do not repeat issue creation, deployment or model generation merely because a browser result is missing. |
| Preparation preview changed or an old push is pending | Review **Project settings → Checks & publish** and the recorded actual commit. Changed files need another preview. A push retry can reuse the recorded commit; it must not overwrite unrelated work or force-push. |

The default `workspace.json` registry has companion `workspace.setup-jobs/`,
`workspace.planning/`, `workspace.json.connections/` and
`repository-preparation/` storage. Preserve these private records. Deleting a
planning journal or binding key is not a budget reset or an authorized retry.

## Accounts and deployment

| Symptom | Meaning and action |
| --- | --- |
| Authentication fails in Modal | Verify the intended workspace under **Settings**, or use `modal setup` and `modal profile current` for CLI authentication. GUI session/saved token pairs can override CLI profiles; inspect which connection the operation uses. |
| Secret or `CODEX_API_KEY` missing | Create the configured secret in that workspace/environment, with that exact variable name. An `OPENAI_API_KEY` variable alone is not what this gateway reads. |
| Model access/quota error | Verify the key's project, access to the pinned model and API quota. Changing a model flag alone is unsupported because pricing and request policy are pinned. |
| Gateway origin rejected | Supply its HTTPS `*.modal.run` origin without credentials, query, port or `/v1` suffix. The runner adds the API path. |
| Gateway lookup finds the wrong application | Check active Modal credentials, `runtime.environment` and app identifiers. Names resolve inside the authenticated workspace. |

Do not put a real key in task JSON, a Git remote, a shell command copied into an
issue, or a public report. The [gateway reference](GATEWAY.md) describes the
credential boundary and specific HTTP error codes.

## Admission and budget

| Error or outcome | Action |
| --- | --- |
| `live execution is disabled` | Expected for the demo and migrated Afterimage profile. A new owner-authorized paid run needs an explicitly enabled, funded policy. |
| Task is merged, already recorded, or its issue is closed | Inspect the completed attempt. Choose genuinely new work; do not reset state to redispatch it. |
| Overlapping tasks | Revisit dependencies and ownership, or explicitly use `plan`/`run --allow-overlap` for reviewed shared-file work in isolated checkouts. Scope enforcement still applies. |
| Missing/unmerged dependency | Record the dependency's real merged PR and select a base containing its merge. |
| Project permits at most N live agents | `budget.max_live_agents` defaults to ten and supports up to 100. A larger authorized stage also needs verified gateway/provider capacity and sufficient remaining allowance. A plan grants neither. |
| Synchronized cohort exceeds gateway capacity | Verify upstream quota, configure `runtime.gateway_concurrency`, then redeploy. Local capacity is checked before allocation and authenticated admission capacity before sandbox launch. Missing deployed capacity is rejected above 20 workers; admission mismatches stop the run with allocation retained. Verify the HTTP URL belongs to that gateway app. |
| Local admission model/infrastructure allowance exhausted | Prior allocations remain reserved, including failed attempts. This is not proof that provider credit is empty or that reservations were billed. Use [budget status](BILLING.md) to inspect the distinct sources; new local allowance still needs an actual authorized migration. |
| Stored budget policy differs | Editing TOML does not migrate the ledger. Keep the old limits and disabled execution, then use the [reviewed budget migration](BUDGET_MIGRATION.md). Budget inspection and migration planning are read-only; applying an increase needs actual authorization and preserves every allocation. |
| Legacy pilot allowance must be preserved | The imported Afterimage ledger retains USD18/USD7 and prior spend. Do not create a replacement ledger to evade that ceiling. |
| PR/check pagination incomplete, changed or limit exceeded | Pagination is bounded to 100 pages. Inspect the remote list and retry read-only inspection after it stabilizes; do not ignore unreturned PRs or checks. |
| `gateway_busy` | Configured provider slots remained full for 15 seconds; the rejected request made no provider call or reservation. Inspect `runtime.gateway_concurrency` and actual API quota; a synchronized 100-worker stage needs explicit adequate capacity. Existing Codex retry limits still apply. |

The `--retry` flag is only for a reviewed failed task with remaining allowance.
It does not override a merged/running claim, replenish funds or restart an entire
cohort automatically. Never delete `.swarm/`, steal locks, or use another clone
as a way to bypass the original authorization.

## Worker failures

Inspect `report.json` first, then the relevant `install.log`, `checks.log`, agent
events and recovered patch. An intermediate nonzero shell command can be a normal
search miss; final status and acceptance checks determine whether work validated.

- `failed` or `rejected`: the worker did not meet the acceptance boundary.
- `needs_repair`: inspect the recorded reason, including patch/digest disagreement.
- `needs_recovery`: evidence retrieval was incomplete; do not treat it as delivered.
- Termination failure: use Modal's dashboard to identify that exact sandbox and
  terminate it; keep the failure evidence and verify cleanup afterward.
- Gateway stop failure: stop the recorded gateway run using the authenticated
  administrative API. Do not publish issued tokens or full administrative payloads.

Recovered partial work can inform a repair, but it is not automatically eligible
for publishing. Preserve original artifacts and attribute any coordinator repair
separately. There is no general automated repair command in this release.
An explicitly qualified recovery candidate can undergo separate reviewed local
preparation through the [integration pipeline](INTEGRATION_PIPELINE.md); its
original failed worker outcome remains unchanged.

## Integration failures

A changed base requires a fresh candidate with full combined validation. Preserve
the old worktree and artifact directory; use a separate attempt and keep provenance.
There is currently no automatic rebase/retry manager. Do not remove fields from
`integration.json` to trick `prepare` into overwriting a reviewed attempt.

For an opted-in shared-ownership candidate, prepare tries three-way application
after strict application fails. `needs_repair` with `three_way_apply_failed`
preserves conflicting paths and the apply log; the dedicated candidate worktree
is reset and cleaned after the failed application. Assign a separate repair and
preserve the original patch/receipt. `candidate_validation_failed` means combined
checks or candidate integrity failed, even if the patch applied cleanly.

If publish/merge requires `--reviewed-applied-patch-sha`, review the saved
`integration.patch` on its recorded base and supply its `applied_patch_sha256` in
addition to the original worker hash. Editing the manifest or hashes does not
constitute review. `another integrator is active` means a local command holds
`integration.lock`; inspect that process rather than deleting a lock file.

If a candidate tree or patch changes after validation, review and test the new
candidate before publishing. If GitHub CI is missing, pending, failed or attached
to another SHA, resolve that state rather than bypassing the merge gate. If branch
protection or repository permissions reject a merge, keep the PR for an authorized
reviewer; the integrator does not use administrator bypasses.

After an ambiguous merge API response, inspect the actual PR on GitHub. The merge
command can reconcile a confirmed merge with the local ledger. Do not create a
second PR for the same already-merged work.

## Safe issue reporting

Report controller version/commit, stage, task ID, error category, relevant config
field names, and sanitized reproduction steps. Include safe hashes or PR links
when useful. Keep API keys, worker tokens, credential hashes, private prompts and
raw customer code out of public bug reports.
