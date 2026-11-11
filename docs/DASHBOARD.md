# Local dashboard

The dashboard shows a selected, curated evidence snapshot in a browser. It reads
local JSON and serves local assets. It does not start workers, change a budget,
review code, merge PRs, refresh provider billing, or contact GitHub or Modal.

From an installed checkout:

```sh
swarm-control dashboard --snapshot /path/to/curated-snapshot.json --port 8787
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). Press Ctrl+C in the terminal
to stop the server. The default port is 8787. The server binds to loopback; it
has no command-line option to expose it on another interface. Choose a curated
snapshot explicitly rather than a worker log, ledger, report directory, or
credentials file.

![Local dashboard showing the recorded hundred-worker pilot](images/dashboard-pilot100-2026-11-11.png)

Recorded view from November 11, 2026, around 19:00 UTC: 100 original failed
reports, 34 provisional product recoveries and unknown actual billing. This is a
historical capture while integration was still running, rather than a final
result or a hosted dashboard. The panel was built separately after the worker
cohort; it receives no feature credit in that cohort.

The page rereads the selected file through `/api/snapshot`; it does not collect
new run evidence. Its source timestamp and stale indicator describe the
snapshot's freshness. A stale snapshot is not evidence that agents are still
running. Worker activity remains unknown unless the curated source supplies an
explicit activity observation. Missing or invalid source data produces a generic
unavailable response without including the file path or its contents.

The task view keeps original worker outcomes separate from locally recovered or
coordinator-repaired patches. A provisional item requires the source's explicit
qualification, nonempty binding checks that are all literally true, and valid
merged-state, attribution, PR, commit and timestamp fields. Product and support labels describe task scope; they do not establish delivery by
themselves. An empty patch remains empty even when a helper check passed.

A finished delivery clock does not accept the later quality window. AFK
comparison, provider invoices and unknown amounts stay unproven or blank unless
the curated source supplies the corresponding evidence. Authorized admission,
model estimates, retained uncertainty, exposure ceilings and reconciled billing
have distinct fields. An unconfirmed invoice is never displayed as billed cost.

The backend accepts at most 8 MiB of JSON and 1,000 unique task rows. It projects
an explicit field allowlist; raw logs, arbitrary input fields, secrets, account
payloads and local paths are not forwarded. Links are restricted to the selected
GitHub repository. The HTTP interface exposes the page, its two bundled assets,
an empty favicon response, `/api/snapshot`, and `/api/health`. Write methods are
rejected. Host and
Origin checks, a restrictive Content Security Policy, no-store responses and no
CORS support keep the viewer local and read-only.

## Curated input

The viewer consumes the recovery delivery allowlist's top-level `repository`,
`run_id`, `as_of_utc`, `clock`, `rows`, `queue`, `final_metrics` and
`local_validation_summary`. Each task row identifies its original outcome and
patch hash, frozen product/support classification, binding checks and separately
attributed recovery. The source's T0 must precede its as-of timestamp; finished
clocks and qualifying merge/check observations must also fit that interval.
The viewer checks presentation consistency; it does not authenticate the source
or rerun independent semantic acceptance.

A target-owned adapter can optionally add:

| Field | Meaning |
| --- | --- |
| `dashboard_task_titles` | Mapping from task IDs to public, bounded display titles |
| `dashboard_activity` | Explicit `active_workers` and aware `observed_at`; missing or future observations stay unknown |
| `dashboard_links` | `report_url` and `tracking_url` within the selected GitHub repository |
| `dashboard_financials` | Separate new authorization, admission reserves, conservative model commitment, uncertainty, exposure, remaining admission and confirmed billed cost, with source kind/time |

Financial values are optional finite, nonnegative USD numbers. Missing values stay
null; a billed total also requires the source's explicit billing reconciliation.
The panel shows the source timestamp and marks an old capture as stale. Updating
the source file is the adapter's responsibility, outside this controller viewer.

## Checks

Run the focused checks from the controller checkout:

```sh
python -m unittest tests.test_dashboard -v
```

These tests use synthetic curated data and temporary local HTTP servers. They
exercise projection, qualification, unknown costs, malformed input, source
read-only behavior, route limits and HTTP protections without paid resources.
