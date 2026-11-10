# Evidence and reproducibility package

The controller already writes private execution artifacts. The contest reporting
package described here is a manual assembly process; this release does not
automatically produce the proposed benchmark summary or a 100-agent certificate.

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
`swarm-control` and not an automatic export. `null` means unmeasured/unknown; it
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
