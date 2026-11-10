# Bounded operations

Every command selects a target with `--project /path/to/target/swarm.toml`.
The examples assume the installed `swarm-control` executable is on PATH.
Live commands require an explicitly enabled, funded policy and existing provider
authentication. Project secret names reference Modal secrets containing CODEX_API_KEY;
real model/GitHub credentials never reach the coding workers.

```sh
swarm-control deploy-gateway --project /path/to/target/swarm.toml
swarm-control probe --project /path/to/target/swarm.toml --gateway-url https://YOUR-GATEWAY.modal.run
swarm-control run A1 A2 --project /path/to/target/swarm.toml --workers 2 --synchronized --model gpt-6.1-sol --model-budget 1.00 --gateway-url https://YOUR-GATEWAY.modal.run
```

Obtain the gateway origin from Modal's deployed application or
`modal.Function.from_name(GATEWAY_APP, "gateway").get_web_url()`.
Deploy uses the configured app, durable gateway ledger, secret name and environment;
keep all three identifiers unchanged when migrating an existing deployment.
Afterimage's profile retains its original identifiers. Extraction deploys nothing.

The probe reserves USD0.50 model and USD1.35 infrastructure allowance. Worker
admission reserves USD0.30 per VM plus USD1 overhead, before provisioning. Models
are priced conservatively with a margin and no cached-input discount. Reservations
are admission controls, not provider invoices. Failures do not refund allocations.
The gateway separately enforces expiring per-worker budgets and durable requests.
See [gateway details](GATEWAY.md).

Each worker gets one task, an exact base, isolated checkout and bounded gateway
token. Synchronized batches wait for all preparations before starting. VM limits
remain 2 CPU/2 GiB maximum, 300 seconds agent time and 1200 seconds total lifecycle.
Patches, hashes, events and final checks are recovered before termination; inspect
any recovery or cleanup failure. Original private artifacts remain under the
target's ignored `.swarm/`, shared with its Git worktrees.

Review each full patch and acceptance tests, then use one integrator:

```sh
swarm-control integrate plan ARTIFACT_DIR --project /path/to/target/swarm.toml
swarm-control integrate prepare ARTIFACT_DIR --project /path/to/target/swarm.toml
swarm-control integrate publish ARTIFACT_DIR --project /path/to/target/swarm.toml --reviewed-patch-sha REVIEWED_SHA256
# Wait for CI on the exact published head.
swarm-control integrate merge ARTIFACT_DIR --project /path/to/target/swarm.toml --reviewed-patch-sha REVIEWED_SHA256
```

Prepare applies the patch on the current configured base branch and runs every
configured command. Publish/merge bind review to the patch hash and tested tree;
merge checks the current base and green exact-head Actions. GitHub atomically
checks the head, not the base, so do not merge through a second client concurrently.
Changed bases need a new validated attempt with old artifacts preserved.

After a cohort, verify sandbox termination, stop gateway runs and stop its deployed
app when no other authorized work needs it. Reconcile billing, recorded repairs,
reverts and full delivery time. A validated patch is not a merged green PR.
