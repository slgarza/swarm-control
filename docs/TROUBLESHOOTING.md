# Troubleshooting and recovery

Start with the exact error and the affected stage. Raw logs and artifacts stay in
the target's ignored `.swarm/` directory. Never change recorded hashes, validation
states, budget totals or original logs to manufacture a successful run.

## Installation and target setup

| Symptom | Meaning and action |
| --- | --- |
| `ensurepip is not available` when creating a venv | Install the matching Python venv support for your OS. The controller requires Python 3.12 or newer; the documented/tested setup uses 3.12. |
| `swarm-control: command not found` | Activate the controller's `.venv`, or invoke its absolute `.venv/bin/swarm-control` path. The target need not contain the controller source. |
| `project config must live at the target checkout root` | Move the config to that project's Git root. Use the demo creator rather than planning inside the nested template directory. |
| `origin does not match project.repository` | Check both the configured owner/repo and `git remote get-url origin`. The target and origin must refer to the same repository. |
| Unknown project setting | Check field spelling and the [schema](PROJECTS.md). Unknown fields are rejected rather than silently ignored. |
| Missing base ref or worker checkout failure | Fetch the configured base and push any intended local commit first. Workers cannot clone an unpushed commit. |
| Public clone fails for a private target | Private-target authentication is not implemented. Use an authorized public target for this release or implement/test the scoped clone adapter first. |

`plan` is local inspection. It does not prove access to Modal, API model quota,
remote issue readiness, or available cumulative budget.

## Accounts and deployment

| Symptom | Meaning and action |
| --- | --- |
| Authentication fails in Modal | Run `modal setup` with your own account; inspect `modal profile current` and the workspace/environment in the dashboard. |
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
| Overlapping tasks | Revisit dependencies and ownership. The current release cannot dispatch same-file work together. |
| Missing/unmerged dependency | Record the dependency's real merged PR and select a base containing its merge. |
| At most ten live agents | This is an implementation limit. A successful 100-worker plan does not lift it. |
| Model/infrastructure allowance exhausted | Prior allocations remain reserved. Reconcile the run and seek a reviewed budget change; retries still need remaining allowance. |
| Stored budget policy differs | Editing the TOML limits does not migrate an existing ledger. No budget-migration CLI is supplied yet. Preserve state until a migration is explicitly designed and reviewed. |
| Legacy pilot allowance must be preserved | The imported Afterimage ledger retains USD18/USD7 and prior spend. Do not create a replacement ledger to evade that ceiling. |
| PR pagination requires review | This release stops at its conservative pagination boundary; it does not claim to have inspected unreturned PRs. |

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

## Integration failures

A changed base requires a fresh candidate with full combined validation. Preserve
the old worktree and artifact directory; use a separate attempt and keep provenance.
There is currently no automatic rebase/retry manager. Do not remove fields from
`integration.json` to trick `prepare` into overwriting a reviewed attempt.

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
