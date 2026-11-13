# Archived budget reporting correction — November 11, 2026

This is a historical implementation and validation record. Its test counts and
measurements describe that attempt, not the current release. For current
accounting behavior and operator commands, use [Billing](../BILLING.md); for
archived evidence, see the [archive index](../archive/README.md). The companion
[JSON receipt](BUDGET-REPORTING.json) remains unchanged.

Local admission reservations could be mistaken for actual provider charges or
account funds. This correction made `budget status` present local policy and
source-scoped provider-reported spend separately, leaving account credit and attributed run
cost unknown when the evidence cannot establish them. See [the operator guide](../BILLING.md).

The explicit OpenAI synchronizer reads the official Costs API with an
administrative credential on the trusted controller. Manual observations keep
unverified provenance. Both retain period/scope, observation versus import time,
freshness, original source digests and private evidence. Failed refreshes remain
visible alongside older evidence. Current-day query windows are marked ongoing.

| Verification | Measured result |
| --- | --- |
| Full offline unit suite | 198 passing tests |
| Unit command elapsed time |22.101 seconds |
| Ruff / Python compilation / diff checks | Pass |
| Complete local validation sequence |22.225 seconds |
| Real target import/status/ledger comparison |1.271 seconds; original logical ledger and profile preserved |
| Live Costs API verification | Not performed; administrative credential was unavailable |
| New workers / deployments / budget migrations | 0 / 0 / 0 |
| Actual preparation billing / contest efficiency | Unknown / unproven |

The target demonstration imported a user-supplied dashboard observation and
verified that its reported spend appeared with manual provenance, unknown capture
time and no repository attribution. No financial value, account identifier or
raw observation is published here. Provider credit remains unknown; no balance is
invented from a monthly reference minus reported costs. No reservation was freed.

The 198 tests included actual CLI import/status operations on synthetic ledgers,
private-cache sharing across worktrees, source and normalized-receipt corruption,
permission/HTTP failures, pagination and exact decimal costs. Additional tests
prove the administrative credential is removed from coding and validation
subprocess environments while the trusted controller retains access.

Preparation retained an early misplaced fixture assertion and initial reporting
fixture mismatches; corrected focused tests and the complete suite subsequently
passed. The [machine-readable record](BUDGET-REPORTING.json) contains reviewed
aggregates and a private validation receipt digest. The instrumented preparation
segment excludes initial unmeasured inspection and subsequent publication/CI;
it is not a merged-feature throughput denominator. No real provider model call,
VM cohort, acceptance delivery or AFK comparison took place.

Provider evidence and allocation controls remain distinct. The benchmark still
requires independently reconciled cost before computing PRs per dollar.
