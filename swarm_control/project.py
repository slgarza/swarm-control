"""Validated, credential-free settings stored in each target repository."""

import re
import subprocess
import tomllib
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from .health import FailurePolicy
from .modal_worker import valid_path


def require(value, message):
    if not value:
        raise ValueError(message)


def string_list(value, name):
    require(isinstance(value, list) and value
            and all(isinstance(item, str) and item.strip() and "\x00" not in item
                    for item in value), f"{name} must be a nonempty list of strings")
    return tuple(value)


def commands(value, name):
    require(isinstance(value, list) and value, f"{name} must contain command argument lists")
    return tuple(string_list(command, name) for command in value)


@dataclass(frozen=True)
class Project:
    path: Path
    root: Path
    repository: str
    base_branch: str
    tasks_path: str
    instructions: tuple[str, ...]
    install: tuple[tuple[str, ...], ...]
    checks: tuple[tuple[str, ...], ...]
    required_checks: tuple[str, ...]
    network_domains: tuple[str, ...]
    worker_app: str
    gateway_app: str
    gateway_ledger: str
    openai_secret: str
    environment: str
    image: str
    setup_commands: tuple[str, ...]
    live_enabled: bool
    max_live_agents: int
    model_limit_cents: int
    infra_limit_cents: int
    gateway_concurrency: int = 20
    gateway_tokens_per_minute: int | None = None
    gateway_requests_per_minute: int | None = None
    failure_policy: FailurePolicy = dataclass_field(default_factory=FailurePolicy)
    source_transport: str = "github"

    @property
    def clone_url(self):
        return f"https://github.com/{self.repository}.git"

    @property
    def tasks_dir(self):
        return self.root / self.tasks_path

    @property
    def base_ref(self):
        return f"origin/{self.base_branch}"

    def require_live(self):
        require(self.live_enabled,
                "live execution is disabled in this project's policy; no cloud resources were created")

    def ledger(self, path):
        from .pilot_state import Ledger

        return Ledger(path, model_limit_cents=self.model_limit_cents,
                      infra_limit_cents=self.infra_limit_cents)

    def worker_manifest(self):
        return {"install_commands": self.install, "check_commands": self.checks}

    def verify_origin(self):
        origin = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], cwd=self.root, text=True
        ).strip()
        accepted = {self.clone_url, self.clone_url.removesuffix(".git"),
                    f"git@github.com:{self.repository}.git",
                    f"git@github.com:{self.repository}"}
        require(origin in accepted, "target checkout origin does not match project.repository")


def load_project(path):
    path = Path(path).expanduser().resolve()
    raw = tomllib.loads(path.read_text())
    require(raw.get("version") == 1, "project config requires version = 1")
    allowed = {"version", "project", "validation", "runtime", "budget", "operations"}
    require(not set(raw) - allowed, "unknown top-level project settings")
    project = raw.get("project", {})
    validation = raw.get("validation", {})
    runtime = raw.get("runtime", {})
    budget = raw.get("budget", {})
    operations = raw.get("operations", {})
    for section in (project, validation, runtime, budget, operations):
        require(isinstance(section, dict), "project settings sections must be tables")
    for section, keys in (
        (project, {"repository", "base_branch", "tasks_dir", "instructions"}),
        (validation, {"install", "checks", "required_checks"}),
        (runtime, {"network_domains", "worker_app", "gateway_app", "gateway_ledger",
                   "gateway_concurrency", "gateway_tokens_per_minute", "gateway_requests_per_minute",
                   "openai_secret", "environment", "image", "setup_commands", "source_transport"}),
        (budget, {"live_enabled", "max_live_agents", "model_limit_cents", "infra_limit_cents"}),
        (operations, {"window_seconds", "minimum_failures", "minimum_tasks", "minimum_share"}),
    ):
        require(not set(section) - keys, "unknown project setting; check the configuration schema")
    repository = project.get("repository")
    require(isinstance(repository, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository),
            "project.repository must be a GitHub owner/repository, without credentials")
    root = path.parent
    git_root = Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=root, text=True
    ).strip()).resolve()
    require(root == git_root, "project config must live at the target checkout root")
    branch = project.get("base_branch", "main")
    require(isinstance(branch, str) and not branch.startswith("-")
            and subprocess.run(["git", "check-ref-format", f"refs/heads/{branch}"],
                               capture_output=True, check=False).returncode == 0,
            "invalid base branch")
    tasks_path = project.get("tasks_dir", "swarm/tasks")
    require(valid_path(tasks_path), "tasks_dir must be a repository-relative directory")
    tasks_dir = root / tasks_path
    require(tasks_dir.is_dir() and tasks_dir.resolve().is_relative_to(root),
            "tasks_dir must exist inside the target checkout")
    instructions = string_list(project.get("instructions", ["README.md", "AGENTS.md"]), "instructions")
    require(all(valid_path(item) and (root / item).is_file()
                and (root / item).resolve().is_relative_to(root) for item in instructions),
            "instruction files must exist inside the target checkout")
    domains = string_list(runtime.get("network_domains", ["github.com", "api.github.com",
                          "codeload.github.com", "raw.githubusercontent.com"]), "network_domains")
    require(all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain)
                and "." in domain and ".." not in domain for domain in domains),
            "network_domains must contain exact DNS names without URLs or wildcards")
    names = {}
    for field, default in (("worker_app", "swarm-control-workers"),
                           ("gateway_app", "swarm-control-gateway"),
                           ("gateway_ledger", "swarm-control-budget-v1"),
                           ("openai_secret", "swarm-control-openai"), ("environment", "main")):
        names[field] = runtime.get(field, default)
        require(isinstance(names[field], str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", names[field]),
                f"invalid runtime.{field}")
    image = runtime.get("image", "node:24.6.0-bookworm-slim")
    require(isinstance(image, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]*", image),
            "runtime.image must be a container image reference")
    setup = runtime.get("setup_commands", ["npm install --global npm@11.16.0 @openai/codex@0.166.0"])
    setup = string_list(setup, "setup_commands")
    source_transport = runtime.get("source_transport", "github")
    require(isinstance(source_transport, str) and source_transport in {"github", "bundle"},
            "runtime.source_transport must be github or bundle")
    gateway_concurrency = runtime.get("gateway_concurrency", 20)
    require(type(gateway_concurrency) is int and 1 <= gateway_concurrency <= 100,
            "runtime.gateway_concurrency must be an integer between 1 and 100")
    rates = {field: runtime.get(field) for field in
             ("gateway_tokens_per_minute", "gateway_requests_per_minute")}
    require(all(value is None for value in rates.values()) or all(value is not None for value in rates.values()),
            "runtime.gateway_tokens_per_minute and gateway_requests_per_minute must be configured together")
    for field, maximum in (("gateway_tokens_per_minute", 10**9),
                           ("gateway_requests_per_minute", 10**6)):
        require(rates[field] is None or type(rates[field]) is int and 1 <= rates[field] <= maximum,
                f"runtime.{field} must be a positive bounded integer")
    live = budget.get("live_enabled", False)
    maximum = budget.get("max_live_agents", 10)
    require(type(live) is bool, "live_enabled must be boolean")
    require(type(maximum) is int and 1 <= maximum <= 100,
            "max_live_agents must be between 1 and 100; capacity is not spend authorization")
    limits = {}
    for field in ("model_limit_cents", "infra_limit_cents"):
        limits[field] = budget.get(field, 0)
        require(type(limits[field]) is int and 0 <= limits[field] <= 1_000_000,
                f"{field} must be nonnegative whole cents")
    require(not live or all(limits.values()), "live execution requires explicit positive budget limits")
    return Project(path, root, repository, branch, tasks_path, instructions,
                   commands(validation.get("install"), "validation.install"),
                   commands(validation.get("checks"), "validation.checks"),
                   string_list(validation.get("required_checks", ["checks"]), "required_checks"),
                   domains, names["worker_app"], names["gateway_app"], names["gateway_ledger"],
                   names["openai_secret"], names["environment"],
                   image, setup, live, maximum, **limits, gateway_concurrency=gateway_concurrency,
                   **rates, failure_policy=FailurePolicy(**operations), source_transport=source_transport)
