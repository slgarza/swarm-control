# Prepare a repository from the browser

Repository preparation is separate from executing agents. It can obtain a GitHub
checkout, create a disabled project profile and help you write useful tasks.
It does not run repository scripts, install project dependencies, deploy services
or authorize model spending.

## Choose a repository

Open **Projects → Add repository**. The **GitHub URL** tab handles clone/fork;
**Local folder** inspects an existing checkout. After inspection, connect an
existing profile with **Connect this prepared project to workspace**, use
**Plan work for this repository**, or choose **Configure manually**.

Use a GitHub `owner/repository` or its exact `https://github.com/owner/repository`
URL, optionally ending in `.git`. Extra URL paths, queries and fragments are not
accepted. Choose a destination folder that does not exist yet, inside an existing local
folder. Other hosts, credential-bearing URLs and Git command options are refused.

Cloning keeps the repository's history, tracked configuration and task bytes.
Existing task statuses are preserved, including completed work. GitHub issues are
separate: cloning does not import them or reset completed tasks to ready.

Choose **fork** explicitly if you need your own GitHub repository. This creates or
reuses the authenticated account's fork, or a selected organization's fork, then
clones that fork. `origin` names the fork; `upstream` names the original source.
The returned fork identity is checked against GitHub before cloning. A cloned
`swarm.toml` that still names the upstream repository needs explicit review; it is
never silently rewritten to grant the fork another project's runtime or budget.
An explicit **retarget** operation can prepare that fresh fork: it changes only
the profile, selects fresh owner/repository-specific runtime identities and sets
disabled, zero-budget policy. Existing task bytes, issue/PR references and completed
statuses stay intact and need review for the fork. Retargeting refuses a checkout
with existing local runtime accounting, and cannot reset an already matching
project's policy. Use a separate fresh clone for a new identity.

Controller authentication can clone repositories your GitHub account can access.
New profiles use frozen Git bundle transport: the controller prepares the reviewed
Git base for upload to worker machines, which do not need the controller's GitHub
credential. That transport supports public and private repository source. The
alternative direct `github` worker transport supports public repository cloning.
The selected transport is shown in configuration and capability metadata. Your
controller's GitHub credential is never copied into task JSON, a worker manifest
or a clone URL. Source transport does not establish provider readiness or authorize
execution.

Git hooks, recursive submodules, global Git configuration and automatic LFS smudge
are disabled during preparation. Authentication uses the controller environment
and the trusted GitHub CLI credential helper. Clone and GitHub operations have
bounded time and output; failures return safe messages without raw command text.

## Initialize project settings

Inspection reads tracked filenames and bounded package metadata. It may suggest
Python or npm install/check commands; these are suggestions for review. It never
executes package scripts to discover whether they work.
For an existing valid profile, inspection shows its actual configured argument
arrays, instruction files, check names, runtime identity and budgets for review.
Credential-looking values are redacted. The configured base branch is shown
separately from the branch currently checked out.

Initialization requires reviewed install/check **argument arrays**, for example:

```json
{
  "install": [["npm", "ci", "--no-audit", "--no-fund"]],
  "checks": [["npm", "run", "check"]]
}
```

It creates a root `swarm.toml`, a task directory and an original `AGENTS.md` if
missing. Missing `.swarm/`, `.env` and `.env.*` protection is appended to
`.gitignore`, preserving its existing bytes and recording the edit in the
publication allowlist. Fresh-fork retargeting applies the same protection.
Other existing files are preserved; initialization refuses an existing `swarm.toml`.
The configured repository must match the actual origin. Instructions must refer
to existing project files or the newly created `AGENTS.md`; symlinks are refused.

The new profile has `live_enabled = false` and both budgets set to zero. Runtime
app, gateway, ledger, secret **name**, environment and capacity can be chosen;
credentials do not belong in these fields. Capacity accepts 1–100 but establishes
neither quota nor permission to spend. Read [project configuration](PROJECTS.md)
before choosing the runtime image or enabling execution.

Explicit profile updates can change only runtime identity/capacity and the budget
policy fields. They preserve other sections semantically, while formatting and
comments may change. The caller must perform any required durable ledger migration
before updating an existing policy. `RepositorySetup.update_profile` itself never
migrates a ledger. The browser's **Execution allowance** operation first performs
any explicitly authorized policy migration through the budget service, then
writes the reviewed profile. Neither path clears allocations, verifies available
provider credit or grants runtime admission by itself.

## Write and review tasks

Select individual GitHub issue numbers to import. Only those open issues are read;
pull requests and closed issues are refused. The result is an editable blocked
draft suggestion, not a saved file or an instruction for the controller to execute
the issue's prose. Review its title and description, then specify ownership and
measurable acceptance criteria. You can also explicitly create an issue from a
reviewed title and description; that action writes to GitHub and requires its own
confirmation. An uncertain response means checking GitHub before retrying.

A saved task defaults to `blocked`, so incomplete drafts can be kept honestly.
Marking it `ready` requires ownership, acceptance and a positive issue number that
is verified as a real open issue in the selected repository. Every task still
passes the ordinary source/dependency/issue gates before dispatch.
New imported or saved issue references include the canonical repository issue URL;
an inherited foreign URL is refused rather than silently rebound to a coincidental
issue number in a fork.

Choose a new uppercase ID for new work. The editor can update an unpublished draft
that this preparation service created and whose bytes still match its journal.
It refuses to overwrite existing versioned, externally changed, frozen or completed
task definitions. Use a new reviewed task rather than rewriting a completed
experiment. Optional artifact/resource metadata follows [dependency analysis](DEPENDENCIES.md).

## Publish preparation explicitly

The preview lists only preparation files created or explicitly edited through
the setup service, with byte counts and hashes. It binds those bytes to the current
repository, branch and Git head. Review it before confirming commit and push.
Changed bytes, a changed head or existing staged changes require another review.
Unrelated tracked edits and untracked files remain outside the commit.

Publication uses an isolated Git index and hashes the reviewed file bytes without
running clean filters. It creates a commit without hooks and can explicitly push
that commit to the selected branch of the validated GitHub repository. Local Git
includes and URL rewrites stop browser publication. Push does not imply successful
CI, accepted delivery, a merged feature PR or authorized worker execution.

If push fails after the local commit, the actual commit remains recorded. Inspect
the repository and a fresh preview; a confirmed retry reuses that commit instead
of creating another. No automatic retry or force push occurs.

Before profile/task writes, private receipts preserve original hashes and bytes
under `repository-preparation/` beside the workspace registry. Keep the registry
and these companion folders outside every Git checkout; the default location
already does this. Individual files are replaced atomically. A partial
multi-file write remains held for inspection with its original evidence; another
action cannot silently overwrite it. These private records and controller
credentials never enter the preparation publication allowlist.

After publishing, connect the prepared project and use **Check plan** before
saving a run draft. A set of 100 serial tasks cannot fill a 100-worker independent
frontier. See [repositories and runs](RUNS.md), [dependency analysis](DEPENDENCIES.md)
and the [two-agent walkthrough](TWO_AGENT_WALKTHROUGH.md) for execution prerequisites.
