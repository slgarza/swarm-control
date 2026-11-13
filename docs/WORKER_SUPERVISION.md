# Worker supervision and task acceptance

Coding workers keep the configured 30–1800 second coding limit. A small process
wrapper owns the Codex subprocess group, records observations and preserves a
private `supervision.json` checkpoint. The outer exec allows twelve additional
seconds only to stop the child and retain its receipt; the coding allowance and
VM lifecycle policy are unchanged. There is no automatic retry, replacement VM,
continuation or budget increase.

The wrapper samples bounded Git status and file metadata inside task ownership,
including new files. A source fingerprint change is an edit signal, not proof
that the feature is correct. The default records warnings when no source change
has been observed and when the closure window approaches. Quiet inference alone
is allowed until the coding deadline.

An operator can explicitly select an earlier stop:

```sh
swarm-control run T01 --project /path/to/target/swarm.toml --base HEAD \
  --workers 1 --model gpt-6.1-sol --timeout 900 --no-progress-timeout 300 \
  --model-budget 1.00 --gateway-url https://your-gateway.modal.run
```

This is a real paid-run command; ordinary source, issue, capacity and cumulative
budget gates still apply. The no-progress period must be between 30 seconds and
the coding limit. It measures absence of observed owned-source changes, not
absence of reasoning. An unavailable source observation does not establish idle
work. At a configured stop the wrapper sends TERM and, after bounded grace, KILL
if needed. It records the explicit policy reason, signals and raw child exit.
Those are worker-supervisor observations, never authenticated provider timeout
or OOM claims. Historical negative/137 exits retain their original unknown causes.

Controller phase and native file-change receipt times remain separate from the
wrapper's source observations. A missing or invalid final supervision receipt
prevents an otherwise successful process from becoming a validated worker.
Failures still recover artifacts and terminate the sandbox.

## Frozen context readiness

CLI task plans and preflight expose the size and digest of required instruction
documents, prompt sizes, excerpt coverage and configured task acceptance.
Documents are read once per readiness call from regular Git objects at the exact selected base;
lazy fetching and replacement objects are disabled. Source text and absolute
paths are excluded from the public readiness metadata. Token counts remain
unknown until actually measured. Large payloads and missing task selectors are
visible warnings; global instructions are preserved.

## Configured task acceptance

Tasks may add frozen argv lists and protected input files:

```json
{
  "acceptance_checks": [["python3", "tests/acceptance/search_behavior.py"]],
  "acceptance_files": ["tests/acceptance/search_behavior.py"]
}
```

The helper runs these after successful baseline checks, with both stages sharing
the existing 240-second validation allowance. If that allowance is exhausted,
acceptance stays unrun. Commands are bounded argv arrays, executed without shell
interpretation or service credentials. Explicit shell interpreters are rejected.
Protected inputs must remain byte-identical to the original frozen base before
and after acceptance. Modified inputs reject the candidate. `acceptance.json`
and `acceptance.log` are separate from baseline check artifacts.

Configured acceptance is not independent product acceptance. Required review,
combined-tree checks, GitHub CI and a real product journey remain separate gates.
Existing tasks without `acceptance_checks` retain their baseline behavior and
show a readiness warning rather than fabricated acceptance.

## Recovery candidates

A failed, interrupted or `needs_repair` worker may retain a nonempty, clean,
digest-bound `recovered_candidate`. Its original status and process exit remain
unchanged. The candidate requires explicit review and independent validation;
it grants no publication or spending authority. Empty patches, protected-input
changes, scope/head violations, redacted patches and missing recovery artifacts
never become recovery candidates.
