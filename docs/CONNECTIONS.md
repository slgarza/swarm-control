# Connect your service accounts

Open **Settings** in the local browser. Account setup belongs to the computer
running Easy Run.
Connecting a repository and authenticating a service are separate steps. Opening
the panel and reading connection status make no provider requests.

Enter a GitHub personal access token and a Modal API token ID/secret pair in the
password fields, then explicitly save and verify each service. GitHub verification
reads the authenticated account name; Modal verification reads the authenticated
workspace name. These checks do not prove repository permissions, VM capacity,
model quota or an authorized budget. Existing GitHub CLI and Modal authentication
can instead be checked explicitly without replacing global profiles.

For Modal's browser sign-in flow, the terminal alternative remains `modal setup`.
That command normally writes Modal's own `~/.modal.toml`. The GUI does not edit
that file. Modal API tokens authenticate the controller to Modal; they are separate
from the OpenAI key used by the gateway.

## Session and saved credentials

Choose whether credentials are retained for this server session or saved for
later starts. You can enter the individual password fields or import literal
`.env` contents through the account setup view. Importing the file only saves
values; use the separate verification and Secret creation actions afterward.
Saved credentials are **plaintext**, protected by file
permissions, rather than encrypted or stored in an operating-system keychain.
The service uses a directory beside the selected workspace registry:
`workspace.json.connections/.env` for a registry named `workspace.json`.
The account setup view displays its exact local path. Its directory is owner-only
(`0700`); `.env`, metadata and lock files are `0600`. Newly written `settings.json`
contains observations and Secret names, with no actual keys. Legacy private
`credentials.json` files remain readable and are preserved without modification;
new credential writes use `.env`.
The service refuses persistence inside a Git checkout, through symlink paths,
or to credential files with unsafe ownership, permissions or hard links.

Session credentials disappear when the server stops. A session override leaves
previously saved credentials intact, so those saved credentials apply again on
the next start. There is no account-removal operation or global-profile rewrite.
The server passes GitHub/Modal credentials only to controller subprocess
environments, never to task definitions or coding-worker metadata. An OpenAI key
saved by choice in `.env` is excluded from that environment.

The downloadable template has empty values:

```dotenv
GH_TOKEN=""
MODAL_TOKEN_ID=""
MODAL_TOKEN_SECRET=""
CODEX_API_KEY=""
```

Fill the required values on your computer. `GITHUB_TOKEN` is accepted as an alias
for `GH_TOKEN`; `OPENAI_API_KEY` is accepted as an alias for `CODEX_API_KEY`.
Other variable names, duplicate names/aliases, command substitution, variable
expansion and multiline values are rejected. Both Modal token fields must be
provided together. Single/double quotes are supported without shell execution;
empty template values are left unchanged. The file is not sourced as a script.
Never upload the completed file to GitHub. Its location must remain outside every
Git checkout, including worktrees. This flow does not change global Git settings.

Status distinguishes **configured** from **verified**. Verification is a cached
observation with its timestamp and safe account/workspace name; loading the page
does not refresh it. An expired token or later permission change needs another
explicit verification. Provider error messages and secret values are excluded
from public status. Editing a saved `.env` outside the running application needs
a server restart; its changed digest invalidates previous cached authentication.

GitHub API authentication and Git fetch/push authentication are separate.
Browser clone and preparation publication install a credential helper for those
commands only. The integrator uses the controller's normal Git fetch/push path;
a GUI token alone does not configure its HTTPS helper. On your own trusted
controller, authenticate the intended GitHub account in the CLI and configure
its Git helper, for example with `gh auth setup-git --hostname github.com`.
Verify that this is the account intended for the target. Never put a token in a
remote URL, worker manifest or source bundle. See [private repositories](PRIVATE_REPOSITORIES.md).
Public fetches may work anonymously, but publishing an integration branch still
needs push authentication even when the repository is public.

## Store the gateway's OpenAI key

Open **Project settings → Execution allowance** and first verify the intended
Modal workspace in **Settings**. Choose the exact Secret name and Modal
environment used by the target's `runtime.openai_secret` and `runtime.environment`,
enter the OpenAI API key and explicitly create the Secret. The controller sends
the key to a separate Modal SDK process over stdin and stores it remotely as
`CODEX_API_KEY`. It puts the key in no subprocess arguments or HTTP status.
The browser's Secret form does not save the entered key locally. The backend
supports the separate explicit `persist_key` option, but this form does not send
it. A key already saved in the private `.env` can be used without
typing it again. Saving that local key alone never creates a Modal Secret.
Changing a local key also does not replace an existing remote Secret; replacing
the gateway's key remains a separately confirmed operation.

Creating an existing name fails without overwriting it. An explicit replacement
updates `CODEX_API_KEY` in the named existing Secret, preserving unrelated keys;
it fails if that Secret is missing. This changes a real Modal resource. A timeout
or interrupted response can leave the outcome uncertain; there is no automatic
retry. Inspect that outcome before another explicitly authorized write. A later
read can verify that the Secret contains the required key, but cannot read its
value or prove which key was retained after an uncertain update.

**Verified OpenAI setup means the Modal Secret was observed with `CODEX_API_KEY`.**
It does not validate the API key against OpenAI, make a model request or check
model access, quotas, billing or account credit. The gateway still needs its own
explicit deployment and the existing policy/source/budget gates before execution.
Changing the authenticated Modal workspace invalidates the old Secret's verified
status. The backend rechecks the workspace immediately before a Secret operation.
**Verify last configured secret** checks the recorded Secret name/environment,
not every selected project's configuration. Compare that observation with the
profile's `runtime.openai_secret` and `runtime.environment` before deployment.

## Local AI planning uses a separate key path

Save the OpenAI key under **Settings → Your personal local .env**, or retain it in a
controller session through the supported connection API. **Plan work** uses that
local key directly for its explicitly authorized Responses request. It does not
read the key back from Modal, use a ChatGPT login or accept an ambient
`OPENAI_API_KEY` as a substitute for this local saved/session choice.

Before generation, verify a GitHub account whose token the controller captured
from the saved/session connection or its `GH_TOKEN`/`GITHUB_TOKEN` environment.
Token-less ambient GitHub CLI login remains available for manual operations but
cannot bind a planner's issue writes to the reviewed account. Changing the key
or GitHub identity invalidates that plan's account binding.

Planning has a separate retained reservation beside the workspace registry.
Saving a key grants no allowance; generating a plan requires explicit source
upload and cost authorization. Creating issues/tasks, creating a Modal Secret,
deploying and launching workers are separate actions. See [AI planning](AI_PLANNING.md).

## Implementation boundaries

`Connections(workspace_path)` supplies cached `status()`, explicit
`configure_github(token, persist=True)`,
`configure_modal(token_id, token_secret, persist=True)`, `verify(provider)` and
`configure_openai(api_key=None, secret_name=None, environment='main', replace=False, persist_key=False)`.
An omitted `api_key` uses the saved/session key. `configure_environment(dotenv_text,
persist=True)` saves literal values without network operations.
`environment_template()` returns empty template text, variable names/configured
flags and the owner's local storage path; it never returns saved values.
Operations return the same public status shape. `ConnectionsError.code` and its
fixed message are safe for the local interface. The account setup's storage path
belongs only in that local view, rather than exported evidence snapshots.
Provider operations have a
30-second outer bound, bounded stdout and discarded stderr; no raw provider logs
are retained. Slow status reads do not wait for the network operation's lock.

`environment()` returns a new **private** controller subprocess mapping. Never
serialize it. It adds configured GitHub/Modal credentials and excludes
`CODEX_API_KEY`, `OPENAI_API_KEY` and `OPENAI_ADMIN_KEY`. Optional billing Cost API
credentials are therefore not supplied by this GUI connection flow. Explicit GUI
Modal tokens use an empty child configuration, leaving global profiles untouched.
Modal operations run in independent processes using the pinned SDK 1.6.3, avoiding
global SDK client caches and process-wide environment changes.
`redact(text)` removes known saved/session/ambient credentials, including the
locally saved model key, and recognized token/assignment patterns from bounded
controller output before it is retained or returned. It exposes no credential
inventory. Arbitrary customer secrets remain outside this credential inventory.

The connection service creates no workers, gateway deployment or allowance, and does not
change the 100-worker capacity. Offline tests use temporary fixtures and mocked
provider operations; they do not establish a live account connection.

Primary references: [Modal Secrets](https://modal.com/docs/guide/secrets),
[Secret create, lookup and update APIs](https://modal.com/docs/sdk/py/latest/Secret).
The pinned SDK's `modal/config.py`, `modal/_workspace.py` and `modal/secret.py`
provide the version-specific authentication and operation contracts.
