# Browser setup from a fresh download

Install Git and Python 3.12 with venv support, download this controller, and run
`./easy-run`. Keep that terminal open. All remaining preparation controls are in
the local browser. Each operator uses their own service accounts.

The pages follow the work: **Settings → Projects → Plan work → Project settings
→ New run → Runs**. A selected run has Summary, Workers, Changes, Costs and
Evidence tabs. Visiting a page starts no provider operation. The AI planning
controller turns an inspected repository and your goal into a proposed plan;
you review its commands and task contracts before approving real preparation.
Starting coding workers has a separate authorization.

## 1. Accounts and your personal .env

Open **Settings**. If GitHub CLI is missing, **Install GitHub CLI locally** downloads a pinned
[official release](https://github.com/cli/cli/releases/tag/v2.103.3), checks the
archive SHA256, and installs only its binary in a private per-workspace tools
directory. It requires no administrator access. Linux/WSL and macOS x64/ARM64 are
supported; a successful checksum check does not authenticate a GitHub account.

Choose your own `.env` file or enter its supported values in the password fields:

```dotenv
GH_TOKEN="YOUR_GITHUB_TOKEN"
MODAL_TOKEN_ID="YOUR_MODAL_TOKEN_ID"
MODAL_TOKEN_SECRET="YOUR_MODAL_TOKEN_SECRET"
CODEX_API_KEY="YOUR_OPENAI_API_KEY"
```

These are placeholders. The importer also accepts `GITHUB_TOKEN` and
`OPENAI_API_KEY` aliases. Unknown variables, duplicate assignments, shell commands,
interpolation and incomplete Modal token pairs are rejected. It parses literal
values; it never sources an uploaded file as a shell script. This file configures
controller accounts, not an arbitrary application environment inside workers.

**Save my local .env** writes a private `0600` file in the workspace's sibling
`.connections` directory, outside all Git repositories. The exact location is
shown in Accounts. With the default workspace it is
`~/.config/swarm-control/workspace.json.connections/.env`. It is a plaintext local
secret file protected by filesystem permissions, not an encrypted vault. Values
are not returned to HTTP, browser storage, command previews, operation history or
preparation publication. Individual GitHub/Modal forms also allow session-only
credentials. Closing that server session discards those in-memory overrides.

Saving credentials makes no verification or deployment request. Use **Verify
existing GitHub login** or **Verify existing Modal login** explicitly, or submit
the individual connection form to save and verify that provider. Verification
observes authentication; repository permissions, billing and available quota
remain separate. See [connection details](CONNECTIONS.md).

AI planning requires a local OpenAI key and a verified GitHub token saved or
supplied to this controller. Ambient terminal authentication alone cannot bind
issue creation to the account you reviewed. Saving the local key enables planning;
it does not upload that key to Modal.

Under **Project settings → Execution allowance**, choose **Create Modal Secret** and review the
secret name and environment. An empty API-key field uses your saved local model
key. This explicit operation sends that key to your selected Modal workspace as
`CODEX_API_KEY`. Replacing an existing secret requires its separate checkbox.
Your project profile stores only the secret's name. Secret presence does not
prove the key's model access, quota or billing status.

## 2. Choose, clone or fork a repository

Open **Projects → Add repository**. Choose **GitHub URL** to clone or fork, or
**Local folder** for an existing Git checkout. Use **Inspect and select folder**
for that folder. An existing valid
profile can be registered with **Connect prepared project**. Otherwise enter a
GitHub URL or `owner/repository` and an unused local destination to clone it. Selecting
**Create my own GitHub fork before cloning** creates or verifies your own fork,
clones that fork as `origin`, and retains the source repository as `upstream`.
Forking changes your GitHub account and requires explicit confirmation.

Cloning brings versioned files, including any `swarm.toml` and task JSON already
in the repo. It does not copy GitHub issues, runtime budgets, private local
credentials or ignored run artifacts. Inherited task status and history are
preserved. A fork whose profile still names its upstream requires an explicit
retarget operation. That creates separate service identities and disabled zero
allowances; existing runtime accounting prevents retargeting. Review inherited
issue/PR references and prepare your own tasks rather than treating upstream
work as a new ready backlog.

## 3. Generate and review a work plan

Choose **Plan work for this repository**. Click **Inspect repository** to read
the local tracked-file inventory and bounded context. Review the selected files,
enter your project goal, maximum tasks (up to 100), desired concurrent agents and
planning request limit. The request reserves a conservative model bound outside
the worker ledger. Actual billed cost remains unknown until reconciled.

Confirm sending that context to OpenAI, then **Generate work plan**. Review the
commands, every task's owned paths and acceptance, dependencies and independent
frontier. A 100-task chain cannot satisfy 100 concurrent agents; validation blocks
that plan. Static analysis cannot prove semantic independence, so review remains
required. No installation, tests, deployment or coding workers run at this step.

**Approve and prepare tasks** creates real GitHub issues and local task files.
It creates a disabled zero-allowance profile only if one is missing, and preserves
existing profiles, tasks and accounting. Source/account drift blocks approval.
Failed or uncertain model/issue calls retain their attempts; use **Previous
planning requests** to inspect them rather than automatically repeating them.
Then open **Review checks and publish**. See [AI planning](AI_PLANNING.md).

## 4. Manual project profile and task preparation

The following controls are available under **Project settings** if you prefer to
prepare the profile and tasks yourself. AI-prepared work uses the same profile,
task, issue and publication contracts.

Select your language's command starting point and review the actual install and
check commands. Commands are JSON argument arrays, executed directly without a
shell. Select instruction files, exact GitHub Actions check names, and the same
Modal Secret name/environment selected for this project. Create the local profile; it
starts with live operations disabled and zero allowance.

Initialization preserves an existing profile and existing instruction files.
Existing profiles show their actual configured commands for review. New profiles
use the source-bundle transport so private source can reach workers without
GitHub credentials. See [private repositories](PRIVATE_REPOSITORIES.md).

### Manual tasks

Import explicit GitHub issue numbers to get editable draft suggestions, or write
the task title and implementation request and create a new GitHub issue. Creating
an issue is a remote write. Imported text is untrusted task input, never a local
command to execute automatically.

Complete a unique task ID, owned paths, expected behavior and acceptance criteria.
Add prerequisite IDs, produced/consumed artifacts and protected behavioral checks
where appropriate. Saving a ready task verifies that its real issue is open.
The editor updates only its own unpublished drafts; published/frozen task files
stay preserved. Cloned ready tasks with another repository's issue URL remain
blocked in the run picker until separately reviewed/rebound work is prepared.

## 5. Project settings: checks and publication

**Run project checks** explicitly executes the configured install and check
commands on your computer. Review and trust those commands first; cloning and
inspection alone never run them. Controller credentials are removed from the
validation environment. Passing checks is separate from independent product
acceptance.

**Preview preparation changes** binds the exact service-managed files and source
state. Confirm the commit message and listed files before **Commit and push
preparation**. The publisher uses an isolated index and includes only those
reviewed setup files. It does not stage unrelated source, `.env` files or keys.
Changed files, unrelated staged changes, source drift and pending uncertain
publication stop the operation. Failed pushes retain the actual local commit;
review its recorded outcome before continuing.

## 6. Project settings: execution allowance and deployment

Enter the cumulative model and infrastructure allowances you authorize and the
worker capacity, up to 100. Existing allocations remain preserved. An increase
uses the controller's recorded migration workflow; it never resets or refunds a
ledger. Enabling live operations needs positive allowances and explicit consent.
Review and publish the resulting profile before preparing a run.

Deploy the gateway only through its explicit potentially billed action. The
returned gateway URL can be used for the separately authorized compatibility
probe and later run launch. A probe can incur model/compute usage and retains its
allocation on failure. A successful probe does not demonstrate capacity for 100
useful simultaneous workers.

## 7. CLI controls and the run builder

The **Project settings → Advanced tools** page builds or accepts supported `swarm-control` commands,
shows their exact arguments, and requires confirmation before execution. Source,
task and candidate inputs bind the preview. Changing accounts or reviewed inputs
requires a new preview. Each preview is single-use. This is a controller command
bridge; arbitrary system shells and direct unrecorded worker launches are not
available. Use **Open run builder** for the selected registered repository.

Open **New run**, select tasks and agents, **Check plan**, then **Save and review
run**. Open its Summary, review launch authorization and explicitly **Run agents**.
A request for 100 workers requires a conservative frontier of 100
independent ready tasks by default. See [run controls](RUNS.md) and
[dependency review](DEPENDENCIES.md).

Setup jobs run in a bounded background service and preserve a private request and
outcome before reporting completion. Reloading a page does not repeat the action.
Interrupted or unrecorded outcomes remain uncertain after a server restart. Use
the account/project operation activity and its recorded result before issuing another remote write.
Keep the terminal open until active setup or agent operations finish.
