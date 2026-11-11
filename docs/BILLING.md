# Reported spend and local admission policy

Use `swarm-control budget status` to understand money before preparing a run.
The status command is offline: it reads the local policy and previously saved
provider evidence. It does not query a provider, start a worker, change limits,
release reservations or enable execution.

```sh
swarm-control budget status --project /path/to/target/swarm.toml
swarm-control budget status --project /path/to/target/swarm.toml --json
```

## The amounts have different meanings

| Amount | Source and meaning |
| --- | --- |
| Local limits | The operator's cumulative admission policy in the target profile and original ledger |
| Reserved | All retained allocations, including failed or ambiguous attempts; not provider charges |
| Unreserved local allowance | Local limits minus allocations; zero can block admission without proving that the provider account is empty |
| Reported provider spend | Costs API results or a clearly identified operator observation/export for a specific scope and period |
| Monthly reference | A dashboard reference supplied in an observation; its enforcement and current applicability remain unknown |
| Provider credit balance | Unknown unless an appropriate independent source supplies it; the Costs API does not return a credit balance |
| Reconciled run cost | Independently attributed and reviewed billing for the exact experiment, including all services, failures and repairs |

The provider's organization total may include other projects, API keys or work.
Selecting the target repository with `--project` does not identify an OpenAI
project. Provider evidence remains **unassigned to a run**. Neither an API read
nor a screenshot satisfies the benchmark's cost reconciliation receipt.

The report includes the source kind, period, observed and recorded timestamps,
freshness and scope. An unknown capture time stays unknown even if an old
observation is imported today. Missing provider evidence is unknown, never zero.
A displayed monthly reference is not silently copied into the authorized local
policy or treated as prepaid credit. The report does not subtract an aggregate
spend from that reference to invent available account funds.

## Read provider costs explicitly

The trusted operator needs an administrative API credential with access to
organization costs in `OPENAI_ADMIN_KEY`. Configure it through your own private
environment or secret manager; do not put it in task files, command arguments,
the repository, receipts or worker environments. Ordinary worker/model keys are
not used as a fallback. Install/check subprocesses and coding-agent environments
remove this administrative credential.

Choose the actual provider scope explicitly. The default query period starts
at the beginning of the current UTC month and ends at the next UTC midnight,
including the current day's reported costs. The report marks that interval as
still in progress and preserves the observation time; it is not a completed
monthly invoice. Explicit dates or timezone-aware timestamps select another
period:

```sh
swarm-control budget sync-openai --project /path/to/target/swarm.toml \
  --openai-project-id "$SWARM_OPENAI_PROJECT_ID"

swarm-control budget sync-openai --project /path/to/target/swarm.toml \
  --organization-scope --start 2026-11-01 --end 2026-11-11
```

The second example's end is exclusive: midnight at the start of November 11 UTC.
Use the intended observation interval, not these example dates automatically.

This command makes only bounded, paginated GET requests to the official
[organization Costs endpoint](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs).
It validates scope, currency, amount precision and response shape, and records
provenance and a source digest. It creates no model calls, provider resources or
budget allocation. Missing credentials, rejected permissions and incomplete
responses remain explicit failures; an error is not converted into zero cost.
OpenAI's [spend controls](https://developers.openai.com/api/docs/guides/spend-limits)
and request/token quotas are separate from these cost observations.

API-confirmed provenance means the reported spend was fetched by this connector.
It is not proof of an invoice, account credit, exclusive use by this repository,
or approval to spend. A private local cache is an operator artifact, not a signed
provider attestation. Retain external source evidence for independent review.

## Import a dashboard observation or provider export

When administrative billing access is unavailable, import a normalized JSON
observation through the same reporting path:

```sh
swarm-control budget import-spend --project /path/to/target/swarm.toml \
  --file /private/provider-observation.json
```

The input contains the version, provider, source kind, decimal USD amount,
period and scope, timestamps and source metadata. Dashboard observations use
`source_kind: "dashboard_observation"`,
`verified_by_provider_api: false`, `billing_reconciled: false`,
`authorizes_spend: false`, and `provider_credit_balance_usd: null`. Unknown
observation timestamps and period boundaries are null; use a clear period label.
`scope.attribution` remains `"unassigned"`. Imported files cannot assert that
they were verified by a live API call. Imports bind the actual original file
digest rather than trusting a supplied checksum.

Complete synthetic input example (not a real account or price estimate):

```json
{
  "version": 1,
  "provider": "openai",
  "source_kind": "dashboard_observation",
  "recorded_at_utc": "2026-11-11T12:00:00Z",
  "observed_at_utc": null,
  "period": {
    "start_utc": null,
    "end_utc": null,
    "label": "Example dashboard window; exact bounds not supplied"
  },
  "scope": {
    "level": "organization",
    "project_ids": [],
    "attribution": "unassigned"
  },
  "spend_usd": "1.23",
  "monthly_reference_usd": "100",
  "reference_kind": "dashboard_monthly_reference",
  "source_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "verified_by_provider_api": false,
  "billing_reconciled": false,
  "authorizes_spend": false,
  "provider_credit_balance_usd": null
}
```

Import replaces the example digest with the original input's SHA256 and stamps
the actual local ingestion time. It does not manufacture the missing capture
time. Exported provider data may use `source_kind: "provider_export"` with the
same explicit period/scope and unverified-import rules.

Snapshots and retained original imports are written under the target's ignored
`.swarm/billing/` with private permissions. New attempts are append-only;
failures do not overwrite prior observations. Invalid JSON, unsupported currency,
unsafe paths or malformed amounts cannot silently replace a valid report.
Raw billing identifiers, account data and original exports stay private.

## Admission and benchmark behavior

The existing `budget inspect` JSON remains compatible with migration clients,
and now labels its amounts as local admission reservations. Its provider balance
and spend fields remain unknown because inspection itself reads no provider.
Offline plans also explicitly separate policy reservations from provider funds.

Importing or refreshing spend **does not refund allocations, raise a ceiling,
reset a ledger, change a remote gateway run or authorize another attempt**.
Use the [reviewed migration workflow](BUDGET_MIGRATION.md) only for an actual
owner-authorized increase. Keep execution disabled until the distinct launch
gates pass. This change does not claim to reconcile previous provider bills.

The [benchmark protocol](contest/BENCHMARK.md) still requires a reconciled,
properly attributed cost receipt before calculating PRs per dollar. Organization
spend, dashboard observations, admission reserves and gateway estimates remain
separate evidence categories. A cache refresh never manufactures a contest win.
