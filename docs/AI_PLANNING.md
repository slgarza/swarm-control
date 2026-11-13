# Reviewed AI planning

The local controller can turn a repository and a goal into a proposed backlog.
It uses the real OpenAI Responses API, then validates the returned structure,
paths, dependency graph and conservative independent frontier. This is an
explicit preparation operation. It does not continuously choose goals, launch
coding agents, deploy infrastructure or integrate patches.

## Inspect, generate, review, apply

1. Select a local Git checkout, or a registered project with an available profile.
   Inspection reads bounded local metadata and source. It makes no provider call,
   runs no repository code and creates no planning budget reservation or target
   files. The browser action still records its local setup job.
2. Save a personal OpenAI key in Settings and explicitly verify the intended
   GitHub account with a controller-captured token (saved, session or supplied in
   `GH_TOKEN`/`GITHUB_TOKEN`). Token-less ambient GitHub CLI login remains
   available to other workflows, but cannot seal a planner's issue-creating
   account. Ambient model keys are excluded. Inspect the selected source
   files and the conservative planning call bound.
3. Enter a goal, an upper limit of 1–100 tasks, 1–100 desired concurrent workers
   and a separate planning allowance. Explicit generation confirmation authorizes
   uploading the selected, scrubbed source context and one bounded model call.
   Desired concurrent agents should not exceed the maximum task count.
4. Review the complete plan, proposed command argument arrays, ownership,
   acceptance criteria, dependencies and reported limitations. A graph error or
   insufficient independent frontier blocks application. A serial chain is not
   padded to claim 100 independent tasks.
5. Separately confirm application of the exact reviewed plan. This can create a
   disabled, zero-budget profile, actual GitHub issues and new ready task files.
   Existing profiles, their limits and commands, and existing/frozen task files
   are preserved. Prerequisites are saved before their consumers.
   New profiles set worker and gateway capacity to the requested agent count,
   while existing profiles keep their limits and may need a separate capacity
   review before launch.
6. Check the preparation through the existing local validation action, review
   and publish its exact file binding, then use New run. Agent launch, authorized
   worker budgets, deployment, independent integration checks and GitHub review
   remain separate gates.

The graph's frontier is conservative static evidence. It does not establish
semantic independence, useful agent capacity, live concurrency or accepted
delivery. Acceptance text and model-proposed commands still need human review;
the planner does not execute them to claim that they pass.

## Source and credential boundaries

The model receives at most 64 KiB of selected excerpts from at most 40 tracked
regular files, plus bounded tracked path metadata and a goal of at most 4096
UTF-8 bytes. README/instructions, manifests, CI and source receive priority.
The inventory is limited to 10,000 paths and 128 MiB of audited regular content.
Private environment/credential files, runtime directories, symlinks, binaries
and common generated trees are excluded from excerpts. Known credentials and
credential-looking assignments are scrubbed. Untracked regular source is hashed
for local drift detection but is not uploaded as model context. Large lock files
can bind the source identity without being selected as excerpts.
Credential-bearing filenames are excluded from both the public inventory and
outbound paths. This filter preserves the existing local source binding. A
repository identity containing credential-looking text is rejected before any
model call.

Repository text is treated as untrusted data in the fixed controller prompt.
No repository instructions become model tools or controller permissions. Git
inspection disables hooks/filters and makes no source imports. Source changes,
the Git base/index/status, instruction/profile contents and account changes
invalidate admission. The review binding seals the immutable plan, analysis,
request identity and selected source/account identities.
During application, unrelated source keeps one fixed protected binding.
Only the explicitly planned output files may change, and their exact bytes must
match the trusted preparation journal. The predicted ignore-file append is
checked separately. An unrelated edit during profile creation, issue creation or
task saving is held before another issue is created.

The local key goes directly from Connections to the fixed HTTPS endpoint in an
in-process Authorization header. It never enters a subprocess environment,
worker manifest, command argument, browser storage or public job result. No
custom endpoint, proxy from the environment, redirect, tool or automatic HTTP
retry is enabled. GitHub issue creation uses the existing credentialed repository
setup service; a verified account observation does not establish repository write
permissions or available quota.

## Planning accounting and recovery

Planning uses a private journal beside the workspace registry, outside every Git
checkout. It is separate from the target's worker ledger and grants no worker
allowance. Before the HTTP request, the journal durably reserves the conservative
call bound: one input token per serialized UTF-8 byte plus 4096 overhead tokens,
at most 32,768 output tokens, the existing conservative `gpt-6.1-sol` rates
($2.50 input and $10 output per million) and 10% margin. The requested allowance
must cover this bound. Inspection advertises a worst-case goal-size bound;
generation calculates the actual request's bound.

This is a per-request planning allowance, not a shared worker balance or provider
account cap. For a registry named `workspace.json`, the journal is stored in the
sibling `workspace.planning/` directory. Keep it and its binding key: a missing
journal or key with remaining records fails closed rather than resetting spend
authorization.

The endpoint is `https://api.openai.com/v1/responses`, with strict JSON-schema
output, `store: false` and `service_tier: default`. Connect/write/pool timeouts
are 10 seconds; the read timeout is 90 seconds, with a 120-second elapsed guard
between response chunks. Decoded output is capped at 1 MiB. No retries occur.
Refused, incomplete, oversized or invalid responses never create project files
or issues. Reported token usage and a conservative price estimate are distinct
from an invoice; actual billed cost remains unknown until independently
reconciled. Reservations remain retained, including failures and process loss.

Identical completed generation requests return the existing plan. An identical
incomplete or uncertain generation is held across restarts. It cannot silently
spend again. A failed job exposes only a safe plan ID and its retained outcome,
so the operator can inspect it without resubmitting generation.

Application reserves the entire apply attempt before any profile or remote
write, then journals every issue-creation intent before making that request.
Confirmed issue IDs and saved task IDs are retained independently. A failed or
interrupted apply is held; it never replays issue creation or silently overwrites
existing tasks. Inspect GitHub and the private progress before any manual repair.
There is no automatic partial-apply recovery or issue cleanup. A completed apply
is idempotent and returns its recorded result.

## Local action contract

All modifying actions use the existing CSRF/origin-checked setup job endpoint
and explicit confirmation. There is no automatic generation on navigation.

| Action | Options | Result |
| --- | --- | --- |
| `planning.inspect` | `path` **or** `project_id` | Source/context binding, selected file metadata, readiness, model and call bound; no source excerpts or credentials |
| `planning.generate` | `path` **or** `project_id`, `goal`, `context_binding`, `max_tasks`, `workers`, `plan_budget_usd` | `plan_id`, `review_binding`, immutable `plan`, `analysis`, `usage`, retained reservation, `state: review_required`, `can_apply` |
| `planning.apply` | `plan_id`, `review_binding` | Actual task/issue IDs, registered `project_id`, profile creation flag, `published: false`, `authorizes_worker_launch: false` |
| `planning.get` | `plan_id` | Recorded plan/reservation and apply progress; no replay or provider calls |
| `repository.select` | `project_id` | Actual path and inspection of that registered local profile |

`Onboarding.selected_repository(project_id)` supplies the same explicit selected
project context for the read-only dashboard GET route. Historical registrations
without an available matching profile fail with `selected_project_unavailable`.

Planning errors use fixed safe codes, including `planning_source_changed`,
`planning_accounts_changed`, `planning_budget_insufficient`,
`planning_invalid_response`, `planning_validation_blocked`,
`planning_attempt_held` and `planning_apply_held`. Failed generation/apply jobs
retain a scrubbed `result` with `plan_id` and known progress for explicit
inspection. They include neither raw exception text nor credentials.

The request format follows [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
and the [GPT-6.1 Sol model reference](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
The controller retains its conservative existing rate policy; the configured
reservation is not a statement of current account credit or an invoice.
