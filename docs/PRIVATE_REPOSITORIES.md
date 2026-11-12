# Private repositories and frozen source bundles

Private GitHub repositories can use a local source bundle rather than giving
GitHub account credentials to coding workers. The controller first needs the
authorized repository checked out locally and its complete selected history.
The existing issue, source, dependency, policy and budget gates still apply.

Configure the reviewed target profile:

```toml
[runtime]
source_transport = "bundle"
```

The runner's `--source-transport bundle` explicitly selects the same transport.
The default `github` preserves the public-repository clone workflow. Planning is
read-only; it selects the transport without constructing a bundle or starting
workers. Repository privacy alone does not change the default for older profiles.

Before opening a budget ledger or contacting Modal, an explicit run constructs
one self-contained Git v2 bundle in an isolated local bare repository. Its only
advertised ref points at the exact frozen commit. Building is limited to 120
seconds and a final 64 MiB bundle, with bounded temporary object storage. Missing
history, shallow clones, unsafe private storage and oversized bundles fail before
provider admission. No source refs, index or tracked checkout files are changed;
uncommitted and untracked files are excluded.

The bundle is retained with its digest and private preparation receipt under the
common checkout's ignored `.swarm/source-bundles/` directory. An existing ignore
rule for `.swarm/` is required. Directories are owner-only and bundle/receipt files
are `0600`. This contains private repository source and history: preserve it as
private evidence and never publish it. Temporary construction objects are removed;
failed partial bundles are removed while their failure receipt is retained.

All workers reuse the same prepared bundle; a 100-task cohort creates one local
bundle and uploads those same bytes to each isolated VM. Modal SDK 1.6.3 supplies
`sandbox.filesystem.copy_from_local.aio`; this version has no `Sandbox.open` API.
Each upload has a 120-second bound and shares the existing 240-second preparation
allowance with checkout/install. Coding and VM lifecycle limits remain unchanged.
There is no automatic retry, replacement worker or additional allowance.

The helper verifies uploaded size, SHA-256, the single advertised ref and the
selected commit, then clones locally without network access or target hooks.
The worker's origin is set to the exact credential-free project GitHub URL.
GitHub account tokens, Modal account tokens and the operator's OpenAI key are not
part of the bundle protocol or worker environment; the existing expiring task
gateway token remains separate. Install and validation use the ordinary project
commands after checkout. The uploaded intermediate bundle is removed on successful
preparation; worker cleanup still runs after failed uploads or preparation.

Public plans/reports expose only transport, base, byte count and digest. They do
not contain the local bundle path or source bytes. Bundles do not include files
outside the selected Git history, operating-system credential stores or ignored
local `.env` files. Repository owners remain responsible for keeping credentials
out of tracked source/history.

Forked task definitions with `issueUrl` must point to the configured project's
exact issue number. A matching number in the fork does not establish that an
upstream issue is the same work item. Foreign definitions require explicit
review/rebinding; their original files and delivery history are preserved.

Offline tests construct real Git bundles and clone them with an unreachable
private GitHub origin, exercise integrity and resource bounds, and reuse one bundle
across 100 mocked uploads. No paid workers or live private-repository cohort were
run by these tests.
