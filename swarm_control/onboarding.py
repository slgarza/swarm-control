"""Explicit first-use operations shared by the local browser and CLI."""

import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .local_commands import execute, redact, validation_environment
from .project import load_project
from .setup_common import SetupError, private_json, require
from .setup_common import project_path as _path

ACTIONS = {
    "github.configure", "modal.configure", "connections.verify", "openai.configure", "environment.configure",
    "repository.inspect", "repository.clone", "repository.retarget", "repository.connect", "tools.install-github", "project.initialize", "tasks.import", "tasks.save",
    "tasks.create-issue", "preparation.preview", "preparation.publish", "project.configure-budget",
    "project.check", "project.deploy", "project.probe", "cli.preview", "cli.execute",
}
SECRET_FIELDS = {"token", "token_id", "token_secret", "api_key"}
CONTROLLER = Path(__file__).resolve().parent.parent


class Onboarding:
    def __init__(self, workspace, *, connections=None, repositories=None):
        from .connections import Connections
        from .repository_setup import RepositorySetup
        self.workspace = workspace
        self.connections = connections or Connections(workspace.path)
        workspace.connections = self.connections
        self.repositories = repositories
        self.repository_type = RepositorySetup
        self.lock = threading.RLock()
        self.thread = None
        self.jobs = {}
        self.previews = {}
        self.root = workspace.path.with_name(workspace.path.stem + ".setup-jobs")

    def _repository(self):
        return self.repositories or self.repository_type(self.workspace, environment=self.connections.environment())

    @property
    def busy(self):
        with self.lock:
            return self.thread is not None and self.thread.is_alive()

    def status(self):
        with self.lock:
            rows = {key: copy.deepcopy(value) for key, value in list(self.jobs.items())[-30:]}
        if self.root.is_dir() and not self.root.is_symlink():
            directories = [path for path in self.root.iterdir() if re.fullmatch(r"[a-f0-9]{32}", path.name)]
            require(len(directories) <= 10000, "Archive old setup history before starting more operations.")
            for path in sorted(directories, key=lambda item: item.stat().st_mtime, reverse=True)[:30]:
                if path.name not in rows:
                    rows[path.name] = self.job(path.name)
        summaries = []
        for value in rows.values():
            summaries.append({key: value.get(key) for key in ("id", "action", "status", "error", "error_code",
                "started_at_utc", "finished_at_utc", "elapsed_seconds", "automatic_retries")})
        from .desktop_tools import status as tools_status
        return {"schema_version": 1, "connections": self.connections.status(), "tools": tools_status(self.workspace.path),
                "operations": summaries[-30:], "busy": self.busy,
                "capabilities": {"actions": sorted(ACTIONS), "worker_capacity": 100,
                    "arbitrary_shell": False, "automatic_ai_planning": False,
                    "credentials_entered_only_in_explicit_connection_forms": True,
                    "cli_commands": ["plan", "analyze", "preflight", "budget", "ops", "integrate", "integrate-queue", "probe", "deploy-gateway"]}}

    def job(self, job_id):
        require(isinstance(job_id, str) and re.fullmatch(r"[a-f0-9]{32}", job_id))
        with self.lock:
            if job_id in self.jobs:
                return copy.deepcopy(self.jobs[job_id])
        # After a restart, a reservation is never silently rerun. A completed
        # outcome remains available; an incomplete original is uncertain.
        directory = self.root / job_id
        require(directory.is_dir() and not directory.is_symlink(), "Unknown setup operation.")
        for filename in ("outcome.json", "request.json"):
            path = directory / filename
            if path.is_file() and not path.is_symlink() and path.stat().st_size <= 4 * 1024 * 1024:
                value = json.loads(path.read_bytes())
                require(isinstance(value, dict) and value.get("id") == job_id and value.get("action") in ACTIONS,
                        "The setup operation record is invalid.")
                if filename == "outcome.json":
                    require(value.get("status") in {"finished", "failed", "uncertain"}, "The setup operation record is incomplete.")
                    return self._scrub({key: value.get(key) for key in ("id", "action", "status", "result", "error", "error_code",
                        "automatic_retries", "started_at_utc", "finished_at_utc", "elapsed_seconds")})
                return {"id": job_id, "action": value["action"], "status": "uncertain",
                        "result": None, "error": "The controller stopped before recording this operation. Inspect its result before requesting another action.",
                        "error_code": "interrupted_setup", "automatic_retries": 0}
        raise SetupError("setup_evidence_unavailable", "Setup history is unavailable. Inspect local storage before retrying.")

    def start(self, action, options, confirmed, request_id):
        require(action in ACTIONS, "Choose a supported setup action.")
        require(isinstance(options, dict) and len(options) <= 30)
        require(confirmed is True, "Confirm the displayed operation before continuing.", "confirmation_required")
        require(isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id),
                "A fresh setup request identity is required.")
        with self.lock:
            if request_id in self.jobs or (self.root / request_id).exists():
                original = self.job(request_id)
                require(original["action"] == action, "This request identity belongs to another operation.")
                return original
            require(not self.busy, "Another setup operation is running. Wait for its result.", "setup_busy")
            require(not self.root.is_symlink() and self.root.resolve() == self.root,
                    "The private setup history directory is unsafe.")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.root, 0o700)
            directory = self.root / request_id
            directory.mkdir(mode=0o700)
            # Connection values and task/source content never enter this audit.
            request = {"id": request_id, "action": action, "confirmed": True,
                       "requested_at_utc": datetime.now(UTC).isoformat(), "automatic_retries": 0,
                       "model_limit_cents": options.get("model_limit_cents") if type(options.get("model_limit_cents")) is int else None,
                       "infra_limit_cents": options.get("infra_limit_cents") if type(options.get("infra_limit_cents")) is int else None,
                       "max_live_agents": options.get("max_live_agents") if type(options.get("max_live_agents")) is int else None,
                       "live_enabled": options.get("live_enabled") if type(options.get("live_enabled")) is bool else None,
                       "cost_usd": None}
            request["intent"] = {key: self._scrub(value) for key in
                ("path", "destination", "repository", "owner", "secret_name", "environment")
                if isinstance(value := options.get(key), str) and len(value) <= 4096}
            private_json(directory / "request.json", request)
            if action in {"github.configure", "modal.configure", "openai.configure", "environment.configure"}:
                # An exact source preview must never be reused with a newly
                # selected account or credential set, even for the same repo.
                self.previews.clear()
            if len(self.jobs) >= 100:
                self.jobs.pop(next(iter(self.jobs)))
            job = {"id": request_id, "action": action, "status": "running", "result": None,
                   "error": None, "error_code": None, "automatic_retries": 0,
                   "started_at_utc": request["requested_at_utc"]}
            self.jobs[request_id] = job
            self.thread = threading.Thread(target=self._run, args=(job, dict(options), directory),
                                           name="swarm-setup", daemon=False)
            self.thread.start()
            return dict(job)

    def _run(self, job, options, directory):
        started = time.monotonic()
        try:
            result = self._scrub(self.perform(job["action"], options, directory))
            encoded = json.dumps(result, allow_nan=False)
            require(len(encoded.encode()) <= 4 * 1024 * 1024, "The result is too large. Select fewer items.")
            with self.lock:
                job.update(status="finished", result=result)
        except (Exception, SystemExit) as error:  # noqa: BLE001 - terminal job boundary
            # External exception text can contain credentials or provider output.
            from .connections import ConnectionsError
            from .repository_setup import RepositorySetupError
            safe = isinstance(error, (SetupError, ConnectionsError, RepositorySetupError))
            with self.lock:
                job.update(status="failed", error=str(error) if safe else
                    "The operation could not complete. Check the selected configuration and recorded result before retrying; remote changes may already exist.",
                    error_code=error.code if safe else "setup_operation_failed")
        finally:
            options.clear()
            with self.lock:
                job.update(finished_at_utc=datetime.now(UTC).isoformat(), elapsed_seconds=time.monotonic() - started)
                outcome = dict(job)
            try:
                private_json(directory / "outcome.json", outcome)
            except OSError:
                with self.lock:
                    job.update(status="uncertain", error="The result could not be saved. Inspect the operation before taking another action.",
                               error_code="setup_evidence_unavailable", result=None)

    def wait(self):
        thread = self.thread
        if thread is not None:
            thread.join()

    def _scrub(self, value):
        if isinstance(value, str):
            sanitizer = getattr(self.connections, "redact", None)
            return sanitizer(value) if sanitizer else redact(value, self.connections.environment())
        if isinstance(value, dict):
            return {key: self._scrub(item) for key, item in value.items() if key not in SECRET_FIELDS and key != "dotenv_text"}
        if isinstance(value, (tuple, list)):
            return [self._scrub(item) for item in value]
        return value

    def perform(self, action, options, directory):
        if action == "github.configure":
            return self.connections.configure_github(**options)
        if action == "modal.configure":
            return self.connections.configure_modal(**options)
        if action == "connections.verify":
            return self.connections.verify(**options)
        if action == "openai.configure":
            return self.connections.configure_openai(**options)
        if action == "environment.configure":
            return self.connections.configure_environment(**options)
        if action == "tools.install-github":
            from .desktop_tools import install_github
            require(not options)
            return install_github(self.workspace.path)
        repository = self._repository()
        if action == "repository.inspect":
            return repository.inspect(**options)
        if action == "repository.clone":
            return repository.clone(**options)
        if action == "repository.retarget":
            result = repository.retarget(**options, confirmed=True)
            self._connect(options["path"], result)
            return result
        if action == "repository.connect":
            require(set(options) == {"path"})
            result = repository.inspect(options["path"])
            self._connect(options["path"], result)
            return result
        if action == "project.initialize":
            result = repository.initialize(**options)
            self._connect(options["path"], result)
            return result
        if action == "tasks.import":
            return repository.import_issues(**options)
        if action == "tasks.create-issue":
            return repository.create_issue(**options, confirmed=True)
        if action == "tasks.save":
            result = repository.save_task(**options)
            self._connect(options["path"], result)
            return result
        if action == "preparation.preview":
            return repository.preparation_preview(**options)
        if action == "preparation.publish":
            return repository.publish_preparation(**options, confirmed=True)
        if action == "project.configure-budget":
            from .gui_budget import configure_budget
            return configure_budget(repository, directory=directory, **options)
        if action == "project.check":
            project = load_project(_path(options.get("path")))
            require(set(options) == {"path"})
            env = validation_environment(self.connections.environment())
            results = []
            for command in project.install + project.checks:
                result = execute(command, cwd=project.root, environment=env, timeout=1200)
                result = self._scrub(result)
                results.append({"argv": command, "exit_code": result["exit_code"],
                                "output": result["output"][-16000:], "stop_reason": result["stop_reason"]})
                private_json(directory / f"check-{len(results)}.json", result)
                if result["exit_code"] != 0:
                    break
            return {"checks_passed": all(row["exit_code"] == 0 for row in results), "commands": results,
                    "independent_product_acceptance": False}
        if action in {"project.deploy", "project.probe"}:
            project = load_project(_path(options.get("path")))
            project.require_live()
            require(set(options) <= {"path", "gateway_url"})
            args = ["deploy-gateway" if action == "project.deploy" else "probe", "--project", str(project.path)]
            if action == "project.probe":
                gateway = options.get("gateway_url")
                require(isinstance(gateway, str) and re.fullmatch(r"https://[A-Za-z0-9-]+\.modal\.run/?", gateway),
                        "Enter your verified Modal gateway HTTPS origin.")
                args.extend(["--gateway-url", gateway])
            result = self._execute_cli(args, directory)
            if action == "project.deploy" and result["exit_code"] == 0:
                # Lookup uses operator credentials but returns only the public URL.
                code = "import json,modal,sys; a=json.loads(sys.stdin.read()); print(modal.Function.from_name(a[0], 'gateway', environment_name=a[1]).get_web_url())"
                lookup = subprocess.run([sys.executable, "-c", code], cwd=CONTROLLER,
                    input=json.dumps([project.gateway_app, project.environment]).encode(), capture_output=True,
                    env=self.connections.environment(), timeout=30, check=False)
                candidate = lookup.stdout.decode("utf-8", "replace").strip()
                if re.fullmatch(r"https://[A-Za-z0-9-]+\.modal\.run/?", candidate):
                    result["gateway_url"] = candidate
            return result
        if action == "cli.preview":
            return self.preview_cli(**options)
        if action == "cli.execute":
            return self.execute_cli(**options, directory=directory)
        raise SetupError("invalid_setup", "Choose a supported setup action.")

    def _connect(self, path, result):
        result["connection"] = self.workspace.connect_project(path)

    def _command(self, command):
        require(isinstance(command, str) and 0 < len(command) <= 4096)
        require(not re.search(r"[;|&<>`\x00]|\$\(", command), "Use one swarm-control command; shell operators are unavailable.")
        args = shlex.split(command)
        if args and args[0] == "swarm-control":
            args.pop(0)
        require(0 < len(args) <= 128 and args[0] in {
            "plan", "analyze", "preflight", "budget", "ops", "integrate", "integrate-queue", "probe", "deploy-gateway"},
            "Choose a supported controller command. Use the run builder to launch agents.")
        require(redact(command) == command and not any("api-key" in arg.lower() or "token-secret" in arg.lower() for arg in args),
                "Configure credentials in Accounts; do not put keys in commands.")
        require(not re.search(r"(?:GH|GITHUB|MODAL|CODEX|OPENAI)_(?:TOKEN|API|SECRET)", command, re.IGNORECASE),
                "Configure credentials in Accounts; do not put keys in commands.")
        if args[0] == "budget":
            require(len(args) > 1 and args[1] in {"status", "inspect", "plan"},
                    "Use the budget form for reviewed policy changes.")
        if args[0] == "ops":
            require(len(args) > 1 and args[1] in {"status", "inspect-ci", "advise-scale", "queue-plan"},
                    "Operations holds require their separate reviewed workflow.")
        if "--help" in args or "-h" in args:
            return args, None
        require(args.count("--project") == 1 and args.index("--project") + 1 < len(args),
                "Include --project and the local swarm.toml path.")
        index = args.index("--project") + 1
        project = load_project(_path(args[index]))
        project.verify_origin()
        args[index] = str(project.path)
        artifact_index = 2 if args[0] == "integrate" else 1 if args[0] == "integrate-queue" else None
        if artifact_index is not None:
            require(len(args) > artifact_index and not args[artifact_index].startswith("-"), "Select the artifact directory or queue manifest.")
            artifact = Path(args[artifact_index]).expanduser()
            args[artifact_index] = str((CONTROLLER / artifact).resolve() if not artifact.is_absolute() else artifact.resolve())
        return args, project

    @staticmethod
    def _binding(args, project):
        value = {"args": args, "controller": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for name in ("onboarding.py", "modal_runner.py", "pilot_integrate.py", "budget_policy.py")
                  if (path := Path(__file__).with_name(name)).is_file()}}
        if project:
            from .workload import load_inventory
            env = validation_environment(dict(os.environ))
            env.update(GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
            def git(*arguments, maximum=8 * 1024 * 1024):
                result = execute(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                                  "-c", "core.attributesFile=/dev/null", *arguments], cwd=project.root, environment=env,
                                 timeout=10, maximum=maximum, capture_bytes=True)
                require(result["exit_code"] == 0 and not result["output_truncated"],
                        "The source review exceeds its bounded size or deadline. Commit or reduce the changes before previewing.")
                return result["bytes"]
            head = git("rev-parse", "HEAD", maximum=1024)
            diff = git("diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--")
            other = git("ls-files", "--others", "--exclude-standard", "-z", maximum=1024 * 1024)
            source_files = {}
            total = 0
            paths = [project.root / name for name in project.instructions]
            paths.extend(project.root / name.decode("utf-8") for name in other.split(b"\0") if name)
            for path in paths:
                require(path.is_symlink() or path.resolve().is_relative_to(project.root), "A source input leaves the repository.")
                if path.is_symlink():
                    raw = os.readlink(path).encode()
                else:
                    require(path.is_file() and path.stat().st_size <= 8 * 1024 * 1024, "A source input exceeds the review size limit.")
                    with path.open("rb") as stream:
                        raw = stream.read(8 * 1024 * 1024 + 1)
                    require(len(raw) <= 8 * 1024 * 1024, "A source input grew beyond the review size limit.")
                total += len(raw)
                require(total <= 32 * 1024 * 1024, "Source inputs exceed the review size limit.")
                source_files[str(path.relative_to(project.root))] = hashlib.sha256(raw).hexdigest()
            value.update(head=head.decode().strip(), diff_sha256=hashlib.sha256(diff).hexdigest(), source_files=source_files,
                         profile_sha256=hashlib.sha256(project.path.read_bytes()).hexdigest(),
                         task_inventory=load_inventory(project)[1]["inventory_sha256"])
            # Candidate and manifest arguments also bind their review inputs.
            artifact_hashes = {}
            artifact_bytes = 0
            artifact_candidates = list(args[1:])
            if args[0] == "integrate-queue":
                manifest = Path(args[1])
                require(manifest.is_file() and not manifest.is_symlink() and manifest.stat().st_size <= 1024 * 1024,
                        "Select a bounded regular queue manifest.")
                queue = json.loads(manifest.read_bytes())
                items = queue.get("items", []) if isinstance(queue, dict) else []
                require(isinstance(items, list) and len(items) <= 1000, "The candidate manifest is invalid.")
                for item in items:
                    require(isinstance(item, dict) and isinstance(item.get("artifacts"), str), "The candidate manifest needs artifact directories.")
                    candidate = Path(item["artifacts"])
                    artifact_candidates.append(str((candidate if candidate.is_absolute() else manifest.parent / candidate).resolve()))
            for argument in artifact_candidates:
                candidate = Path(argument)
                if not candidate.is_absolute() or candidate.is_symlink():
                    continue
                files = [candidate] if candidate.is_file() else [candidate / name for name in
                    ("manifest.json", "report.json", "changes.patch", "integration.json", "integration.patch")]
                for source in files:
                    if source.is_file() and not source.is_symlink():
                        require(source.stat().st_size <= 32 * 1024 * 1024, "An artifact exceeds the review size limit.")
                        if str(source) not in artifact_hashes:
                            with source.open("rb") as stream:
                                raw = stream.read(32 * 1024 * 1024 + 1)
                            require(len(raw) <= 32 * 1024 * 1024, "An artifact grew beyond the review size limit.")
                            artifact_bytes += len(raw)
                            require(artifact_bytes <= 64 * 1024 * 1024, "Candidate inputs exceed the review size limit. Select a smaller queue.")
                            artifact_hashes[str(source)] = hashlib.sha256(raw).hexdigest()
            value["artifacts"] = artifact_hashes
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def preview_cli(self, command):
        args, project = self._command(command)
        binding = self._binding(args, project)
        with self.lock:
            require(len(self.previews) < 1000, "Too many command previews; restart the local dashboard.")
            self.previews[binding] = {"args": args, "created": time.monotonic(), "used": False}
        paid = args[0] in {"probe", "deploy-gateway"}
        return {"argv": ["swarm-control", *args], "review_binding": binding,
                "confirmation_required": True, "paid_operation": paid,
                "summary": "Review this exact controller operation before executing it. "
                           + ("Provider usage may incur charges." if paid else "Existing review, source and accounting gates apply."),
                "executes_shell": False}

    def execute_cli(self, command, review_binding, *, directory):
        args, project = self._command(command)
        binding = self._binding(args, project)
        with self.lock:
            preview = self.previews.get(review_binding)
            require(binding == review_binding and preview and preview["args"] == args
                    and not preview["used"] and time.monotonic() - preview["created"] <= 900,
                    "The preview changed, expired or was already used. Review a new command.")
            preview["used"] = True
        return self._execute_cli(args, directory)

    def _execute_cli(self, args, directory):
        result = execute([sys.executable, "-m", "swarm_control", *args], cwd=CONTROLLER,
                         environment=self.connections.environment(), timeout=1800)
        result = self._scrub(result)
        private_json(directory / "command-output.json", result)
        return {**result, "output": result["output"][-32000:], "argv": ["swarm-control", *args]}
