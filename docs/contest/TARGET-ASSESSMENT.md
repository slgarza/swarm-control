# Target assessment: Afterimage

Assessment date: 2026-11-10; updated after the November 11 hundred-worker pilot.
Afterimage supported a real source-based implementation cohort with 100 worker
VMs, distinct coding threads and per-task command/model activity. Its qualifying
useful concurrency, collision handling and comparative efficiency remain unproven.
Repository size is not the binding requirement; useful simultaneous work is.

## Update: observed hundred-worker pilot

The [November 11 report](https://github.com/slgarza/Afterimage/blob/main/docs/swarm/pilot100-2026-11-11.md)
records the separate frozen B001–B100 workload and its execution from target
commit `79404fe6d4ed5cf1b59b5610a3aebbe9d1f8021f`. These tasks used disjoint leaf
ownership and explicit UI, engine, protocol and client-policy contracts. They are
distinct from the earlier dependency-bearing A graph described below.

All 100 original reports ended `needs_repair`: 63 contained nonempty patches and
37 were empty. The report preserves those failures and separately identifies
unchanged patch recovery, coordinator repairs, independent review, exact-head CI,
combined checks and actual merges. It does not equate 100 VMs with 100 useful
contributors or describe the disconnected leaf modules as a playable MVP.

This establishes that Afterimage was sufficient for a real infrastructure and
delivery pilot. It does not settle whether it is the best contest target. The
disjoint workload did not exercise live shared-file collisions; the assisted
recovery and serial integration tail are measured costs of this run. Actual
billing, the full quality window and a strong AFK comparison remain necessary
before claiming efficiency. The earlier inspection below remains historical
evidence at its original commit, rather than a description of today's repository.

## Update: full design source and 100-task backlog

The original checkout audit below did not include the design documents stored in
the owner's separate OpenClaw project. That source was subsequently retrieved:
eleven documents covering canonical Rules v1, screen flows, visual design, audio,
City Moments, achievements and monetization. The prepared
[Afterimage PR33](https://github.com/slgarza/Afterimage/pull/33) imports unchanged
source snapshots with hashes and defines exactly 100 new implementation tasks.

The initial graph has 11 tasks without unmet prerequisites and 89 waiting on code
dependencies or specific edge decisions/delivery gates. A001/A004/A026 form the
first disjoint three-worker candidate cohort. This is enough to prepare useful
staged product work; it is not 100 useful simultaneous coding sessions. Reassess
the final target using observed overlap, quality and throughput after the early
cohorts rather than assuming that either repository size or total task count
establishes scale. No new paid cohort accompanied the backlog preparation.

## Evidence

Read-only inspection of the clean checkout at
[`b90519426a0bcec2b4f95c3f219f247678edd92d`](https://github.com/slgarza/Afterimage/tree/b90519426a0bcec2b4f95c3f219f247678edd92d):

| Signal | Observed value |
| --- | --- |
| Tracked files | 74 |
| Production TypeScript/TSX, excluding tests | 16 files, 809 lines |
| Test files | 16, 1,374 lines |
| Workspaces | Four |
| Task metadata | 12 merged, one superseded, zero ready |
| Open GitHub issues / PRs at inspection | Zero / zero |
| Latest main CI job | Green, 86 seconds |

File and line counts were measured from tracked files at the pinned commit;
they are not claims from the project README. The
[README](https://github.com/slgarza/Afterimage/tree/b90519426a0bcec2b4f95c3f219f247678edd92d)
reports 291 passing tests. The
[task records](https://github.com/slgarza/Afterimage/tree/b90519426a0bcec2b4f95c3f219f247678edd92d/swarm/tasks),
[issue list](https://github.com/slgarza/Afterimage/issues) and
[CI run](https://github.com/slgarza/Afterimage/actions/runs/42905639874)
provide the remaining public evidence. Issue and CI state can change after this
assessment.

The Firebase authority and complete multiplayer flow remain product work, and
the planned backend/E2E paths are not implemented in this commit. Some game
contracts still need decisions. Generating 100 tickets immediately would risk
inventing specifications, splitting trivial work or dispatching blocked tasks.
Completed pilot tasks cannot count again as fresh deliveries. The original pilot
allowance remains accounted for; this assessment authorizes no additional spend.

## Selection gate for the final target

Select a maintained repository with permission to contribute, a passing baseline,
reproducible CI and independent acceptance checks. Review at least 100 useful
unblocked tasks that can start from the same frozen commit, plus repair work and
dependent follow-ups. Include realistic shared-file edits with explicit contracts;
100 unrelated cosmetic changes do not demonstrate delivery efficiency.

Before reserving compute, freeze the backlog, acceptance and PR granularity,
required checks, baseline configuration, observation window and billing rules.
Confirm API/VM/CI capacity and an authorized cumulative budget. Use the identical
initial workload for the swarm and the strongest approved AFK baseline.

Afterimage can exercise same-file clean merges, genuine conflicts, combined test
failures, repair receipts and original-versus-integrated patch review. Expand only
as real product requirements become ready. Then use bounded follow-up cohorts
to address the observed failures before a qualifying collision and AFK benchmark,
preserving failures, interventions, spending and cleanup evidence. Each new paid
run needs its own authorized allowance. The observed hundred-worker execution
establishes infrastructure scale; useful simultaneous contribution and an
efficiency advantage remain unproven.
