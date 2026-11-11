"""Strict local dashboard catalog; public metadata never includes source paths."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 256 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
SECRET = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+\S+)",
    re.IGNORECASE,
)


def require(condition):
    if not condition:
        raise ValueError("Invalid dashboard catalog")


def identity(value):
    require(
        isinstance(value, str)
        and ID.fullmatch(value) is not None
        and not SECRET.search(value)
    )
    return value


def label(value):
    require(isinstance(value, str) and 1 <= len(value) <= 160)
    require(
        value == value.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )
    require(not SECRET.search(value) and "/" not in value and "\\" not in value)
    return value


def keys(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected))


@dataclass(frozen=True)
class _Run:
    id: str
    label: str
    snapshot: Path


@dataclass(frozen=True)
class _Project:
    id: str
    label: str
    repository: str
    runs: tuple[_Run, ...]


@dataclass(frozen=True)
class Catalog:
    workspace_label: str
    _projects: tuple[_Project, ...]

    def public(self):
        projects = [
            {
                "id": p.id,
                "label": p.label,
                "repository": p.repository,
                "runs": [{"id": r.id, "label": r.label} for r in p.runs],
            }
            for p in self._projects
        ]
        first = self._projects[0] if self._projects else None
        return {
            "workspace_label": self.workspace_label,
            "projects": projects,
            "default_project_id": first.id if first else None,
            "default_run_id": first.runs[0].id if first and first.runs else None,
        }

    def resolve(self, project_id=None, run_id=None):
        if project_id is not None:
            identity(project_id)
        if run_id is not None:
            identity(run_id)
        if not self._projects:
            require(project_id is None and run_id is None)
            return None, "", None, None
        project = (
            self._projects[0]
            if project_id is None
            else next((p for p in self._projects if p.id == project_id), None)
        )
        require(project is not None)
        if not project.runs:
            require(run_id is None)
            return None, project.repository, project.id, None
        run = (
            project.runs[0]
            if run_id is None
            else next((r for r in project.runs if r.id == run_id), None)
        )
        require(run is not None)
        return run.snapshot, project.repository, project.id, run.id


def _reject_constant(_value):
    raise ValueError("Invalid dashboard catalog")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def load_catalog(path):
    """Read configuration once. Snapshot files may be missing or later updated."""
    try:
        path = Path(path).absolute()
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        value = json.loads(
            raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object
        )
        keys(value, ("schema_version", "workspace_label", "projects"))
        require(type(value["schema_version"]) is int and value["schema_version"] == 1)
        workspace = label(value["workspace_label"])
        require(isinstance(value["projects"], list) and len(value["projects"]) <= 100)
        projects, project_ids, total_runs = [], set(), 0
        for item in value["projects"]:
            keys(item, ("id", "label", "repository", "runs"))
            project_id, project_label = identity(item["id"]), label(item["label"])
            require(project_id not in project_ids)
            project_ids.add(project_id)
            repository = item["repository"]
            require(
                isinstance(repository, str)
                and REPO.fullmatch(repository) is not None
                and not SECRET.search(repository)
            )
            require(isinstance(item["runs"], list))
            total_runs += len(item["runs"])
            require(total_runs <= 1000)
            runs, run_ids = [], set()
            for source in item["runs"]:
                keys(source, ("id", "label", "snapshot"))
                run_id, run_label = identity(source["id"]), label(source["label"])
                require(run_id not in run_ids)
                run_ids.add(run_id)
                filename = source["snapshot"]
                require(
                    isinstance(filename, str)
                    and 1 <= len(filename) <= 4096
                    and not any(ord(c) < 32 or ord(c) == 127 for c in filename)
                )
                snapshot = Path(filename)
                if not snapshot.is_absolute():
                    snapshot = path.parent / snapshot
                runs.append(_Run(run_id, run_label, snapshot.absolute()))
            projects.append(
                _Project(project_id, project_label, repository, tuple(runs))
            )
        return Catalog(workspace, tuple(projects))
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        raise ValueError("Invalid or unavailable dashboard catalog") from None
