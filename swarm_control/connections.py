"""Private controller account credentials and explicit, bounded verification.

Never serialize environment(): it contains controller credentials. Public methods
return only the fixed status projection. No network action runs from status().
"""

import copy
import fcntl
import hashlib
import hmac
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

MAX_FILE_BYTES = 32768
MAX_OUTPUT_BYTES = 8192
TIMEOUT_SECONDS = 30
_PROVIDERS = {"github", "modal", "openai"}
_MODEL_KEYS = {"CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY"}
_MODAL_OAUTH = {"MODAL_OAUTH_REFRESH_TOKEN", "MODAL_OAUTH_CLIENT_ID",
                "MODAL_OAUTH_CLIENT_SECRET", "MODAL_OAUTH_JWT_KEY"}
_CODES = {"github_auth_failed", "modal_auth_failed", "provider_unavailable", "provider_timeout",
          "invalid_provider_response", "sdk_version_mismatch", "secret_exists", "secret_missing",
          "workspace_changed", "configuration_changed", "helper_unavailable"}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_ENV_KEYS = ("GH_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "CODEX_API_KEY")
_ENV_ALIASES = {"GITHUB_TOKEN": "GH_TOKEN", "OPENAI_API_KEY": "CODEX_API_KEY"}
_TOKEN_PATTERN = re.compile(
    r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}|(?:ak|as)-[A-Za-z0-9_-]{8,}|Bearer[ \t]+[^\s\"'<>]+)", re.IGNORECASE)
_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:GH_TOKEN|GITHUB_TOKEN|MODAL_TOKEN_ID|MODAL_TOKEN_SECRET|CODEX_API_KEY|OPENAI_API_KEY|OPENAI_ADMIN_KEY)"
    r"[\"']?\s*[:=]\s*(?:\"[^\"]+\"|'[^']+'|[^\s\"']+)", re.IGNORECASE)


class ConnectionsError(ValueError):
    """Fixed, HTTP-safe errors; never interpolate paths, keys or subprocess text."""

    def __init__(self, code):
        self.code = code
        super().__init__({"invalid_credential": "Enter a valid credential without whitespace.",
                          "invalid_name": "Use a name with letters, numbers, underscores or hyphens.",
                          "invalid_option": "The account setup option is invalid.",
                          "invalid_environment": "Use only the supported credential names and literal values, without expansion.",
                          "storage_unsafe": "Private settings must be outside Git repositories and symlink paths.",
                          "storage_unavailable": "Private settings could not be saved securely.",
                          "storage_corrupt": "Private settings are invalid; preserve them for local review.",
                          "modal_not_verified": "Verify the intended Modal workspace first.",
                          "openai_not_configured": "Choose and configure a Modal Secret first."
                          }.get(code, "The account operation could not be completed."))


def _credential(value):
    if (not isinstance(value, str) or not 8 <= len(value) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in value) or "$" in value or "`" in value):
        raise ConnectionsError("invalid_credential")
    return value


def _name(value):
    if (not isinstance(value, str) or not _NAME.fullmatch(value)
            or value.startswith(("ghp_", "gho_", "github_pat_", "ak-", "as-", "sk-"))):
        raise ConnectionsError("invalid_name")
    return value


def _now():
    return datetime.now(UTC).isoformat()


def _observation(*, verified=False, identity=None, error_code=None, uncertain=False, checked=True):
    return {"verified": verified, "identity": identity, "checked_at_utc": _now() if checked else None,
            "error_code": error_code, "last_operation_uncertain": uncertain}


def _empty():
    return {"schema_version": 1}


def parse_environment(text, *, allow_empty=False):
    """A literal credential-file subset, with no shell or dotenv expansion."""
    if (not isinstance(text, str) or len(text.encode()) > MAX_FILE_BYTES
            or any(ord(char) < 32 and char not in "\r\n\t" for char in text)):
        raise ConnectionsError("invalid_environment")
    result, seen = {}, set()
    try:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key = _ENV_ALIASES.get(key.strip(), key.strip())
            if not separator or key not in _ENV_KEYS or key in seen:
                raise ValueError
            seen.add(key)
            value = value.strip()
            if "$" in value or "`" in value or "\x00" in value:
                raise ValueError
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'"):
                if len(value) < 2 or not value.endswith("'") or "'" in value[1:-1]:
                    raise ValueError
                value = value[1:-1]
            if value:
                result[key] = _credential(value)
        if ("MODAL_TOKEN_ID" in result) != ("MODAL_TOKEN_SECRET" in result):
            raise ValueError
    except (ValueError, TypeError):
        raise ConnectionsError("invalid_environment") from None
    if not result and not allow_empty:
        raise ConnectionsError("invalid_environment")
    return result


def _render_environment(values):
    return ("# Private Swarm Control credentials. Keep outside Git.\n"
            + "".join(key + "=" + json.dumps(values[key]) + "\n" for key in _ENV_KEYS if key in values)).encode()


def _execute(command, *, env, input=None, timeout=TIMEOUT_SECONDS):
    """Bound both time and stdout without ever retaining stderr or raw logs."""
    process = None
    output = bytearray()
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE if input is not None
                                   else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            pending = memoryview(input) if input is not None else None
            if pending is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                for key, _ in selector.select(min(remaining, .1)):
                    if key.fileobj is process.stdin:
                        try:
                            pending = pending[os.write(key.fd, pending):]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, MAX_OUTPUT_BYTES + 1 - len(output))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_OUTPUT_BYTES:
                            raise ValueError
            process.wait(timeout=max(.01, deadline - time.monotonic()))
        return process.returncode, bytes(output)
    except (TimeoutError, subprocess.TimeoutExpired):
        return None, b""
    except (OSError, ValueError):
        return -1, b""
    finally:
        if process is not None:
            # Bound descendants too, including a naturally exited direct child
            # whose descendants still hold an output descriptor.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.SubprocessError:
                    pass
            if process.stdout:
                process.stdout.close()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()


class Connections:
    """One browser-server session; persisted data is private plaintext, not a keychain."""

    def __init__(self, workspace_path, *, base_environment=None, runner=None):
        self._workspace = Path(workspace_path).expanduser().absolute()
        self._directory = self._workspace.with_name(self._workspace.name + ".connections")
        self._path = self._directory / "credentials.json"
        self._metadata_path = self._directory / "settings.json"
        self._env_path = self._directory / ".env"
        self._base_environment = dict(os.environ if base_environment is None else base_environment)
        self._runner = runner or _execute
        self._lock = threading.RLock()
        self._session = {}
        self._ambient_observations = {}
        self._disk = _empty()
        self._disk_env = {}
        self._session_model_key = None
        self._storage_error = None
        try:
            self._assert_location()
            self._disk = self._read()
        except ConnectionsError as error:
            self._storage_error = error.code
        except OSError:
            self._storage_error = "storage_unavailable"

    def _assert_location(self):
        for path in (self._directory, *self._directory.parents):
            bare_repository = ((path / "HEAD").is_file() and (path / "objects").is_dir()
                               and (path / "refs").is_dir() and (path / "config").is_file())
            if path.is_symlink() or (path / ".git").exists() or bare_repository:
                raise ConnectionsError("storage_unsafe")
        if self._workspace.is_symlink():
            raise ConnectionsError("storage_unsafe")

    @staticmethod
    def _private(info, *, directory=False):
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (not correct_type or info.st_uid != os.getuid() or info.st_mode & 0o077
                or (not directory and info.st_nlink != 1)):
            raise ConnectionsError("storage_unsafe")

    def _read_bytes(self, path):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            self._private(os.fstat(stream.fileno()))
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ConnectionsError("storage_corrupt")
        return raw

    @staticmethod
    def _json(raw):
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ConnectionsError("storage_corrupt")
                value[key] = item
            return value
        return json.loads(raw, object_pairs_hook=unique)

    def _read(self):
        try:
            if not self._directory.exists():
                self._disk_env = {}
                return _empty()
            self._private(self._directory.stat(), directory=True)
            legacy = _empty()
            if self._path.exists() or self._path.is_symlink():
                legacy = self._json(self._read_bytes(self._path))
                self._validate(legacy)
            values = {}
            if "github" in legacy:
                values["GH_TOKEN"] = legacy["github"]["token"]
            if "modal" in legacy:
                values.update(MODAL_TOKEN_ID=legacy["modal"]["token_id"],
                              MODAL_TOKEN_SECRET=legacy["modal"]["token_secret"])
            env_raw = None
            if self._env_path.exists() or self._env_path.is_symlink():
                env_raw = self._read_bytes(self._env_path)
                try:
                    values.update(parse_environment(env_raw.decode(), allow_empty=True))
                except ConnectionsError:
                    raise ConnectionsError("storage_corrupt") from None
            metadata = {}
            if self._metadata_path.exists() or self._metadata_path.is_symlink():
                metadata = self._json(self._read_bytes(self._metadata_path))
                if (not isinstance(metadata, dict) or type(metadata.get("schema_version")) is not int
                        or metadata["schema_version"] != 2
                        or set(metadata) - {"schema_version", "environment_sha256", *_PROVIDERS}
                        or not isinstance(metadata.get("environment_sha256"), str)
                        or not re.fullmatch("[a-f0-9]{64}", metadata["environment_sha256"])):
                    raise ConnectionsError("storage_corrupt")
                projected = {"schema_version": 1}
                for provider in _PROVIDERS & metadata.keys():
                    entry = metadata[provider]
                    fields = {"github": {"token": "validation-placeholder"},
                              "modal": {"token_id": "validation-placeholder", "token_secret": "validation-placeholder"},
                              "openai": {}}[provider]
                    if not isinstance(entry, dict) or (provider != "openai" and set(entry) != {"observation"}):
                        raise ConnectionsError("storage_corrupt")
                    projected[provider] = {**fields, **entry}
                self._validate(projected)
            result = _empty()
            for provider in ("github", "modal", "openai"):
                fields = None
                if provider == "github" and "GH_TOKEN" in values:
                    fields = {"token": values["GH_TOKEN"]}
                elif provider == "modal" and "MODAL_TOKEN_ID" in values:
                    fields = {"token_id": values["MODAL_TOKEN_ID"], "token_secret": values["MODAL_TOKEN_SECRET"]}
                elif provider == "openai":
                    previous = metadata.get(provider, legacy.get(provider))
                    if previous:
                        fields = {"secret_name": previous["secret_name"], "environment": previous["environment"]}
                if fields is not None:
                    previous = metadata.get(provider, legacy.get(provider, {}))
                    observation = previous.get("observation", _observation(checked=False))
                    if metadata and (env_raw is None or hashlib.sha256(env_raw).hexdigest()
                                     != metadata["environment_sha256"]):
                        observation = _observation(checked=False)
                    result[provider] = {**fields, "observation": observation}
            self._disk_env = values
            return result
        except ConnectionsError:
            raise
        except (OSError, ValueError, TypeError):
            raise ConnectionsError("storage_corrupt") from None

    @staticmethod
    def _validate(value):
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1
                or set(value) - {"schema_version", *_PROVIDERS}):
            raise ConnectionsError("storage_corrupt")
        for provider in _PROVIDERS & value.keys():
            record = value[provider]
            fields = {"github": {"token"}, "modal": {"token_id", "token_secret"},
                      "openai": {"secret_name", "environment"}}[provider]
            if not isinstance(record, dict) or set(record) != fields | {"observation"}:
                raise ConnectionsError("storage_corrupt")
            try:
                for field in fields:
                    (_name if provider == "openai" else _credential)(record[field])
            except ConnectionsError:
                raise ConnectionsError("storage_corrupt") from None
            observation = record["observation"]
            if (not isinstance(observation, dict)
                    or set(observation) != {"verified", "identity", "checked_at_utc", "error_code",
                                           "last_operation_uncertain"}
                    or type(observation["verified"]) is not bool
                    or type(observation["last_operation_uncertain"]) is not bool
                    or observation["error_code"] not in _CODES | {None}
                    or observation["identity"] is not None and (
                        not isinstance(observation["identity"], str)
                        or not _IDENTITY.fullmatch(observation["identity"]))):
                raise ConnectionsError("storage_corrupt")
            if observation["checked_at_utc"] is not None:
                try:
                    timestamp = datetime.fromisoformat(observation["checked_at_utc"])
                    if timestamp.utcoffset() is None:
                        raise ValueError
                except (ValueError, TypeError):
                    raise ConnectionsError("storage_corrupt") from None
            elif observation["verified"] or observation["error_code"] is not None:
                raise ConnectionsError("storage_corrupt")

    @contextmanager
    def _write_lock(self):
        self._assert_location()
        try:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._private(self._directory.stat(), directory=True)
            descriptor = os.open(self._directory / "settings.lock",
                                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a") as stream:
                self._private(os.fstat(stream.fileno()))
                fcntl.flock(stream, fcntl.LOCK_EX)
                yield
        except ConnectionsError:
            raise
        except OSError:
            raise ConnectionsError("storage_unavailable") from None

    def _persist(self, provider, record, *, expected=None):
        with self._write_lock():
            latest = self._read()
            if expected is not None and latest.get(provider) != expected:
                self._disk = latest
                return False
            latest[provider] = record
            values = dict(self._disk_env)
            if provider == "github":
                values["GH_TOKEN"] = record["token"]
            elif provider == "modal":
                values.update(MODAL_TOKEN_ID=record["token_id"], MODAL_TOKEN_SECRET=record["token_secret"])
            self._commit(latest, values)
            self._disk = latest
            self._storage_error = None
            return True

    def _commit(self, records, values):
        env_raw = _render_environment(values)
        metadata = {"schema_version": 2, "environment_sha256": hashlib.sha256(env_raw).hexdigest()}
        for provider in _PROVIDERS & records.keys():
            record = records[provider]
            metadata[provider] = {"observation": record["observation"]}
            if provider == "openai":
                metadata[provider].update(secret_name=record["secret_name"], environment=record["environment"])
        # A crash between the two replacements invalidates cached verification
        # through the environment digest; it cannot authenticate changed tokens.
        for target, raw in ((self._env_path, env_raw),
                            (self._metadata_path, (json.dumps(metadata, allow_nan=False) + "\n").encode())):
            temporary = self._directory / ("settings." + uuid.uuid4().hex + ".tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        directory_fd = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self._disk_env = values

    def _set_environment(self, values, persist):
        if type(persist) is not bool:
            raise ConnectionsError("invalid_option")
        if persist:
            with self._write_lock():
                records = self._read()
                combined = {**self._disk_env, **values}
                for provider, fields in (("github", {"token": combined.get("GH_TOKEN")}),
                                         ("modal", {"token_id": combined.get("MODAL_TOKEN_ID"),
                                                    "token_secret": combined.get("MODAL_TOKEN_SECRET")})):
                    affected = "GH_TOKEN" in values if provider == "github" else "MODAL_TOKEN_ID" in values
                    if affected:
                        records[provider] = {**fields, "observation": _observation(checked=False)}
                if "CODEX_API_KEY" in values and "openai" in records:
                    records["openai"]["observation"] = _observation(checked=False)
                self._commit(records, combined)
                self._disk = records
                self._storage_error = None
                if "GH_TOKEN" in values:
                    self._session.pop("github", None)
                if "MODAL_TOKEN_ID" in values:
                    self._session.pop("modal", None)
            if "CODEX_API_KEY" in values:
                self._session_model_key = None
        else:
            if "GH_TOKEN" in values:
                self._session["github"] = {"token": values["GH_TOKEN"], "observation": _observation(checked=False)}
            if "MODAL_TOKEN_ID" in values:
                self._session["modal"] = {"token_id": values["MODAL_TOKEN_ID"],
                                           "token_secret": values["MODAL_TOKEN_SECRET"],
                                           "observation": _observation(checked=False)}
            if "CODEX_API_KEY" in values:
                self._session_model_key = values["CODEX_API_KEY"]
                record, _ = self._effective("openai")
                if record:
                    self._session["openai"] = {**record, "observation": _observation(checked=False)}

    def _model_key(self):
        return self._session_model_key or self._disk_env.get("CODEX_API_KEY")

    def planning_account_binding(self, binding_secret):
        """Private keyed identity binding; never a public credential digest.

        The planner keeps its random binding secret outside Git. Rotated keys or
        a different verified GitHub account invalidate a reviewed plan, including
        after restart with persisted credentials. Ambient model keys are excluded.
        """
        with self._lock:
            github, source = self._effective("github")
            token = github.get("token") if github else self._base_environment.get("GH_TOKEN", self._base_environment.get("GITHUB_TOKEN"))
            observation = github.get("observation", {}) if github else self._ambient_observations.get("github", {})
            value = [token, self._model_key(), source, observation.get("verified") is True,
                     self._identity(observation.get("identity"))]
            return hmac.new(binding_secret, json.dumps(value, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()

    def with_planning_key(self, callback, *, binding_secret, expected_binding):
        """Deliver the local key directly to one trusted in-process HTTP request.

        It never enters a subprocess environment, an argument or a job result.
        Copy under the lock, then release it before the bounded network call.
        """
        from .setup_common import require
        with self._lock:
            require(self.planning_account_binding(binding_secret) == expected_binding,
                    "The connected account changed. Inspect the repository again.", "planning_accounts_changed")
            key = self._model_key()
            require(bool(key), "Save an OpenAI key in Settings before generating a plan.", "planning_key_required")
        return callback(key)

    def _known_credentials(self):
        keys = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", *_MODEL_KEYS, *_MODAL_OAUTH}
        values = {self._base_environment.get(key) for key in keys}
        values.update(self._disk_env.values())
        values.add(self._session_model_key)
        for collection in (self._disk, self._session):
            for provider in ("github", "modal"):
                record = collection.get(provider, {})
                values.update(record.get(key) for key in ("token", "token_id", "token_secret"))
        return {value for value in values if isinstance(value, str) and value}

    def redact(self, text):
        """Scrub bounded controller output; never return the credential inventory."""
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        if not isinstance(text, str):
            return "Output unavailable."
        with self._lock:
            values = self._known_credentials()
        variants = {variant for value in values for variant in
                    (value, json.dumps(value)[1:-1], quote(value, safe=""))}
        for value in sorted(variants, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        text = _TOKEN_PATTERN.sub("[redacted]", text)
        return _ASSIGNMENT_PATTERN.sub("[redacted]", text)

    def configure_environment(self, dotenv_text, persist=True):
        """Save literal credentials only; no verification or Secret creation."""
        values = parse_environment(dotenv_text)
        with self._lock:
            self._set_environment(values, persist)
            return self.status()

    def environment_template(self):
        with self._lock:
            env = self.environment()
            variables = [{"name": key, "configured": bool(self._model_key()) if key == "CODEX_API_KEY"
                          else bool(env.get(key)), "source": ("session" if self._session_model_key else
                          "private_file" if self._disk_env.get(key) else "ambient") if key == "CODEX_API_KEY"
                          else self._effective("github" if key == "GH_TOKEN" else "modal")[1]}
                         for key in _ENV_KEYS]
            return {"template": "".join(key + '=\"\"\n' for key in _ENV_KEYS), "variables": variables,
                    "storage": {**self.status()["storage"]}}

    def _effective(self, provider):
        if provider in self._session:
            return self._session[provider], "session"
        if provider in self._disk:
            return self._disk[provider], "private_file"
        return None, "ambient"

    def environment(self):
        """Fresh controller-only subprocess env. Never expose in HTTP or worker metadata."""
        with self._lock:
            env = {key: value for key, value in self._base_environment.items() if key not in _MODEL_KEYS}
            github, _ = self._effective("github")
            modal, _ = self._effective("modal")
            if github:
                env.update(GH_TOKEN=github["token"], GITHUB_TOKEN=github["token"])
            elif "GH_TOKEN" not in env and env.get("GITHUB_TOKEN"):
                env["GH_TOKEN"] = env["GITHUB_TOKEN"]
            if modal:
                for key in _MODAL_OAUTH:
                    env.pop(key, None)
                env.update(MODAL_TOKEN_ID=modal["token_id"], MODAL_TOKEN_SECRET=modal["token_secret"])
                # This child-only override avoids reading or changing unrelated
                # global profiles when the GUI supplies its own complete token pair.
                env["MODAL_CONFIG_PATH"] = os.devnull
            env.update(GH_PROMPT_DISABLED="1", GH_PAGER="cat", PAGER="cat", NO_COLOR="1")
            from .desktop_tools import local_github
            github_cli = local_github(self._workspace)
            if github_cli is not None:
                env["PATH"] = str(github_cli.parent) + os.pathsep + env.get("PATH", os.defpath)
            return env

    def _identity(self, value, forbidden=()):
        if (not isinstance(value, str) or not _IDENTITY.fullmatch(value)
                or value.startswith(("ghp_", "gho_", "github_pat_", "ak-", "as-", "sk-"))):
            return None
        secrets = self._known_credentials() | {value for value in forbidden if value}
        if any(secret and secret in value for secret in secrets):
            return None
        return value

    def status(self):
        """Read cached observations only. Configured does not mean authenticated."""
        with self._lock:
            result = {"schema_version": 1, "storage": {"persistent_available": self._storage_error is None,
                       "kind": "private_plaintext_env", "error_code": self._storage_error,
                       "path": str(self._env_path)}}
            for provider in ("github", "modal", "openai"):
                record, source = self._effective(provider)
                observation = record.get("observation", {}) if record else self._ambient_observations.get(provider, {})
                ambient_present = (provider == "github" and bool(self._base_environment.get("GH_TOKEN")
                                   or self._base_environment.get("GITHUB_TOKEN")) or provider == "modal"
                                   and bool(self._base_environment.get("MODAL_TOKEN_ID")
                                            and self._base_environment.get("MODAL_TOKEN_SECRET")))
                result[provider] = {"configured": bool(record or ambient_present or observation.get("verified")),
                                    "verified": observation.get("verified") is True,
                                    "source": source, "persisted": source == "private_file",
                                    "identity": self._identity(observation.get("identity")),
                                    "checked_at_utc": observation.get("checked_at_utc"),
                                    "error_code": observation.get("error_code"),
                                    "last_operation_uncertain": observation.get("last_operation_uncertain", False)}
                if provider == "github":
                    result[provider]["planning_credential_captured"] = bool(record and record.get("token")
                        or self._base_environment.get("GH_TOKEN") or self._base_environment.get("GITHUB_TOKEN"))
                if provider == "openai":
                    modal_status = result["modal"]
                    result[provider]["verified"] = (result[provider]["verified"] and modal_status["verified"]
                                                     and result[provider]["identity"] == modal_status["identity"])
                    result[provider].update(secret_name=self._identity(record.get("secret_name")) if record else None,
                                            environment=self._identity(record.get("environment")) if record else None,
                                            api_key_verified=False, model_access_verified=False,
                                            quota_verified=False, local_key_configured=bool(self._model_key()),
                                            local_key_source="session" if self._session_model_key else
                                            "private_file" if self._disk_env.get("CODEX_API_KEY") else None)
            return result

    def _configure(self, provider, values, persist):
        if type(persist) is not bool:
            raise ConnectionsError("invalid_option")
        record = {**values, "observation": _observation(checked=False)}
        with self._lock:
            if persist:
                self._persist(provider, record)
                self._session.pop(provider, None)
            else:
                self._session[provider] = record
        return self.verify(provider)

    def configure_github(self, token, persist=True):
        return self._configure("github", {"token": _credential(token)}, persist)

    def configure_modal(self, token_id, token_secret, persist=True):
        return self._configure("modal", {"token_id": _credential(token_id),
                                         "token_secret": _credential(token_secret)}, persist)

    def _call(self, provider, request=None):
        writing = bool(provider == "openai" and request and request.get("operation") == "openai_configure")
        if provider == "github":
            command = ["gh", "api", "--hostname", "github.com", "/user", "--jq", ".login"]
            data = None
        else:
            command = [sys.executable, "-m", "swarm_control.connection_worker"]
            data = json.dumps(request, allow_nan=False).encode()
        try:
            code, raw = self._runner(command, env=self.environment(), input=data, timeout=TIMEOUT_SECONDS)
            if code is None:
                return {"ok": False, "error_code": "provider_timeout", "uncertain": writing}
            if code != 0:
                return {"ok": False, "error_code": "github_auth_failed" if provider == "github"
                        else "helper_unavailable", "uncertain": writing}
            if len(raw) > MAX_OUTPUT_BYTES:
                raise ValueError
            if provider == "github":
                identity = self._identity(raw.decode().strip())
                return {"ok": identity is not None, "identity": identity,
                        "error_code": None if identity else "invalid_provider_response"}
            result = json.loads(raw)
            if (not isinstance(result, dict) or type(result.get("ok")) is not bool
                    or result.get("error_code") not in _CODES | {None}
                    or "uncertain" in result and type(result["uncertain"]) is not bool):
                raise ValueError
            if result["ok"] and provider == "openai" and result.get("secret_present") is not True:
                raise ValueError
            return {"ok": result["ok"], "identity": self._identity(result.get("identity"),
                                                                      (request.get("api_key"),)),
                    "error_code": result.get("error_code"), "uncertain": result.get("uncertain", False)}
        except Exception:  # noqa: BLE001 - External exception text is never a public response.
            return {"ok": False, "error_code": "invalid_provider_response", "uncertain": writing}

    def _finish(self, provider, snapshot, source, response, *, preserve_uncertain=False):
        observation = _observation(verified=response["ok"], identity=response.get("identity"),
                                   error_code=response.get("error_code"),
                                   uncertain=response.get("uncertain", False) or preserve_uncertain)
        with self._lock:
            if self._effective(provider) != (snapshot, source):
                return self.status()
            if snapshot is None:
                self._ambient_observations[provider] = observation
            else:
                record = {**snapshot, "observation": observation}
                if source == "private_file":
                    self._persist(provider, record, expected=snapshot)
                else:
                    self._session[provider] = record
            return self.status()

    def verify(self, provider):
        if provider not in _PROVIDERS:
            raise ConnectionsError("invalid_option")
        with self._lock:
            snapshot, source = copy.deepcopy(self._effective(provider))
            request = {"operation": "modal_verify"}
            if provider == "openai":
                if snapshot is None:
                    raise ConnectionsError("openai_not_configured")
                modal_status = self.status()["modal"]
                if not modal_status["verified"] or not modal_status["identity"]:
                    raise ConnectionsError("modal_not_verified")
                request = {"operation": "openai_verify", "secret_name": snapshot["secret_name"],
                           "environment": snapshot["environment"], "expected_workspace": modal_status["identity"]}
        response = self._call(provider, request)
        return self._finish(provider, snapshot, source, response,
                            preserve_uncertain=bool(snapshot and snapshot["observation"]["last_operation_uncertain"]))

    def configure_openai(self, api_key=None, secret_name=None, environment="main", replace=False, persist_key=False):
        with self._lock:
            api_key = _credential(api_key if api_key is not None else self._model_key())
        secret_name, environment = _name(secret_name), _name(environment)
        if type(replace) is not bool or type(persist_key) is not bool:
            raise ConnectionsError("invalid_option")
        with self._lock:
            modal_status = self.status()["modal"]
            if not modal_status["verified"] or not modal_status["identity"]:
                raise ConnectionsError("modal_not_verified")
            if api_key in secret_name or api_key in environment:
                raise ConnectionsError("invalid_name")
            if persist_key:
                self._set_environment({"CODEX_API_KEY": api_key}, True)
            record = {"secret_name": secret_name, "environment": environment,
                      "observation": _observation(checked=False)}
            if self._storage_error is None:
                self._persist("openai", record)
                self._session.pop("openai", None)
                source = "private_file"
            else:
                self._session["openai"] = record
                source = "session"
            request = {"operation": "openai_configure", "api_key": api_key,
                       "secret_name": secret_name, "environment": environment, "replace": replace,
                       "expected_workspace": modal_status["identity"]}
        response = self._call("openai", request)
        request.pop("api_key", None)
        api_key = None
        return self._finish("openai", record, source, response)
