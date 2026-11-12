"""Private repository registry, immutable run drafts and explicit CLI execution.

Planning and connection methods never deploy resources. Explicit launch uses the
same source, authorization and accounting gates from either CLI or local UI.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from pathlib import Path

from .dashboard_catalog import REPO, SECRET, identity, label

MAX_BYTES = 8 * 1024 * 1024
MAX_TASK_BYTES = 256 * 1024
_RUNNER_LOCK = threading.RLock()
_OPTIONS = {"task_ids", "workers", "timeout", "model_budget", "synchronized",
            "allow_overlap", "base", "label", "context_mode", "require_parallel", "no_progress_timeout"}
_STATES = {"draft", "historical", "launching", "runner_finished", "interrupted"}
_WORKER_STATES = frozenset({"starting", "preparing", "ready", "working", "checking", "recovering",
                            "validated", "needs_repair", "rejected", "failed", "interrupted",
                            "not_dispatched", "launch_failed", "needs_recovery"})


class ProjectConnectionError(ValueError):
    """An actionable, public-safe connection error; never contains local paths."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def default_workspace():
    return Path.home() / ".config" / "swarm-control" / "workspace.json"


def _require(value, message="Invalid workspace data"):
    if not value:
        raise ValueError(message)


def _now():
    return datetime.now(UTC).isoformat()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(path, maximum=MAX_BYTES):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result

    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    _require(len(raw) <= maximum, "Local workspace input is too large")
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON")))
    return value, raw


def _safe_text(value, fallback="Unavailable", maximum=160):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return fallback
    if SECRET.search(value) or re.search(r"(?:/home/|/Users/|/tmp/|/root/|[A-Za-z]:\\)", value):
        return fallback
    return value[:maximum]


def _git(project, *args):
    result = subprocess.run(["git", *args], cwd=project.root, capture_output=True, check=False)
    _require(result.returncode == 0, "Registered repository metadata is unavailable")
    return result.stdout


@contextmanager
def _runner_project(project):
    """Preflight's legacy globals must never cross repository HTTP requests."""
    from . import modal_runner
    with _RUNNER_LOCK:
        saved = {key: getattr(modal_runner, key) for key in ("ROOT", "REPOSITORY", "APP", "PROJECT")}
        try:
            modal_runner.configure(project)
            yield modal_runner
        finally:
            for key, value in saved.items():
                setattr(modal_runner, key, value)


def _options(value):
    _require(isinstance(value, dict) and not set(value) - _OPTIONS, "Unknown run option")
    result = {"workers": 1, "timeout": 300, "model_budget": None,
              "synchronized": False, "allow_overlap": False, "base": "HEAD",
              "label": "New run", "context_mode": "full", "require_parallel": None,
              "no_progress_timeout": None, **value}
    tasks = result.get("task_ids")
    _require(isinstance(tasks, list) and 1 <= len(tasks) <= 100,
             "Select between 1 and 100 tasks")
    _require(all(isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9_-]{1,39}", item)
                 for item in tasks) and len(set(tasks)) == len(tasks), "Invalid or duplicate task IDs")
    _require(type(result["workers"]) is int and 1 <= result["workers"] <= min(100, len(tasks)),
             "Agents must be between 1 and the number of selected tasks, at most 100")
    _require(type(result["timeout"]) is int and 30 <= result["timeout"] <= 1800,
             "Coding time must be between 30 and 1800 seconds")
    required = result["require_parallel"]
    if required is None and result["workers"] == 100:
        required = result["require_parallel"] = 100
    _require(required is None or type(required) is int and 1 <= required <= result["workers"],
             "Required independent tasks must be within the selected agent count")
    idle = result["no_progress_timeout"]
    _require(idle is None or type(idle) is int and 30 <= idle <= result["timeout"],
             "No-progress stop must be between 30 seconds and the coding time limit")
    _require(type(result["synchronized"]) is bool and type(result["allow_overlap"]) is bool,
             "Run flags must be booleans")
    _require(not result["synchronized"] or result["workers"] == len(tasks),
             "A synchronized run needs one task per agent")
    _require(isinstance(result["context_mode"], str) and result["context_mode"] in {"full", "task"},
             "Invalid context mode")
    base = result["base"]
    _require(isinstance(base, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./~^{}@-]{0,199}", base)
             and not SECRET.search(base), "Invalid base reference")
    result["label"] = label(result["label"])
    if result["model_budget"] is not None:
        _require(type(result["model_budget"]) in (int, float, str), "Invalid model reservation")
        _require(not isinstance(result["model_budget"], str) or len(result["model_budget"]) <= 64,
                 "Model reservation text is too long")
        try:
            amount = Decimal(str(result["model_budget"])) * 100
            _require(amount.is_finite() and amount == amount.to_integral_value()
                     and 0 < amount <= 18000, "Model reservation must use whole cents, up to USD180")
            result["model_budget"] = int(amount) / 100
        except DecimalException:
            raise ValueError("Invalid model reservation") from None
    return result


class Workspace:
    """A target-independent private catalog. Reading never creates local state."""

    def __init__(self, path=None):
        self.path = Path(path or default_workspace()).expanduser().absolute()

    def _check_path(self):
        _require(not self.path.is_symlink() and self.path.name != "control.sqlite3",
                 "Unsafe workspace file")
        _require(not self.path.exists() or self.path.is_file(), "Unsafe workspace file")

    def _read(self):
        self._check_path()
        if not self.path.exists():
            return {"schema_version": 1, "kind": "swarm_control_workspace",
                    "workspace_label": "Local workspace", "projects": []}
        try:
            value, _ = _json(self.path)
            self._validate(value)
            return value
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("Invalid or unavailable local workspace") from None

    @staticmethod
    def _validate(value):
        _require(isinstance(value, dict) and set(value) == {
            "schema_version", "kind", "workspace_label", "projects"})
        _require(type(value["schema_version"]) is int and value["schema_version"] == 1
                 and value["kind"] == "swarm_control_workspace")
        label(value["workspace_label"])
        _require(isinstance(value["projects"], list) and len(value["projects"]) <= 100)
        ids, runs = set(), 0
        for project in value["projects"]:
            _require(isinstance(project, dict) and set(project) == {
                "id", "label", "repository", "project_path", "runs"})
            project_id = identity(project["id"])
            _require(project_id not in ids)
            ids.add(project_id)
            label(project["label"])
            _require(isinstance(project["repository"], str) and REPO.fullmatch(project["repository"])
                     and not SECRET.search(project["repository"]))
            _require(project["project_path"] is None or isinstance(project["project_path"], str)
                     and 0 < len(project["project_path"]) <= 4096)
            _require(isinstance(project["runs"], list))
            run_ids = set()
            for run in project["runs"]:
                _require(isinstance(run, dict) and set(run) == {
                    "id", "label", "status", "created_at_utc", "started_at_utc", "finished_at_utc",
                    "launch_requested_at_utc", "options", "bindings", "preflight", "snapshot", "snapshot_sha256", "attempt"})
                _require(identity(run["id"]) not in run_ids)
                run_ids.add(run["id"])
                label(run["label"])
                _require(run["status"] in _STATES)
                if run["options"] is not None:
                    _options(run["options"])
                _require(isinstance(run["bindings"], dict) and isinstance(run["preflight"], dict))
                for key in ("created_at_utc", "started_at_utc", "finished_at_utc", "launch_requested_at_utc"):
                    if run[key] is not None:
                        _require(isinstance(run[key], str) and datetime.fromisoformat(run[key]).utcoffset() is not None)
                _require(run["snapshot"] is None or isinstance(run["snapshot"], str))
                _require(run["snapshot_sha256"] is None or isinstance(run["snapshot_sha256"], str)
                         and re.fullmatch(r"[a-f0-9]{64}", run["snapshot_sha256"]))
                _require(run["attempt"] is None or isinstance(run["attempt"], dict))
            runs += len(project["runs"])
        _require(runs <= 1000)

    @contextmanager
    def _write(self):
        self._check_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        _require(not lock_path.is_symlink(), "Unsafe workspace lock")
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = self._read()
            yield value
            self._validate(value)
            raw = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode()
            _require(len(raw) <= MAX_BYTES, "Workspace is full")
            temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _project(value, project_id):
        identity(project_id)
        found = next((p for p in value["projects"] if p["id"] == project_id), None)
        _require(found is not None, "Unknown registered project")
        return found

    @staticmethod
    def _run(project, run_id):
        identity(run_id)
        found = next((run for run in project["runs"] if run["id"] == run_id), None)
        _require(found is not None, "Unknown registered run")
        return found

    @staticmethod
    def _load_project(registered):
        from .project import load_project
        _require(registered["project_path"] is not None,
                 "This repository has historical evidence only; register its project configuration first")
        try:
            project = load_project(registered["project_path"])
            _require(project.repository == registered["repository"])
            project.verify_origin()
            return project
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
            raise ValueError("Registered project configuration or origin is unavailable") from None

    def initialize(self, workspace_label="Local workspace"):
        workspace_label = label(workspace_label)
        _require(not self.path.exists(), "Workspace already exists")
        with self._write() as value:
            _require(not self.path.exists(), "Workspace already exists")
            value["workspace_label"] = workspace_label
        return self.public_catalog()

    def add_project(self, project_id, project_path=None, *, label_text=None, repository=None):
        identity(project_id)
        project_label = label(label_text or project_id)
        path = None
        if project_path is not None:
            from .project import load_project
            project = load_project(project_path)
            project.verify_origin()
            _require(repository is None or repository == project.repository,
                     "Repository does not match the project configuration")
            repository, path = project.repository, str(project.path)
        _require(isinstance(repository, str) and REPO.fullmatch(repository)
                 and not SECRET.search(repository), "A GitHub owner/repository is required")
        with self._write() as value:
            existing = next((p for p in value["projects"] if p["id"] == project_id), None)
            if existing is not None:
                _require(existing["repository"] == repository and existing["project_path"] is None
                         and path is not None, "Project already registered")
                existing.update(project_path=path, label=project_label)
            else:
                value["projects"].append({"id": project_id, "label": project_label,
                    "repository": repository, "project_path": path, "runs": []})
        return self.project_context(project_id)

    def connect_project(self, path, *, label_text=None):
        """Connect one explicitly selected checkout, preserving its existing runs.

        A connection validates local configuration, origin and task metadata. It
        never runs target commands, clones a URL, edits the target or opens a ledger.
        """
        from .project import load_project
        if (not isinstance(path, (str, Path)) or not str(path).strip()
                or len(str(path)) > 4096 or any(ord(c) < 32 for c in str(path))
                or "://" in str(path) or str(path).startswith("git@")):
            raise ProjectConnectionError("local_path_required",
                "Choose the local folder where you cloned the repository, or its swarm.toml file. A GitHub URL is not a local folder.")
        selected = Path(path).expanduser()
        if selected.is_dir():
            selected = selected / "swarm.toml"
        if not selected.is_file():
            raise ProjectConnectionError("project_config_missing",
                "No project configuration was found. Clone the repository first and select its root folder containing swarm.toml. See the setup guide for an unconfigured project.")
        try:
            project = load_project(selected)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            raise ProjectConnectionError("project_config_invalid",
                "The project configuration is invalid. Check swarm.toml, README.md, AGENTS.md and the task directory using the project setup guide.") from None
        try:
            project.verify_origin()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ProjectConnectionError("project_origin_mismatch",
                "The Git origin must match project.repository in swarm.toml. Check the selected checkout and its GitHub owner/repository.") from None
        try:
            project_label = label(label_text) if label_text is not None else label(project.root.name)
        except ValueError:
            raise ProjectConnectionError("project_label_invalid",
                "Use a project name of 1 to 160 characters, without slashes or credentials.") from None
        slug = re.sub(r"[^a-z0-9_-]", "-", project.repository.lower())[:40]
        candidate = {"id": slug + "-" + _digest(str(project.path).encode())[:12],
                     "label": project_label, "repository": project.repository,
                     "project_path": str(project.path), "runs": []}
        # Validate tasks before changing the registry: a failed connection cannot
        # leave a broken selectable project behind.
        try:
            self._project_context(candidate)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError):
            raise ProjectConnectionError("project_tasks_invalid",
                "The project tasks could not be read. Check the JSON files in the configured task directory before connecting again.") from None
        with self._write() as value:
            existing = next((p for p in value["projects"] if p["project_path"] == str(project.path)), None)
            if existing is None:
                historical = [p for p in value["projects"] if p["project_path"] is None
                              and p["repository"] == project.repository]
                existing = historical[0] if len(historical) == 1 else None
            if existing is not None:
                _require(existing["repository"] == project.repository,
                         "The connected checkout no longer matches its registered repository")
                existing["project_path"] = str(project.path)
                if label_text is not None:
                    existing["label"] = project_label
                project_id = existing["id"]
            else:
                value["projects"].append(candidate)
                project_id = candidate["id"]
        return self.project_context(project_id)

    def public_catalog(self):
        value = self._read()
        projects = []
        for project in value["projects"]:
            projects.append({"id": project["id"], "label": project["label"],
                "repository": project["repository"], "configurable": project["project_path"] is not None,
                "runs": [{"id": r["id"], "label": r["label"], "status": r["status"],
                          "workers": r["options"]["workers"] if r["options"] else None,
                          "created_at_utc": r["created_at_utc"], "has_snapshot": bool(r["snapshot"])}
                         for r in reversed(project["runs"])]})
        first = projects[0] if projects else None
        return {"schema_version": 1, "mode": "workspace", "workspace_label": value["workspace_label"],
                "projects": projects, "default_project_id": first["id"] if first else None,
                "default_run_id": first["runs"][0]["id"] if first and first["runs"] else None}

    def project_context(self, project_id):
        registered = self._project(self._read(), project_id)
        return self._project_context(registered)

    def _project_context(self, registered):
        project_id = registered["id"]
        result = {"project_id": project_id, "label": registered["label"],
                  "repository": registered["repository"], "configurable": False,
                  "limits": {"max_workers": 0, "gateway_concurrency": 0},
                  "defaults": {"workers": 1, "timeout": 300, "base": "HEAD", "synchronized": False,
                               "allow_overlap": False, "context_mode": "full"},
                  "tasks": [], "live_execution_enabled": False, "authorizes_spend": False}
        if registered["project_path"] is None:
            return result
        project = self._load_project(registered)
        files = sorted(project.tasks_dir.glob("*.json"))
        _require(len(files) <= 1000, "Too many task definitions")
        rows = []
        with _runner_project(project) as runner:
            for filename in files:
                if not re.fullmatch(r"[A-Z][A-Z0-9_-]{1,39}", filename.stem):
                    continue
                _require(filename.stat().st_size <= MAX_TASK_BYTES, "Task definition is too large")
                task = runner.load_task(filename.stem)
                state = task.get("status", "ready")
                rows.append({"id": task["id"], "title": _safe_text(task["title"], task["id"]),
                             "status": state if state in {"ready", "blocked", "running", "merged", "failed"} else "unknown",
                             "dependencies": task.get("dependsOn", []),
                             "has_task_context": bool(task.get("context_files"))})
        result.update(configurable=True, tasks=rows, live_execution_enabled=project.live_enabled,
                      limits={"max_workers": project.max_live_agents, "max_tasks": project.max_live_agents,
                              "gateway_concurrency": project.gateway_concurrency})
        return result

    @staticmethod
    def _bindings(project, options, tasks, base_sha):
        from .workload import load_inventory

        # Hash unstaged/index changes and source identities; never persist customer source.
        diff = _git(project, "diff", "--binary", "HEAD", "--")
        status = _git(project, "status", "--porcelain", "--untracked-files=normal")
        inventory_identity = load_inventory(project)[1]
        return {"base_sha": base_sha,
                "dependency_inventory_sha256": inventory_identity["inventory_sha256"],
                "checkout_head": _git(project, "rev-parse", "HEAD").decode().strip(),
                "working_tree_sha256": _digest(diff + b"\0" + status),
                "project_sha256": _digest(project.path.read_bytes()),
                "task_sha256": {task["id"]: _digest((project.tasks_dir / (task["id"] + ".json")).read_bytes())
                                for task in tasks},
                "instructions_sha256": {name: _digest((project.root / name).read_bytes())
                                        for name in project.instructions},
                "controller_source_sha256": {name: _digest(Path(__file__).with_name(name).read_bytes())
                    for name in ("modal_runner.py", "modal_worker.py", "context_packet.py", "progress.py",
                                 "supervision.py", "dependencies.py", "workload.py")
                    if Path(__file__).with_name(name).is_file()},
                "options_sha256": _digest(json.dumps(options, sort_keys=True).encode())}

    @staticmethod
    def _preflight(project, options):
        from .preflight import build_preflight
        with _runner_project(project) as runner:
            tasks = [runner.load_task(task_id) for task_id in options["task_ids"]]
            _require(max(options["workers"], len(tasks)) <= project.max_live_agents,
                     "Selected agents or tasks exceed this project's configured capacity")
            if options["context_mode"] == "task":
                _require(all(task.get("context_files") for task in tasks),
                         "Task context mode needs context_files on every selected task")
            raw = build_preflight(project, options["task_ids"], workers=options["workers"],
                synchronized=options["synchronized"], base=options["base"], online=False,
                model_budget=options["model_budget"], allow_overlap=options["allow_overlap"],
                timeout=options["timeout"], context_mode=options["context_mode"],
                require_parallel=options.get("require_parallel"))
            if options["context_mode"] == "task":
                runner.build_context_packets(tasks, raw["base_sha"], options["context_mode"], timeout=options["timeout"])
            bindings = Workspace._bindings(project, options, tasks, raw["base_sha"])
        public = {key: raw[key] for key in (
            "preparation_checks_passed", "local_workload_ready", "local_admission_reserve_sufficient",
            "launch_ready", "local_workload_blockers", "local_admission_blockers", "launch_blockers",
            "proposed_model_reserve_cents", "configured_gateway_capacity_sufficient")}
        public["minimum_infrastructure_reserve_cents"] = raw["local_admission"]["minimum_infrastructure_reserve_cents"]
        remaining = raw["budget_status"]["local_admission_policy"]["unreserved_cents"]
        public["remaining_model_allowance_cents"] = remaining["model"] if remaining else None
        public["remaining_infrastructure_allowance_cents"] = remaining["infrastructure"] if remaining else None
        from .workload import public_analysis, public_context
        public["dependency_analysis"] = public_analysis(raw.get("dependency_analysis"))
        public["context_readiness"] = public_context(raw.get("context_readiness"))
        return public, bindings

    @staticmethod
    def _public_preflight(source):
        from .workload import public_analysis, public_context

        result = {}
        for key in ("preparation_checks_passed", "local_workload_ready", "local_admission_reserve_sufficient",
                    "configured_gateway_capacity_sufficient"):
            result[key] = source.get(key) if type(source.get(key)) is bool else None
        result["launch_ready"] = False
        for key in ("local_workload_blockers", "local_admission_blockers", "launch_blockers"):
            rows = source.get(key, [])
            result[key] = [value for value in rows[:100] if isinstance(value, str)
                           and re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", value) and not SECRET.search(value)] \
                if isinstance(rows, list) else []
        for key in ("proposed_model_reserve_cents", "minimum_infrastructure_reserve_cents",
                    "remaining_model_allowance_cents", "remaining_infrastructure_allowance_cents"):
            value = source.get(key)
            result[key] = value if type(value) is int and 0 <= value <= 100000000 else None
        result["dependency_analysis"] = public_analysis(source.get("dependency_analysis"))
        result["context_readiness"] = public_context(source.get("context_readiness"))
        return result

    @staticmethod
    def _public_run(project, run, *, workspace_path=None):
        options = run["options"] or {}
        attempt = run["attempt"] or {}
        outcomes = attempt.get("worker_outcomes", {})
        outcomes = outcomes if isinstance(outcomes, dict) else {}
        result = {"id": run["id"], "project_id": project["id"], "repository": project["repository"],
                  "label": run["label"], "status": run["status"],
                  "workers": options.get("workers"), "task_ids": options.get("task_ids", []),
                  "base_sha": run["bindings"].get("base_sha") if isinstance(run["bindings"].get("base_sha"), str)
                      and re.fullmatch(r"[a-f0-9]{40}", run["bindings"]["base_sha"]) else None,
                  "timeout": options.get("timeout"),
                  "require_parallel": options.get("require_parallel"),
                  "no_progress_timeout": options.get("no_progress_timeout"),
                  "model_budget": options.get("model_budget"),
                  "synchronized": options.get("synchronized"), "allow_overlap": options.get("allow_overlap"),
                  "context_mode": options.get("context_mode"),
                  "source_run_id": _safe_text(run["bindings"].get("source_run_id"), fallback=None),
                  "created_at_utc": run["created_at_utc"], "started_at_utc": run["started_at_utc"],
                  "launch_requested_at_utc": run["launch_requested_at_utc"],
                  "finished_at_utc": run["finished_at_utc"], "preflight": Workspace._public_preflight(run["preflight"]),
                  "has_snapshot": bool(run["snapshot"]), "actual_billed_cost_usd": None,
                  "authorizes_spend": False, "launch_ready": False,
                  "runner_exit_code": attempt.get("exit_code") if type(attempt.get("exit_code")) is int else None,
                  "worker_outcomes": {key: count for key, count in outcomes.items()
                      if key in {"validated", "needs_repair", "rejected", "failed", "interrupted", "not_dispatched",
                                 "launch_failed", "working", "preparing", "checking", "ready", "unknown"}
                      and type(count) is int and 0 <= count <= 100},
                  "launch_command": None}
        if run["status"] == "draft" and run["id"] is not None:
            command = ["swarm-control", "runs", "launch", project["id"], run["id"]]
            if workspace_path is not None and Path(workspace_path).absolute() != default_workspace().absolute():
                command.extend(["--workspace", "<workspace.json>"])
            result["launch_command"] = shlex.join(command)
        return result

    def prepare_run(self, project_id, options, *, save=False):
        options = _options(options)
        registered = self._project(self._read(), project_id)
        project = self._load_project(registered)
        readiness, bindings = self._preflight(project, options)
        run = {"id": "run-" + uuid.uuid4().hex[:16] if save else None,
               "label": options["label"], "status": "draft" if save else "preview",
               "created_at_utc": _now(), "started_at_utc": None, "finished_at_utc": None,
               "launch_requested_at_utc": None,
               "options": options, "bindings": bindings, "preflight": readiness,
               "snapshot": None, "snapshot_sha256": None, "attempt": None}
        if save:
            with self._write() as value:
                current = self._project(value, project_id)
                _require(current["project_path"] == registered["project_path"], "Registered project changed")
                current["runs"].append(run)
        return self._public_run(registered, run, workspace_path=self.path)

    def run_detail(self, project_id, run_id):
        registered = self._project(self._read(), project_id)
        run = self._run(registered, run_id)
        result = self._public_run(registered, run, workspace_path=self.path)
        result["execution"] = self._execution(registered, run)
        if result["started_at_utc"] is None and result["execution"] is not None:
            result["started_at_utc"] = result["execution"]["first_agent_started_at_utc"]
        return result

    def _execution(self, registered, run):
        """Read fixed report fields; observations never rewrite request history.

        A report is not a provider inventory or a merged delivery. Missing,
        malformed, oversized, foreign or unsafe reports remain unknown.
        """
        from .progress import PHASES
        if run["options"] is None or run["status"] not in {"launching", "runner_finished", "interrupted"}:
            return None
        try:
            project = self._load_project(registered)
            batch = self._batch(project, run)
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
            return None
        if batch is None:
            return None
        now = datetime.now(UTC)
        requested = run["launch_requested_at_utc"]
        requested = datetime.fromisoformat(requested).astimezone(UTC) if requested else None

        def observed_time(value):
            try:
                when = datetime.fromisoformat(value) if isinstance(value, str) else None
                if when is None or when.utcoffset() is None:
                    return None
                when = when.astimezone(UTC)
                return when if when <= now and (requested is None or when >= requested) else None
            except (ValueError, OverflowError):
                return None

        tasks = run["options"]["task_ids"]
        observed, outcomes, phases, starts, events = 0, {}, {}, [], []
        task_observations = []
        for task_id in tasks:
            path = batch / task_id / "report.json"
            report = None
            try:
                _require(not path.is_symlink() and path.resolve().is_relative_to(batch.resolve()))
                candidate, _ = _json(path, MAX_TASK_BYTES)
                _require(isinstance(candidate, dict) and candidate.get("task_id") == task_id
                         and candidate.get("run_id") == run["id"]
                         and candidate.get("base_sha") == run["bindings"]["base_sha"])
                status = candidate.get("status")
                _require(isinstance(status, str) and status in _WORKER_STATES)
                report = candidate
            except (OSError, ValueError, TypeError, RecursionError):
                status = "unknown"
            outcomes[status] = outcomes.get(status, 0) + 1
            row = {"task_id": task_id, "status": status, "phase": None,
                   "terminal_reason": None, "source_change_count": None,
                   "first_source_change_seconds": None, "file_change_signals": None,
                   "last_event_at_utc": None, "recovered_candidate_available": False,
                   "useful_progress_verified": False}
            task_observations.append(row)
            if report is None:
                continue
            observed += 1
            supervisor = report.get("supervision")
            if isinstance(supervisor, dict) and supervisor.get("source") == "worker_supervisor_observed":
                reason = supervisor.get("terminal_reason")
                if isinstance(reason, str) and reason in {"child_exited", "coding_deadline_reached", "configured_no_source_progress",
                              "supervisor_interrupted", "spawn_failed"}:
                    row["terminal_reason"] = reason
                count = supervisor.get("source_change_count")
                if type(count) is int and 0 <= count <= 1000000:
                    row["source_change_count"] = count
                first = supervisor.get("first_source_change_seconds")
                if type(first) in (int, float) and 0 <= first <= run["options"]["timeout"] + 60:
                    row["first_source_change_seconds"] = first
            recovered = report.get("recovered_candidate")
            if (isinstance(recovered, dict) and recovered.get("status") == "candidate"
                    and recovered.get("requires_independent_validation") is True
                    and recovered.get("base_sha") == run["bindings"]["base_sha"]
                    and isinstance(recovered.get("patch_sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", recovered["patch_sha256"])
                    and recovered["patch_sha256"] == report.get("patch_sha256")):
                row["recovered_candidate_available"] = True
            started = observed_time(report.get("agent_started_at"))
            if started is not None:
                starts.append(started)
            progress = report.get("progress")
            if not isinstance(progress, dict):
                continue
            last_event = observed_time(progress.get("last_event_received_at_utc"))
            if last_event is not None:
                events.append(last_event)
                row["last_event_at_utc"] = last_event.isoformat()
            count = progress.get("file_change_signals")
            if type(count) is int and 0 <= count <= 1000000:
                row["file_change_signals"] = count
            rows = progress.get("phases")
            if not isinstance(rows, list) or len(rows) > 32:
                continue
            for phase in reversed(rows):
                if (isinstance(phase, dict) and isinstance(phase.get("phase"), str)
                        and phase["phase"] in PHASES and observed_time(phase.get("started_at_utc")) is not None):
                    phases[phase["phase"]] = phases.get(phase["phase"], 0) + 1
                    row["phase"] = phase["phase"]
                    break
        return {"source": "local_worker_reports", "observed_at_utc": now.isoformat(),
                "observation_kind": "local_report_read_not_provider_inventory",
                "expected_tasks": len(tasks), "observed_tasks": observed,
                "missing_tasks": len(tasks) - observed, "worker_outcomes": outcomes,
                "phase_counts": phases, "phase_counts_kind": "last_reported_phase_not_active_resources",
                "task_observations": task_observations,
                "first_agent_started_at_utc": min(starts).isoformat() if starts else None,
                "last_reported_event_at_utc": max(events).isoformat() if events else None,
                "merged_delivery_verified": False, "actual_billed_cost_usd": None}

    def historical_snapshot(self, project_id=None, run_id=None):
        value = self._read()
        if not value["projects"]:
            _require(project_id is None and run_id is None, "Unknown registered project")
            return None, "", None, None
        registered = self._project(value, project_id or value["projects"][0]["id"])
        if not registered["runs"]:
            _require(run_id is None, "Unknown registered run")
            return None, registered["repository"], registered["id"], None
        run = self._run(registered, run_id or registered["runs"][-1]["id"])
        path = Path(run["snapshot"]) if run["snapshot"] else None
        if path:
            _require(not path.is_symlink() and path.is_file(), "Historical snapshot has changed")
            source, raw = _json(path)
            _require(_digest(raw) == run["snapshot_sha256"], "Historical snapshot has changed")
            _require(source.get("repository") == registered["repository"]
                     and source.get("run_id") == run["bindings"].get("source_run_id"),
                     "Historical source binding differs")
        return path, registered["repository"], registered["id"], run["id"]

    def import_snapshot(self, project_id, snapshot_path, *, run_id=None, label_text="Historical run"):
        from .dashboard import project_snapshot
        registered = self._project(self._read(), project_id)
        _require(not Path(snapshot_path).is_symlink(), "Snapshot must be an explicit curated file")
        source, raw = _json(snapshot_path)
        _require(isinstance(source, dict) and source.get("repository") == registered["repository"],
                 "Snapshot belongs to a different repository")
        projected = project_snapshot(source, _digest(raw), datetime.now(UTC))
        _require(projected.get("available") is True, "Snapshot is not valid curated evidence")
        run_id = identity(run_id or "history-" + uuid.uuid4().hex[:16])
        run_label = label(label_text)
        with self._write() as value:
            current = self._project(value, project_id)
            _require(not any(r["id"] == run_id for r in current["runs"]), "Run already registered")
            directory = self.path.parent / (self.path.name + ".history") / project_id
            _require(not directory.is_symlink() and not directory.parent.is_symlink(), "Unsafe history directory")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination = directory / (run_id + ".json")
            with destination.open("xb") as stream:
                os.chmod(destination, 0o600)
                stream.write(raw)
            current["runs"].append({"id": run_id, "label": run_label, "status": "historical",
                "created_at_utc": _now(), "started_at_utc": None, "finished_at_utc": None,
                "launch_requested_at_utc": None,
                "options": None, "bindings": {"base_sha": None, "source_run_id": source.get("run_id")}, "preflight": {},
                "snapshot": str(destination), "snapshot_sha256": _digest(raw), "attempt": None})
        return self.run_detail(project_id, run_id)

    def import_catalog(self, catalog_path):
        """Copy curated legacy sources into private immutable workspace history."""
        from .dashboard_catalog import load_catalog
        catalog = load_catalog(catalog_path)
        result = []
        for registered in catalog.public()["projects"]:
            existing = self._read()
            match = next((p for p in existing["projects"] if p["id"] == registered["id"]), None)
            if match is None:
                self.add_project(registered["id"], label_text=registered["label"], repository=registered["repository"])
            else:
                _require(match["repository"] == registered["repository"], "Catalog project differs")
            for run in registered["runs"]:
                snapshot, _, _, _ = catalog.resolve(registered["id"], run["id"])
                result.append(self.import_snapshot(registered["id"], snapshot,
                              run_id=run["id"], label_text=run["label"]))
        return result

    @staticmethod
    def _batch(project, run):
        from .pilot_state import common_state
        directory = common_state(project.root) / "modal-runs"
        if not directory.is_dir():
            return None
        candidates = sorted(directory.glob("batch-*/plan.json"))
        _require(len(candidates) <= 10000, "Too many batch records to reconcile")
        found = []
        for path in candidates:
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                continue
            try:
                data, _ = _json(path, MAX_TASK_BYTES)
                if data.get("run_id") != run["id"]:
                    continue
                _require(data.get("repository") == project.repository
                         and data.get("base_sha") == run["bindings"]["base_sha"]
                         and data.get("tasks") == run["options"]["task_ids"], "Run batch binding differs")
                found.append(path.parent)
            except (OSError, ValueError, AttributeError, RecursionError):
                continue
        _require(len(found) <= 1, "Multiple batches claim this run; inspect private evidence")
        return found[0] if found else None

    def launch(self, project_id, run_id, *, gateway_url=None, execute=False):
        """Explicit launch; all existing worker and financial gates still apply."""
        from .operations import OperationsJournal
        from .pilot_state import common_state
        registered = self._project(self._read(), project_id)
        run = self._run(registered, run_id)
        _require(run["status"] == "draft" and run["attempt"] is None,
                 "This run has already been attempted or is historical; preserve it and create a new draft")
        if not execute:
            return {**self._public_run(registered, run, workspace_path=self.path), "execution_requested": False}
        project = self._load_project(registered)
        project.require_live()
        readiness, current = self._preflight(project, run["options"])
        _require(current == run["bindings"], "Draft source or configuration changed; create a new reviewed draft")
        _require(readiness["local_workload_ready"], "Run source preparation is blocked; inspect its preview")
        known_budget = set(readiness["local_admission_blockers"]) - {"durable_unreserved_allowance_not_observed"}
        _require(not known_budget, "Run admission is blocked; inspect existing allowance without resetting it")
        _require(run["options"]["model_budget"] is not None, "An explicit total model reservation is required")
        OperationsJournal(common_state(project.root) / "operations.sqlite3").require_admission(project.failure_policy)
        _require(isinstance(gateway_url, str) and re.fullmatch(r"https://[A-Za-z0-9-]+\.modal\.run/?", gateway_url),
                 "An explicit deployed gateway HTTPS origin is required")
        attempt = {"id": uuid.uuid4().hex, "status": "launching", "launch_requested_at_utc": _now(),
                   "finished_at_utc": None, "exit_code": None, "batch_id": None, "worker_outcomes": {}}
        with self._write() as value:
            stored = self._run(self._project(value, project_id), run_id)
            _require(stored["status"] == "draft" and stored["attempt"] is None, "Run launch was already reserved")
            stored.update(status="launching", launch_requested_at_utc=attempt["launch_requested_at_utc"], attempt=attempt)
        options = run["options"]
        argv = [sys.executable, "-m", "swarm_control", "run", *options["task_ids"],
                "--project", str(project.path), "--workers", str(options["workers"]),
                "--base", run["bindings"]["base_sha"], "--timeout", str(options["timeout"]),
                "--model", "gpt-6.1-sol", "--model-budget", str(options["model_budget"]),
                "--gateway-url", gateway_url, "--run-id", run_id,
                "--context-mode", options["context_mode"]]
        for key, flag in (("synchronized", "--synchronized"), ("allow_overlap", "--allow-overlap")):
            if options[key]:
                argv.append(flag)
        for key, flag in (("require_parallel", "--require-parallel"),
                          ("no_progress_timeout", "--no-progress-timeout")):
            if options.get(key) is not None:
                argv.extend([flag, str(options[key])])
        try:
            process = subprocess.run(argv, cwd=Path(__file__).resolve().parent.parent, check=False)
            attempt.update(status="runner_finished", exit_code=process.returncode)
        except BaseException:
            attempt["status"] = "interrupted"
            raise
        finally:
            attempt["finished_at_utc"] = _now()
            try:
                batch = self._batch(project, run)
            except (OSError, ValueError, subprocess.SubprocessError):
                batch = None
                attempt["batch_observation"] = "unavailable"
            actual_starts = []
            if batch:
                attempt["batch_id"] = batch.name
                for task_id in options["task_ids"]:
                    try:
                        report, _ = _json(batch / task_id / "report.json", MAX_TASK_BYTES)
                        at = report.get("agent_started_at")
                        if isinstance(at, str):
                            try:
                                when = datetime.fromisoformat(at)
                                if when.utcoffset() is not None:
                                    actual_starts.append(when.astimezone(UTC).isoformat())
                            except ValueError:
                                pass
                        outcome = report.get("status")
                        if outcome not in {"validated", "needs_repair", "needs_recovery", "rejected", "failed", "interrupted",
                                           "not_dispatched", "launch_failed", "working", "preparing", "checking", "ready"}:
                            outcome = "unknown"
                        attempt["worker_outcomes"][outcome] = attempt["worker_outcomes"].get(outcome, 0) + 1
                    except (OSError, ValueError, AttributeError):
                        attempt["worker_outcomes"]["unknown"] = attempt["worker_outcomes"].get("unknown", 0) + 1
            with self._write() as value:
                stored = self._run(self._project(value, project_id), run_id)
                _require(stored["attempt"]["id"] == attempt["id"], "Launch attempt binding changed")
                stored.update(status=attempt["status"], finished_at_utc=attempt["finished_at_utc"],
                              started_at_utc=min(actual_starts) if actual_starts else None, attempt=attempt)
        return self.run_detail(project_id, run_id)


def _parser(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--workspace", type=Path, default=default_workspace())
    return parser


def workspace_main():
    parser = _parser("Register local repositories and preserve curated run history; no cloud resources")
    parser.add_argument("command", choices=["init", "add", "list", "import-snapshot", "import-catalog"])
    parser.add_argument("items", nargs="*")
    parser.add_argument("--id")
    parser.add_argument("--label")
    parser.add_argument("--repository")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    workspace = Workspace(args.workspace)
    if args.command == "init":
        _require(not args.items, "init does not accept positional arguments")
        result = workspace.initialize(args.label or "Local workspace")
    elif args.command == "list":
        _require(not args.items, "list does not accept positional arguments")
        result = workspace.public_catalog()
    elif args.command == "add":
        _require(len(args.items) <= 1 and args.id, "add requires --id and a project path, or --repository for history only")
        result = workspace.add_project(args.id, args.items[0] if args.items else None,
                                       label_text=args.label, repository=args.repository)
    elif args.command == "import-catalog":
        _require(len(args.items) == 1, "import-catalog needs one legacy catalog file")
        result = workspace.import_catalog(args.items[0])
    else:
        _require(len(args.items) == 2, "import-snapshot needs a project ID and curated snapshot file")
        result = workspace.import_snapshot(args.items[0], args.items[1], run_id=args.run_id,
                                           label_text=args.label or "Historical run")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def runs_main():
    parser = _parser("Prepare immutable runs, inspect history and explicitly launch through ordinary controller gates")
    parser.add_argument("command", choices=["plan", "create", "list", "show", "launch"])
    parser.add_argument("project")
    parser.add_argument("items", nargs="*")
    parser.add_argument("--agents", "--workers", dest="workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--model-budget")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--label", default="New run")
    parser.add_argument("--context-mode", choices=["full", "task"], default="full")
    parser.add_argument("--require-parallel", type=int)
    parser.add_argument("--no-progress-timeout", type=int)
    parser.add_argument("--synchronized", action="store_true")
    parser.add_argument("--allow-overlap", action="store_true")
    parser.add_argument("--gateway-url")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    workspace = Workspace(args.workspace)
    if args.command in {"plan", "create"}:
        _require(not args.execute, "Only launch accepts --execute")
        result = workspace.prepare_run(args.project, {"task_ids": args.items, "workers": args.workers,
            "timeout": args.timeout, "model_budget": args.model_budget, "base": args.base, "label": args.label,
            "context_mode": args.context_mode, "synchronized": args.synchronized,
            "allow_overlap": args.allow_overlap, "require_parallel": args.require_parallel,
            "no_progress_timeout": args.no_progress_timeout}, save=args.command == "create")
    elif args.command == "list":
        _require(not args.items and not args.execute, "list accepts no run ID or execution")
        project = workspace._project(workspace._read(), args.project)
        result = [workspace._public_run(project, run, workspace_path=workspace.path) for run in reversed(project["runs"])]
    elif args.command == "show":
        _require(len(args.items) == 1 and not args.execute, "show requires one run ID")
        result = workspace.run_detail(args.project, args.items[0])
    else:
        _require(len(args.items) == 1, "launch requires one immutable draft ID")
        result = workspace.launch(args.project, args.items[0], gateway_url=args.gateway_url, execute=args.execute)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if args.command != "launch" or not args.execute or result.get("runner_exit_code") == 0 else 1
