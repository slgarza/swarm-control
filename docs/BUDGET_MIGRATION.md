# Reviewing a cumulative budget increase

`swarm-control budget` inspects an existing target ledger and prepares a proposed
policy increase without allocating money or creating resources. Applying an
increase is a separate, explicit offline operation requiring a reviewed plan and
a private receipt of the owner's actual authorization. A plan is not authorization.
This guide does not approve any increase for Afterimage or another project.

All operations select the target's root `swarm.toml`. Git worktrees use the
original checkout's `.swarm/control.sqlite3` and the same dispatcher/integration
locks. There are no provider calls, gateway deployments, probes, worker launches,
GitHub writes, allocation refunds or model requests in these commands.

## Inspect and propose without changing the ledger

```sh
swarm-control budget inspect --project /path/to/target/swarm.toml
```

Inspection opens an existing regular SQLite file using URI `mode=ro`. It does not
instantiate `Ledger`, initialize policy tables, create a missing `.swarm/`
directory, acquire lock files or reset anything. A missing ledger is reported
without creating one; it cannot be migrated. The output includes allocated and
remaining cents, active task count, the stored policy and a logical state digest.

An older ledger may have allocation/task history but no populated `budget_policy`
table. Inspection identifies that case as `legacy_inferred_v1_default`, explicitly
inferring the original USD18 model/USD7 infrastructure limits. It preserves the
absence of the policy table. Invalid history, allocations exceeding that policy,
symlinked state/ledger files and malformed stored policy stop inspection.

SQLite may create or update its own `-shm`/empty `-wal` coordination sidecars
when reading a database configured for WAL. Read-only here means no SQL writes,
policy initialization, allocation changes or budget lock files; it does not
promise an immutable directory listing. Inspection reads committed live WAL
transactions so pending checkpointing cannot hide existing reservations.

Keep live execution disabled and the profile's limits equal to the old policy.
Choose proposed whole-cent cumulative limits, then capture a private plan:

```sh
umask 077
swarm-control budget plan --project /path/to/target/swarm.toml \
  --model-limit-cents "$SWARM_PROPOSED_MODEL_CENTS" \
  --infra-limit-cents "$SWARM_PROPOSED_INFRA_CENTS" > /private/migration-plan.json
```

The two task-specific shell variables must contain the proposed integer amounts.
This command only prints a proposal. It requires an existing ledger, matching
old profile limits, disabled execution and no running or awaiting-integration
tasks. Neither limit may decrease, at least one must increase, and each must be
at most 1,000,000 cents. Existing reservations remain spent for admission purposes.
The proposed totals must therefore include all earlier allocations plus the
proposed future run's allowance. Admission reservations are not provider invoices.

The plan binds the repository, common ledger path and physical file identity,
profile hash, exact old/new policy, legacy/stored policy source, allocation totals
and logical digest of all database tables and schema. Its digest covers those
fields. Changes after review require a fresh plan and fresh reviewed authorization;
do not rewrite an old plan to conceal drift.

## Record the actual owner's authorization

An authorized operator must independently review the proposal and retain the
underlying confirmation privately. Create a separate JSON receipt with exactly
these fields, populated from that actual confirmation and the reviewed plan:

| Field | Required content |
| --- | --- |
| `version` | Integer `1` |
| `kind` | `budget_policy_increase` |
| `approved` | Boolean `true`, only after actual approval |
| `authorization_id` | A new 8–100 character identity using letters, digits, `_` or `-` |
| `repository` | Exact plan repository |
| `ledger_path` | Exact plan ledger path |
| `plan_sha256` | Exact reviewed plan digest |
| `state_sha256` | Exact plan state digest |
| `old_policy_source` | Exact plan source, including the explicit legacy designation where present |
| `old_policy` | Exact plan object with `model_limit_cents` and `infra_limit_cents` |
| `new_policy` | Exact approved plan object with both limits |
| `confirming_authority` | Identity of the owner who authorized these exact limits |
| `confirmation_evidence_sha256` | SHA256 of the retained private confirmation |

The tool validates this receipt's bindings and records its original-byte SHA256.
It does not authenticate an approval system or independently prove that the
named person granted permission. The operator remains responsible for the real
authorization and supporting evidence. Do not fabricate a receipt or treat
preparation, an accounting ceiling or a requested capacity as approval.

Keep the plan, authorization receipt and confirmation outside version control,
for example in the target's ignored `.swarm/` or a private directory. Both JSON
inputs must be regular files with no group/other permissions (`chmod 600`);
symlinked inputs are rejected. No credentials, API keys or raw provider logs
belong in the receipt.

## Apply only the reviewed increase

After actual approval, a separately authorized operator can run:

```sh
swarm-control budget apply --project /path/to/target/swarm.toml \
  --plan /private/migration-plan.json \
  --authorization /private/authorization.json --apply
```

The explicit `--apply` flag and separate receipt are both required. Execution
remains disabled throughout. Apply acquires both existing single-host controller
locks, reloads the profile, verifies the repository and old limits, and starts
`BEGIN IMMEDIATE` on the existing ledger. It rechecks physical identity, logical
state, profile hash, exact old policy and absence of active/awaiting work before
changing anything. A busy database or changed state stops the attempt.

For a legacy ledger only, the transaction initializes the original policy before
increasing it. For an existing policy it updates that single row. The transaction
also appends `policy_migrations`, including the authorization identity, receipt
and confirmation hashes, authority, plan/state hashes, old/new limits and equal
before/after allocation totals. SQL triggers reject audit updates and deletions.
Authorization identities cannot be reused. The transaction verifies all protected
table contents and allocation totals before committing; unexpected history
changes roll back the policy and audit insert together.

The tool does not create a backup or replace the database. SQLite provides the
atomic transaction; retain the original private evidence and any independently
managed backups. The append-only audit is an application/database control, not
protection against a database administrator deliberately removing its triggers.
Locks coordinate cooperating processes sharing one checkout, not other clones,
hosts or arbitrary direct database editors.

After success, independently review and update **only the target profile's two
budget limits** to match the stored policy. Keep `live_enabled=false`. The
controller intentionally rejects use of the old profile against the new policy.
Applying this accounting migration does not authorize enabling execution or a new
run. Model/VM quotas, gateway capacity, source/issue admission, benchmark protocol
and the separately approved run allowance still require their normal gates.

The remote gateway's immutable per-run ledger is not migrated or reset. Its
USD180 per-run ceiling and existing reservations remain unchanged. This command
only manages the local cumulative policy used before admission; a new run must
still fit both the preserved cumulative allowance and all normal gateway checks.
