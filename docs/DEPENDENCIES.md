# Dependency analysis and a real 100-task frontier

One hundred task definitions can describe a serial chain, one hundred independent
changes, or a mixture. The analyzer reports the selected workload's prerequisites
and current conflict-free frontier before a run is admitted. It makes no provider
calls, opens no budget ledger, changes no task definitions and starts no workers.

From an installed controller:

```sh
swarm-control analyze D01 D02 --project /path/to/swarm-demo/swarm.toml --workers 2 --base HEAD
```

Pass the prepared cohort's actual task IDs, or use `--manifest` with a reviewed
JSON object containing a nonempty `taskIds` array. Do not combine a manifest with
positional IDs. Omitting both inspects the complete inventory; it does not select
the entire backlog for a paid run. To require a frontier for one hundred workers,
use a reviewed workload with sufficient independent ready tasks:

```sh
swarm-control analyze --project /path/to/target/swarm.toml --workers 100 --require-parallel 100 --base HEAD
```

For a frozen cohort, add `--manifest /path/to/target/swarm/cohort.json` to that
command. Backlog analysis can succeed while later tasks remain blocked on valid
prerequisites; an explicit cohort reports those selected-task holds as blockers.

This strict check is preparation evidence. Ordinary source, actual merged
dependency, issue, budget and provider-capacity gates still apply to execution.
Plans do not prove useful simultaneous work. New paid runs require their own
authorized remaining allowance.

## Read the report

| Report field | Meaning |
| --- | --- |
| `inventory_task_count` | Definitions supplied to the analysis |
| `selected_task_count` | Tasks explicitly selected for this stage |
| `task_count` | Selected tasks plus their hard prerequisite closure |
| `edges` | Directed prerequisites, each with a declared or inferred reason |
| `ready_count` | Selected tasks whose recorded status and hard prerequisites permit new work |
| `safe_frontier_count` | A deterministic conservative maximal set without observed read/write or write/write conflicts |
| `suggested_dispatch_ids` | Up to the requested worker count from that frontier, provided analysis is complete and valid |
| `max_wave_width` | Largest topological layer after hypothetical prerequisite completion |
| `critical_path_length` | Number of unfinished tasks in the longest dependency chain |
| `cycles` | Bounded cycle witnesses; a repeated endpoint closes each path |
| `selected_blockers` | Reasons an explicitly selected task cannot start |
| `review_hints` | Explainable possible relationships which need operator review |
| `static_scan.incomplete` | Existing source could not be inspected within the bounded scan |

The frontier is a greedy maximal set, not a mathematical maximum independent set.
Candidates with fewer observed conflicts are considered first, with path
specificity and task ID as deterministic tie-breaks. A broad root-owner task
therefore does not hide a ready frontier of one hundred separate leaf owners.
Likewise, a topological layer is not an exact maximum antichain. Neither is a
semantic proof that the tasks are independent. The report explicitly retains
`semantic_independence_verified: false` and `provider_capacity_verified: false`.
The `allow_overlap` setting records an intentional shared-file experiment; it
does not turn overlapping writes into a conflict-free frontier.
For a deliberately reviewed collision experiment, an operator can explicitly
choose a smaller minimum, such as `--require-parallel 1 --allow-overlap` with
100 workers. That overrides the default minimum, not the dependency, source,
budget or provider gates, and supplies no claim of 100 independent tasks.

Examples of workload shapes:

- **100 independent leaves:** current frontier 100, critical path one.
- **100 tasks in a chain:** current frontier one, critical path 100.
- **One bootstrap followed by 100 leaves:** current frontier one and theoretical
  future layer 100. Merge and verify the bootstrap first, then freeze a new base
  and prepare the leaf stage. A later wave cannot run against the old base and
  assume that unmerged prerequisite code exists.

The offline graph supports up to 10,000 definitions, including inventories larger
than one live cohort. Task files are limited to 256 KiB each and 32 MiB in total.
Analysis defaults to 100 requested workers and suggests no more than the requested
count; the graph can analyze larger hypothetical widths. This does not raise the
deployed controller/gateway's 100-worker per-cohort limit, account quota or spending
authorization. `--require-parallel` accepts 1–100 and is an explicit exit-status
gate for this command. Live runs default that minimum to 100 when 100 workers
are requested; choosing 100 workers alone does not make this analyzer strict.

The browser's AI planner reuses this analysis on its proposed tasks. Generating a
proposal requires separate planning authorization, and applying it requires
reviewed approval to create issues/files. Neither a model proposal nor a passing
graph is a worker launch. See [AI planning](AI_PLANNING.md).

## Declare dependencies and resources

`dependsOn` remains the authoritative direct task relationship. A dependency's
local `merged` status and positive `pullRequest` are only preparation observations;
live admission must verify the real merged PR and its ancestry in the chosen base.
Historical completed records can remain minimal: `id`, `status` and `pullRequest`.

Three optional task fields improve early detection:

```json
{
  "id": "API01",
  "ownership": ["src/contracts/session.ts"],
  "produces": ["contract:session-v1", "src/contracts/session.ts"],
  "consumes": [],
  "reads": ["docs/SESSION_CONTRACT.md"]
}
```

```json
{
  "id": "VIEW01",
  "ownership": ["src/views/session.tsx"],
  "consumes": ["contract:session-v1"],
  "reads": ["src/contracts/session.ts"]
}
```

These are resource-field excerpts. A runnable task still needs the normal title,
prompt, acceptance, issue and other project metadata described in
[project configuration](PROJECTS.md).

- `produces` names artifacts delivered by this task. Use a namespace such as
  `contract:session-v1` for a logical identity. An identity shaped like a relative
  file path with a suffix, such as `src/contracts/session.ts`, must fall inside
  the task's ownership. These declarations grant no additional write permission.
- `consumes` names artifacts the task requires. A unique producer automatically
  establishes a directed prerequisite. Missing or ambiguous producers are errors.
- `reads` lists repository-relative paths the task will read. Existing shared
  reads can be parallel. A reader and another task writing the same path receive
  a review hint. A path missing at the frozen base, with one explicitly declared
  `produces` provider, establishes a hard `missing_input` prerequisite.

Resource lists are bounded, unique and reject unsafe paths or credential-looking
identities. Existing task JSON without these fields retains its old behavior.
Frozen or completed experiment definitions are preserved; add resources in a new
reviewed workload or explicitly versioned preparation change.

## Static import review

When supplied a repository root and exact Git commit, the analyzer reads owned
source blobs from that commit. Dirty working-tree changes do not alter the scan.
It inspects common relative JavaScript/TypeScript import, export and require
forms, plus Python relative imports, without importing or running target code.

An existing import whose target is owned by another task produces a possible
dependency hint and a conservative parallel conflict. An unresolved relative
import can identify another task's exact planned file ownership, including an
unselected potential provider. Such a lexical hint does not invent a hard
direction or silently modify `dependsOn`. Review it and freeze the intended
contract explicitly.

New owned files absent at the selected base are expected. They receive an
informational coverage note, and can still supply a 100-task frontier under their
declared contracts. Their future implementation cannot be statically inspected.
The detector does not resolve arbitrary module aliases, dynamic code generation,
runtime API behavior or dependencies expressed only in prose. Human or independent
acceptance review remains necessary.

Source review is capped at 1,000 owned source files, 256 KiB per file and 8 MiB
total source bytes. Git inventory reads are capped at 16 MiB and 100,000 entries;
resource metadata is capped at 100,000 entries. Source read deadlines and pipe
limits prevent an unbounded capture. Existing source scan failures or truncation
make strict frontier eligibility false. An existing Python file whose imports
cannot be parsed also makes the scan incomplete. Reports contain bounded IDs, relative
paths and reasons, never source bodies, absolute checkout paths or credentials.

Only the selected tasks and their prerequisites contribute metadata errors,
cycles and ownership conflicts. Unrelated historical tasks do not invalidate an
explicitly selected independent cohort. Artifact ambiguity and missing-import
provider hints can reference inventory tasks outside that selection because those
relationships are relevant to the selected consumer.
The inventory still must be readable: malformed JSON, unsafe files or global
size-limit violations stop inventory loading before graph selection.
