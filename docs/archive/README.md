# Historical records

These are dated observations and validation receipts. Their commands, interfaces,
test totals and capability statements describe the recorded source version, not
the current release. Use the [current documentation index](../README.md) for
installation, credentials, planning, execution and integration instructions.

Historical image files and machine-readable receipts retain their original paths
and bytes so existing source references and hashes remain verifiable. A receipt
does not establish current test totals, provider capacity, invoices or acceptance.

## Interface history

- [November 11 interface captures](DASHBOARD_HISTORY.md).
- [Current interface and capture provenance](../DASHBOARD.md#screenshots).

## Dated controller validation

| Recorded scope | Historical receipt |
| --- | --- |
| Budget reporting preparation, November 11 | [Narrative](../contest/BUDGET-REPORTING.md), [receipt](../contest/BUDGET-REPORTING.json) |
| Operations holds and integration attempts, November 11 | [Receipt](../evidence/controller-operations-2026-11-11.json) |
| Initial repository/run workspace, November 11 | [First receipt](../evidence/run-workspace-validation-2026-11-11.json), [revised receipt](../evidence/run-workspace-validation-v2-2026-11-11.json) |
| Dependency and capacity validation, November 12 | [Receipt](../evidence/dependency-scalability-validation-2026-11-12.json) |
| Easy Run preparation, November 12 | [Receipt](../evidence/easy-run-validation-2026-11-12.json) |
| Initial GUI onboarding preparation, November 12 | [Receipt](../evidence/gui-onboarding-validation-2026-11-12.json) |

Some receipts refer to superseded onboarding steps or language guides. Those
references record what was checked then; they are not links to supported current
workflows. Current guides and interface text use English.

## Project lineage and pilot evidence

The controller was extracted from
[Afterimage commit 2217882](https://github.com/slgarza/Afterimage/commit/2217882c3d5597bbb92a4ae28bc8a2e02ccedb0b).
Afterimage is a separate client project. Its customer source, private credentials,
runtime records and cumulative accounting do not belong in this controller.
The local tutorial contains original small Python examples.

The [November 10 ten-worker report](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot10-2026-11-10.md)
records ten reviewed merged PRs, including one coordinator code repair.
The [November 11 hundred-worker report](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot100-2026-11-11.md)
records failed original workers and separately attributed provisional recoveries.
These records do not establish 100 useful concurrent agents, reconciled costs or
an advantage over an AFK baseline. Follow the [current evidence protocol](../contest/EVIDENCE.md)
for a new experiment; archived reports do not authorize another run.
