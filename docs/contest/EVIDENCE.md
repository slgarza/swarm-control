# Evidence and reproducibility package

The controller writes private execution artifacts and can export sanitized local
aggregates with `swarm-control evidence collect`. Acceptance review, billing
reconciliation and baseline approval remain external evidence. The exporter does
not issue a 100-agent certificate.

## Capture before dispatch

- Controller commit, model ID, Codex/Modal versions and runtime image identity.
- Target repository and exact starting commit, task IDs, dependency graph and
  acceptance criteria fixed before execution.
- Baseline CI result, test/check names and independently reviewed quality criteria.
- Authorized cumulative budget, prior allocations, run allowance and cost rules.
- Benchmark start/stop definition, deadline, repetition ID and comparison pairing.

Keep the original target ledger. Cloning a target for an experiment does not
implicitly authorize a new allowance or erase the prior spending history.

## Capture during and after execution

| Evidence | Purpose |
| --- | --- |
| Per-worker task, sandbox/session ID and base SHA | Establish identity and common starting source |
| Start/end and command/model events | Establish genuine activity and concurrency, including gaps |
| Original patch SHA256 and changed paths | Attribute actual source contributions |
| Worker final status and independent checks | Distinguish validated work from incomplete attempts |
| Reviewed candidate base, tree and head SHA | Bind validation and review to the candidate being published |
| PR URL, exact-head CI and merge SHA/time | Establish delivered work |
| Repair patch hashes and intervention log | Preserve worker/coordinator attribution |
| Post-merge checks, defects and reverts | Establish combined quality over the agreed observation window |
| Provider usage and reconciled billing | Establish the dollar denominator |
| Sandbox termination and gateway shutdown records | Establish cleanup, including failures |

Hash original evidence before deriving summaries and preserve it unchanged.
Retain a mapping from public aggregates to private originals so authorized
reviewers can audit them. Redacted exports have their own hashes; they do not
replace the originals. Timestamps should include timezone and clock source.

## Where the current controller writes evidence

Under the target's ignored `.swarm/` directory:

```text
control.sqlite3                  cumulative allocations and task claims
modal-controller.lock            dispatcher lock shared by Git worktrees
modal-runs/batch-.../
  plan.json                     selected base, tasks and requested configuration
  summary.json                  activity/concurrency summary when collected
  D01/
    manifest.json               task and worker configuration
    report.json                 status, timing, cleanup and recovery
    changes.patch               original recovered code patch
    changes.json                patch hash and scope checks
    checks.log                  final configured checks
    install.log                 dependency preparation
    agent.jsonl                 private agent events
    last-message.txt            worker handoff
    versions.json              worker tool versions
    integration.json           candidate/review/PR state after integration starts
    integration.patch          actual diff on the current integration base
```

Some files appear only after their stage runs successfully; early failures can
leave partial evidence. Other budget/probe/integration logs are also written.
Inspect actual outputs rather than treating this abbreviated tree as a completeness
guarantee. Git worktrees use the original checkout's common state directory.

Gateway administrative status can contain credential hashes. Agent logs can
contain prompts, source and sensitive output. Do not publish the raw `.swarm/`
tree, SQLite database, tokens, secret values or raw customer artifacts.

## Public report template

Copy [run-summary.template.json](run-summary.template.json) into a separate report
and fill only verified fields. It is a proposed report format, not an input to
`swarm-control`. The CLI export is a smaller measured format documented below.
`null` means unmeasured/unknown; it
must not be silently interpreted as zero. Empty lists mean entries have not been
provided, not proof that no failures or interventions occurred.

Attach a delivery table with task ID, acceptance outcome, PR URL, patch hash,
reviewed head, merge SHA/time, check evidence and repair attribution. Attach a
paired AFK table using the same definitions. Include rejected/failed tasks in the
full workload table rather than omitting them from the run.

Publish sanitized aggregates and source/PR links in an authorized repository.
Retain provider billing documents privately when they contain account details;
publish a reviewed breakdown and reconciliation status. Use a stable report commit
or release tag when submitting so later edits cannot silently change the evidence.

## Reviewer checklist

- Can another operator install this controller and reproduce the local example?
- Is the real target/backlog identified at a frozen starting commit?
- Do at least 100 distinct agents have genuine overlapping work evidence?
- Is every claimed delivery linked to accepted code, exact-head checks and merge?
- Are shared-file conflicts, combined failures and repairs accounted for?
- Does the elapsed time include the agreed review/integration/CI interval?
- Is dollar efficiency based on reconciled eligible charges, with exemptions clear?
- Is the baseline strong, comparable and selected without favorable cherry-picking?
- Are interventions, failures, missing events and quality regressions disclosed?
- Is cleanup confirmed without discarding durable budget history?

The historical ten-agent [public report](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot10-2026-11-10.md)
and [JSON](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot10-2026-11-10.json)
are useful examples of reporting limits and repair attribution. They are not
completed evidence for the 100-agent requirement or an AFK efficiency comparison.

## Exporting measured aggregates

Run this after integration and the agreed observation window. Store inputs and
exports in the target's ignored `.swarm/` or another private evidence directory.
The command is offline and only reads files; redirect stdout to save the report.

```sh
swarm-control evidence collect /path/to/target/.swarm/modal-runs/batch-ID \
  --pipeline swarm \
  --started-at 2026-11-24T14:00:00-06:00 \
  --ended-at 2026-11-24T15:00:00-06:00 \
  --protocol /private/comparison-protocol.json \
  --cost-receipt /private/reconciled-cost.json \
  --acceptance-receipts /private/acceptance.json > /private/swarm-report.json
swarm-control evidence compare /private/swarm-report.json /private/afk-report.json
```

The timestamps are examples, not a scheduled or completed run. They must cover
the full delivery clock defined in the benchmark, including setup after workload
release, review, CI and integration. Without both timestamps no hourly rate is
calculated. The tool checks worker/merge times against that interval but cannot
observe when the operator actually released the backlog.

Prepare one identical protocol file for the pair before dispatch:

```json
{
  "comparison_id": "target-repetition-1",
  "baseline_configuration_sha256": "<SHA256 of the approved baseline configuration>",
  "acceptance_policy_sha256": "<SHA256 of independent acceptance and quality rules>",
  "cost_policy_sha256": "<SHA256 of cost allocation and exemption rules>",
  "clock_definition": "Workload release through final accepted merge and required checks",
  "observation_window_seconds": 86400
}
```

Replace digest placeholders with 64 lowercase hexadecimal characters. Matching
protocol hashes establish matching supplied rules, not organizer approval. The
collector also hashes the task objects from every manifest, sorted by ID. Both
pipelines must use the same frozen task objects. An AFK adapter must produce the
same local evidence layout or an independently audited compatible report; the
controller does not run an AFK product or invent baseline results.

A reconciled cost receipt uses USD, includes all failed attempts and repairs,
and follows the same allocation rule for both pipelines:

```json
{
  "reconciliation_status": "reconciled",
  "gross_total": 12.50,
  "confirmed_internal_token_exemption": 0,
  "contest_eligible_total": 12.50
}
```

A positive exemption also requires `exemption_evidence_sha256`: the hash of the
private confirmation identifying who approved the classification and which
charges qualify. Eligible cost must equal gross cost minus that confirmed amount.
Keep invoices, credits, setup amortization and the confirming authority in the
private supporting breakdown. The exporter hashes the original receipt and
exports only allowed cost fields. It never substitutes budget reservations for
bills. Unknown cost and a zero dollar denominator produce null dollar rates.

The acceptance file is a list of independent review receipts:

```json
[
  {
    "task_id": "T001",
    "patch_sha256": "<original worker patch SHA256>",
    "reviewed_head_sha": "<reviewed PR head Git SHA>",
    "merge_commit_sha": "<actual merge Git SHA>",
    "accepted": true,
    "post_merge_checks": "success",
    "reverted": false,
    "observation_ended_at": "2026-11-25T15:00:00-06:00",
    "evidence_sha256": "<SHA256 of the independent acceptance/check evidence>"
  }
]
```

The hashes must match `integration.json`; the quality observation must cover the
protocol window after the merge. Explicit rejection, a revert or failed checks
does not count. Missing acceptance for a recorded merge leaves the accepted
delivery total unknown. The export reports observed merges separately.

For concurrency the exporter requires distinct sandbox IDs, matching target/base,
timezone-aware session times, observed command and model events, and a nonempty
patch with passing worker checks and matching hashes. Duplicate identities are
excluded in full. It computes half-open interval overlap with UTC-normalized
times, including seconds at or above 100. Sampled contribution spans end at the
last observed event and may contain idle gaps; they do not prove continuously
useful work. `scale_requirement_verified` and `contest_win_proven` remain false.

Only IDs, allowed statuses, numbers, timestamps and hashes are exported. Worker
prompts, source, commands, session credentials, handoffs, arbitrary errors and raw
logs are omitted. Original artifact hashes retain an audit mapping. Review even
sanitized exports before publishing. The CLI does not publish or call providers.
