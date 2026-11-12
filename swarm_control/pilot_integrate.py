"""Review-gated integration: plan/prepare/publish/merge ARTIFACT_DIR.

Publish and merge require --reviewed-patch-sha SHA256. Prepare runs local checks
on current origin/main; merge refuses if that base has since changed. Worktrees
and logs remain available after any failure. No command force-pushes or bypasses CI.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from .modal_worker import (
    acceptance_commands,
    acceptance_files,
    owned,
    protected_acceptance_files,
    valid_path,
)
from .pilot_state import common_state
from .project import load_project

ROOT = Path.cwd()
REPO = None
PROJECT = None
BASE_BRANCH = "main"


def configure(project):
    global ROOT, REPO, PROJECT, BASE_BRANCH
    ROOT, REPO, PROJECT, BASE_BRANCH = project.root, project.repository, project, project.base_branch
SECRET = re.compile(
    rb"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})"
    rb"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    rb'|"private_key"\s*:\s*"[^"\s]'
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return datetime.now(UTC).isoformat()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


@contextmanager
def integration_lock(state, *, wait=False, timeout=30):
    """Serialize credentialed integration across this checkout's worktrees."""
    state.mkdir(parents=True, exist_ok=True)
    with (state / "integration.lock").open("a") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait or time.monotonic() >= deadline:
                    raise ValueError("another integrator is active for this repository") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def command(argv, cwd, *, data=None, log=None, timeout=None):
    env = {key: value for key, value in os.environ.items()
           if key not in {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "OPENAI_ADMIN_KEY",
                          "OPENAI_ADMIN_API_KEY"} and not key.endswith("_API_KEY")}
    if argv[0] != "gh":
        env = {key: value for key, value in env.items()
               if key not in {"GH_TOKEN", "GITHUB_TOKEN"} and not key.endswith("_API_KEY")}
    if timeout is None:
        result = subprocess.run(argv, cwd=cwd, env=env, input=data, capture_output=True, check=False)
    else:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            for termination_signal in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, termination_signal)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = process.communicate(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                raise ValueError("local check deadline reached; process cleanup is uncertain") from None
            if log:
                log.write_bytes(stdout + stderr)
            raise ValueError("local check deadline reached; process group was stopped") from None
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if log:
        log.write_bytes(result.stdout + result.stderr)
    require(result.returncode == 0, f"{argv[0]} {argv[1]} failed; inspect {log or 'local command output'}")
    return result.stdout


def git(root, *args):
    return command(["git", *args], root).decode().strip()


def github(path, *, method="GET", body=None):
    argv = ["gh", "api", "--method", method, f"repos/{REPO}/{path}"]
    if body is not None:
        argv.extend(["--input", "-"])
    return json.loads(command(argv, ROOT, data=json.dumps(body).encode() if body is not None else None))


def artifacts(directory, *, recovery_reviewed=False):
    manifest = json.loads((directory / "manifest.json").read_text())
    report = json.loads((directory / "report.json").read_text())
    patch = (directory / "changes.patch").read_bytes()
    digest = hashlib.sha256(patch).hexdigest()
    task = manifest["task"]
    require(manifest.get("repository") == f"https://github.com/{REPO}.git", "unexpected repository")
    require(re.fullmatch(r"[A-Z][A-Z0-9_-]{1,39}", task.get("id", "")), "invalid task ID")
    require(report.get("task_id") == task["id"], "task/report mismatch")
    require(isinstance(task.get("ownership"), list) and task["ownership"]
            and all(valid_path(path) for path in task["ownership"]), "invalid ownership")
    require(isinstance(task.get("title"), str) and task["title"].strip()
            and "\n" not in task["title"], "invalid title")
    require(isinstance(task.get("acceptance"), list) and task["acceptance"]
            and all(isinstance(item, str) and item.strip() for item in task["acceptance"]), "missing acceptance")
    require(type(task.get("issue")) is int and task["issue"] > 0, "task needs a GitHub issue")
    acceptance_commands(task)
    acceptance_files(task)
    require(re.fullmatch(r"[a-f0-9]{40}", manifest.get("base_sha", ""))
            and manifest["base_sha"] == report.get("base_sha"), "invalid worker base")
    changes = report.get("changes", {})
    require(changes.get("head_unchanged") is True
            and changes.get("ownership_violations") == []
            and changes.get("prohibited_files") == []
            and not report.get("recovery_errors") and not report.get("patch_redacted"), "worker did not validate")
    require(patch and digest == report.get("patch_sha256"), "checked/downloaded patch digest mismatch")
    if recovery_reviewed:
        recovery = report.get("recovered_candidate", {})
        require(report.get("status") in {"needs_repair", "failed", "interrupted"}
                and isinstance(recovery, dict) and recovery.get("status") == "candidate"
                and recovery.get("original_worker_status") == report["status"]
                and recovery.get("original_agent_exit") == report.get("agent_exit")
                and recovery.get("patch_sha256") == digest
                and recovery.get("base_sha") == report["base_sha"]
                and recovery.get("head_unchanged") is True and recovery.get("scope_clean") is True
                and recovery.get("requires_independent_validation") is True
                and recovery.get("original_outcome_preserved") is True
                and recovery.get("authorizes_publish") is False,
                "recovery candidate binding or explicit review is missing")
    else:
        require(report.get("status") == "validated"
                and type(report.get("agent_exit")) is int and report["agent_exit"] == 0
                and type(report.get("check_exit")) is int and report["check_exit"] == 0,
                "worker did not validate")
        require(digest == report.get("checked_patch_sha256"), "checked/downloaded patch digest mismatch")
    metadata = {"task": task, "timings": [report.get(key) for key in ("started_at", "finished_at", "wall_seconds")]}
    require(not SECRET.search(patch) and not SECRET.search(json.dumps(metadata).encode()),
            "credential content prohibited in artifacts")
    return task, report, digest


def source_bindings(directory, task, report, tested_base):
    """Bind the exact frozen inputs and selected validation, never their raw content."""
    require(isinstance(tested_base, str) and re.fullmatch(r"[a-f0-9]{40}", tested_base),
            "invalid tested base for source binding")
    sources = {}
    parsed = {}
    for name in ("manifest.json", "report.json", "changes.patch"):
        path = directory / name
        require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 32 * 1024 * 1024,
                "integration source is missing, unsafe or too large")
        content = path.read_bytes()
        sources[name] = hashlib.sha256(content).hexdigest()
        if name.endswith(".json"):
            parsed[name] = json.loads(content)
    require(parsed["manifest.json"].get("task") == task and parsed["report.json"] == report,
            "integration source metadata changed before validation")
    profile = None
    if PROJECT is not None:
        path = getattr(PROJECT, "path", None)
        config_hash = None
        if path is not None:
            require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 1024 * 1024,
                    "selected project configuration is missing or unsafe")
            config_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        instructions = []
        for name in getattr(PROJECT, "instructions", ()):
            entry = command(["git", "ls-tree", "-z", tested_base, "--", name], ROOT).split(b"\t", 1)
            fields = entry[0].split()
            require(len(entry) == 2 and len(fields) == 3
                    and fields[0] in {b"100644", b"100755"} and fields[1] == b"blob"
                    and entry[1] == name.encode() + b"\0",
                    "selected instruction is not a regular file at the tested base")
            content = command(["git", "show", f"{tested_base}:{name}"], ROOT)
            instructions.append({"path_sha256": hashlib.sha256(name.encode()).hexdigest(),
                                 "content_sha256": hashlib.sha256(content).hexdigest()})
        profile = {"config_sha256": config_hash, "install": PROJECT.install, "checks": PROJECT.checks,
                   "required_checks": getattr(PROJECT, "required_checks", ()),
                   "instructions": instructions, "tested_base_sha": tested_base,
                   "repository": REPO, "base_branch": BASE_BRANCH}
    def canonical(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": 1, "source_sha256": sources, "task_sha256": canonical(task),
            "project_validation_sha256": canonical(profile), "tested_base_sha": tested_base}


def check_source_bindings(directory, state, task=None, report=None):
    """Fail closed for old receipts and any drift before reusing accepted checks."""
    binding = state.get("source_bindings")
    require(isinstance(binding, dict) and binding.get("schema_version") == 1,
            "integration source binding missing; preserve this attempt and explicitly prepare a new artifact copy")
    if task is None or report is None:
        current_task = json.loads((directory / "manifest.json").read_text())["task"]
        current_report = json.loads((directory / "report.json").read_text())
        require(task is None or task == current_task, "integration task differs from frozen source")
        require(report is None or report == current_report, "integration report differs from frozen source")
        task, report = current_task, current_report
    require(binding == source_bindings(directory, task, report, state.get("tested_base_sha"))
            and binding["source_sha256"]["changes.patch"] == state.get("patch_sha256"),
            "integration source binding changed; preserve this attempt and explicitly prepare a new artifact copy")
    if state.get("status") in {"prepared", "committed", "published", "merged"}:
        configured = acceptance_commands(task) is not None
        require(state.get("baseline_checks_exit") == 0 and type(state.get("baseline_checks_exit")) is int
                and state.get("checks_finished_at")
                and state.get("acceptance_configured") is configured
                and (not configured or type(state.get("acceptance_checks_exit")) is int
                     and state["acceptance_checks_exit"] == 0), "accepted integration checks are missing")


def audit(root, base, ownership):
    """Recheck real staged, unstaged, deleted and untracked candidate paths."""
    changed = command(["git", "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                       "--name-only", "-z", base, "--"], root)
    untracked = command(["git", "ls-files", "--others", "--exclude-standard", "-z"], root)
    paths = sorted(set(filter(None, (changed + untracked).decode().split("\0"))))
    require(paths, "empty candidate")
    for path in paths:
        require(valid_path(path) and owned(path, ownership), f"ownership violation: {path}")
        target = root / path
        require(not target.is_symlink() and not any(parent.is_symlink() for parent in target.parents if parent != root),
                f"symlink prohibited: {path}")
        require(not any(part.startswith(".env") for part in Path(path).parts)
                and not re.search(r"(?:^|/)(?:auth\.json|\.modal\.toml|.*(?:service[-_]?account|firebase[-_]?config|credentials).*)$", path, re.IGNORECASE),
                f"credential file prohibited: {path}")
        require(not target.is_dir(), f"submodule/directory prohibited: {path}")
        if target.is_file():
            require(not SECRET.search(target.read_bytes()), f"credential content prohibited: {path}")
    return paths


def candidate(state, task, *, committed=False):
    root = Path(state["worktree"])
    require(git(root, "branch", "--show-current") == state["branch"], "integration branch changed")
    require(git(root, "rev-parse", "HEAD") == state["head_sha" if committed else "tested_base_sha"], "integration HEAD changed")
    audit(root, state["tested_base_sha"], task["ownership"])
    require(not git(root, "diff", "--name-only"), "unstaged changes after validation")
    require(not git(root, "ls-files", "--others", "--exclude-standard"), "untracked changes after validation")
    require(git(root, "write-tree") == state["tested_tree"], "candidate tree changed after validation")
    if "applied_patch_sha256" in state:
        require(applied_digest(root, state["tested_base_sha"]) == state["applied_patch_sha256"],
                "applied patch changed after validation")
    if committed:
        require(git(root, "rev-parse", "HEAD^{tree}") == state["tested_tree"], "committed tree differs from validation")
        require(git(root, "rev-parse", "HEAD^") == state["tested_base_sha"], "commit parent differs from tested base")
    return root


def applied_patch(root, base):
    return command(["git", "diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
                    "--no-textconv", "--no-renames", base, "--"], root)


def applied_digest(root, base):
    return hashlib.sha256(applied_patch(root, base)).hexdigest()


def require_recorded_budget_policy():
    """Check existing accounting read-only before irreversible integration writes."""
    path = common_state(ROOT) / "control.sqlite3"
    if not path.exists():
        return
    require(PROJECT is not None, "select a project before checking the recorded budget policy")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0)) as db:
            db.execute("PRAGMA query_only = ON")
            rows = db.execute(
                "SELECT singleton,model_limit_cents,infra_limit_cents FROM budget_policy"
            ).fetchall()
    except sqlite3.Error:
        raise ValueError(
            "cannot verify the existing budget policy read-only; reconcile the ledger "
            "before publishing or merging; no policy was changed"
        ) from None
    require(len(rows) == 1 and rows[0][0] == 1,
            "existing ledger has no single recorded budget policy; reconcile it before integration")
    require(rows[0][1:] == (PROJECT.model_limit_cents, PROJECT.infra_limit_cents),
            "selected profile differs from the recorded budget policy; select a matching profile "
            "before publishing or merging; no policy was changed")


@contextmanager
def preparation_lock(directory):
    """Keep one artifact's preparation exclusive without blocking other candidates."""
    locks = common_state(ROOT) / "integration-preparation-locks"
    require(not locks.is_symlink(), "unsafe preparation lock directory")
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
    path = locks / f"{key}.lock"
    require(not path.is_symlink(), "unsafe preparation lock")
    with path.open("a") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("this candidate is already being prepared") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prepare(directory, task, report, digest, worktree=None, *, expected_base=None,
            fetch_base=True, check_timeout=None, recovery_reviewed=False):
    """Prepare in a dedicated checkout; only shared Git setup holds the global lock."""
    with preparation_lock(directory):
        require(not (directory / "integration.json").exists(), "integration already exists; preserve it and use a new artifact copy for a new attempt")
        with integration_lock(common_state(ROOT), wait=True):
            if fetch_base:
                git(ROOT, "fetch", "origin", f"{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}")
            base = git(ROOT, "rev-parse", f"origin/{BASE_BRANCH}")
            require(expected_base is None or expected_base == base,
                    "main changed; explicitly prepare and test on the new base")
            binding = source_bindings(directory, task, report, base)
            require(binding["source_sha256"]["changes.patch"] == digest, "patch changed before preparation")
            branch = f"swarm/{task['id'].lower()}-integrate-{uuid.uuid4().hex[:8]}"
            root = (worktree or common_state(ROOT) / "integration-worktrees" / branch.split("/")[-1]).resolve()
            root.parent.mkdir(parents=True, exist_ok=True)
            git(ROOT, "worktree", "add", "-b", branch, str(root), base)
            state = {"task_id": task["id"], "batch_id": directory.parent.name, "status": "preparing",
                     "patch_sha256": digest, "worker_base_sha": report["base_sha"], "tested_base_sha": base,
                     "worktree": str(root), "branch": branch, "preparation_started_at": now(),
                     "attribution": "reviewed_worker_recovery" if recovery_reviewed else "validated_worker",
                     "original_worker_status": report.get("status"),
                     "original_agent_exit": report.get("agent_exit"), "source_bindings": binding}
            save(directory / "integration.json", state)
        try:
            return _prepare_candidate(directory, task, report, digest, root, base, state,
                                      check_timeout=check_timeout)
        except (Exception, SystemExit):
            if state.get("status") in {"preparing", "checking"}:
                state.update(status="interrupted", failed_at=now())
                save(directory / "integration.json", state)
            raise


def _prepare_candidate(directory, task, report, digest, root, base, state, *, check_timeout=None):
    patch = (directory / "changes.patch").read_bytes()
    require(hashlib.sha256(patch).hexdigest() == digest, "patch changed before apply")
    manifest = json.loads((directory / "manifest.json").read_text())
    try:
        command(["git", "apply", "--check", "--index", "-"], root, data=patch)
    except ValueError:
        if manifest.get("allow_overlap") is not True:
            state.update(status="needs_repair", repair_reason="patch_does_not_apply", failed_at=now())
            save(directory / "integration.json", state)
            raise
        state["application_method"] = "three_way"
        try:
            command(["git", "merge-base", "--is-ancestor", report["base_sha"], base], root)
            command(["git", "apply", "--3way", "--index", "-"], root, data=patch,
                    log=directory / "integration-apply.log")
        except ValueError:
            conflicts = command(["git", "diff", "--name-only", "--diff-filter=U", "-z"], root)
            changed = command(["git", "diff", "--name-only", "-z", base, "--"], root)
            state.update(status="needs_repair", repair_reason="three_way_apply_failed",
                         conflicting_files=sorted(filter(None, conflicts.decode().split("\0"))),
                         changed_files=sorted(filter(None, changed.decode().split("\0"))), failed_at=now())
            # This is a newly created dedicated worktree; retain the original patch/logs,
            # then remove partial application and conflict markers before any repair.
            save(directory / "integration.json", state)
            git(root, "reset", "--hard", base)
            git(root, "clean", "-fd")
            state["worktree_cleaned"] = True
            save(directory / "integration.json", state)
            raise ValueError("patch needs repair; preserved integration.json and original worker patch") from None
    else:
        command(["git", "apply", "--index", "-"], root, data=patch)
        state["application_method"] = "direct"
    try:
        paths = audit(root, base, task["ownership"])
        tree = git(root, "write-tree")
        require(PROJECT is not None, "select a project before integration")
    except ValueError:
        state.update(status="needs_repair", repair_reason="candidate_validation_failed", failed_at=now())
        save(directory / "integration.json", state)
        raise
    started = now()
    (directory / "integration.patch").write_bytes(applied_patch(root, base))
    state.update(status="checking", tested_tree=tree, changed_files=paths,
                 applied_patch_sha256=applied_digest(root, base),
                 checks_started_at=started)
    acceptance = acceptance_commands(task)
    protected = acceptance_files(task)
    state.update(acceptance_configured=acceptance is not None, acceptance_checks_exit=None,
                 independent_product_acceptance=False)
    save(directory / "integration.json", state)
    try:
        state["acceptance_input_sha256"] = protected_acceptance_files(root, report["base_sha"], protected)
        for phase, commands in (("install", PROJECT.install), ("checks", PROJECT.checks)):
            for index, argv in enumerate(commands):
                options = {"timeout": check_timeout} if check_timeout is not None else {}
                command(argv, root, log=directory / f"integration-{phase}-{index + 1}.log", **options)
        if acceptance is not None:
            protected_acceptance_files(root, report["base_sha"], protected)
            for index, argv in enumerate(acceptance):
                options = {"timeout": check_timeout} if check_timeout is not None else {}
                command(argv, root, log=directory / f"integration-acceptance-{index + 1}.log", **options)
            protected_acceptance_files(root, report["base_sha"], protected)
            state["acceptance_checks_exit"] = 0
        candidate(state, task)
        check_source_bindings(directory, state, task, report)
    except ValueError:
        state.update(status="needs_repair", repair_reason="candidate_validation_failed", failed_at=now())
        save(directory / "integration.json", state)
        raise
    state.update(status="prepared", checks_finished_at=now(), baseline_checks_exit=0)
    save(directory / "integration.json", state)
    return state


def load_review(directory, digest, reviewed, reviewed_applied=None):
    require(reviewed == digest, "explicit --reviewed-patch-sha must match the verified worker patch")
    state = json.loads((directory / "integration.json").read_text())
    require(state.get("patch_sha256") == digest, "integration artifact changed")
    if state.get("application_method") == "three_way":
        require(isinstance(state.get("applied_patch_sha256"), str)
                and reviewed_applied == state["applied_patch_sha256"],
                "three-way integration requires --reviewed-applied-patch-sha for the validated diff")
    check_source_bindings(directory, state)
    return state


def publish(directory, task, report, state):
    require_recorded_budget_policy()
    check_source_bindings(directory, state, task, report)
    require(state["status"] in {"prepared", "committed"}, "integration is not publishable")
    root = candidate(state, task, committed=state["status"] == "committed")
    require(github(f"git/ref/heads/{BASE_BRANCH}")["object"]["sha"] == state["tested_base_sha"], "main changed; prepare and test again")
    issue = github(f"issues/{task['issue']}")
    require(issue.get("state") == "open" and "pull_request" not in issue, "task issue is not open")
    title = f"[{task['id']}] {task['title']}"
    if state["status"] == "prepared":
        git(root, "commit", "-m", title)
        state.update(status="committed", head_sha=git(root, "rev-parse", "HEAD"), reviewed_at=now())
        save(directory / "integration.json", state)
    candidate(state, task, committed=True)
    require_recorded_budget_policy()
    check_source_bindings(directory, state, task, report)
    git(root, "push", "--set-upstream", "origin", f"HEAD:refs/heads/{state['branch']}")
    body = "\n".join([
        task["title"], "", "Acceptance:", *(f"- {item}" for item in task["acceptance"]), "",
        "Validation: all project-configured install and check commands passed on the candidate tree.",
        f"Ownership/public contract: limited to {', '.join(task['ownership'])}; acceptance defines the contract.",
        f"Reviewed patch SHA256: {state['patch_sha256']}", f"Tested base: {state['tested_base_sha']}",
        f"Application: {state.get('application_method', 'direct')}; applied diff SHA256: {state.get('applied_patch_sha256', 'unrecorded')}",
        f"Tested tree: {state['tested_tree']}", f"Source: Modal worker {task['id']}, batch {state['batch_id']}.",
        f"Worker started: {report.get('started_at')}; finished: {report.get('finished_at')}; seconds: {report.get('wall_seconds')}.",
        f"Integration checks: {state['checks_started_at']} through {state['checks_finished_at']}.",
        "Artifacts and raw logs remain local.", "", f"Closes #{task['issue']}",
    ])
    require(not SECRET.search(body.encode()), "credential content prohibited in PR metadata")
    existing = github(f"pulls?state=open&head={REPO.split('/')[0]}:{state['branch']}&base={BASE_BRANCH}")
    if existing:
        pr = existing[0]
    else:
        require_recorded_budget_policy()
        check_source_bindings(directory, state, task, report)
        pr = github("pulls", method="POST", body={
            "title": title, "body": body, "head": state["branch"], "base": BASE_BRANCH})
    require(pr["head"]["sha"] == state["head_sha"], "published PR head differs")
    state.update(status="published", pr_number=pr["number"], pr_url=pr["html_url"], published_at=now())
    save(directory / "integration.json", state)
    return state


def green_checks(payload, head, required_checks=None):
    checks = payload.get("check_runs", [])
    require(payload.get("total_count", len(checks)) == len(checks), "check pagination incomplete")
    actions = [check for check in checks if check.get("app", {}).get("slug") == "github-actions"]
    required_checks = required_checks or ("checks",)
    require(set(required_checks) <= {check.get("name") for check in actions}, "required CI checks missing")
    require(all(check.get("head_sha") == head and check.get("status") == "completed"
                and check.get("conclusion") == "success" for check in actions), "GitHub Actions checks are not all green for this head")


def all_checks(head):
    """Collect every check page, stopping safely on inconsistent or excessive data."""
    checks, identifiers, expected = [], set(), None
    for page in range(1, 101):
        suffix = "" if page == 1 else f"&page={page}"
        payload = github(f"commits/{head}/check-runs?per_page=100{suffix}")
        entries = payload.get("check_runs")
        total = payload.get("total_count")
        require(isinstance(entries, list) and type(total) is int and total >= 0,
                "invalid check pagination response")
        require(expected is None or expected == total, "check list changed during pagination")
        expected = total
        for check in entries:
            require(isinstance(check, dict), "invalid check pagination entry")
            identity = check.get("id")
            if identity is not None:
                require(identity not in identifiers, "duplicate check during pagination")
                identifiers.add(identity)
        checks.extend(entries)
        require(len(checks) <= expected, "check pagination exceeds total")
        if len(checks) == expected:
            return {"total_count": expected, "check_runs": checks}
        require(len(entries) == 100, "check pagination incomplete")
    raise ValueError("check pagination limit exceeded")


def merge(directory, task, state):
    require_recorded_budget_policy()
    check_source_bindings(directory, state, task)
    require(state["status"] in {"published", "merged"}, "integration has not been published")
    candidate(state, task, committed=True)
    pr = github(f"pulls/{state['pr_number']}")
    require(not pr.get("draft") and pr["base"]["ref"] == BASE_BRANCH
            and pr["head"]["sha"] == state["head_sha"] and pr["head"]["ref"] == state["branch"], "PR changed or is not mergeable work")
    green_checks(all_checks(state["head_sha"]), state["head_sha"], PROJECT.required_checks if PROJECT else None)
    if not pr.get("merged"):
        require(pr.get("state") == "open" and state["status"] == "published", "PR is not open")
        require(github(f"git/ref/heads/{BASE_BRANCH}")["object"]["sha"] == state["tested_base_sha"], "main changed; prepare and test again")
        require_recorded_budget_policy()
        check_source_bindings(directory, state, task)
        from .operations import OperationsJournal

        journal = OperationsJournal(common_state(ROOT) / "operations.sqlite3")
        reservation = journal.reserve_action("merge_pr", f"{state['pr_number']}:{state['head_sha']}")
        try:
            result = github(f"pulls/{state['pr_number']}/merge", method="PUT", body={
                "merge_method": "squash", "sha": state["head_sha"], "commit_title": f"[{task['id']}] {task['title']}"})
        except BaseException:
            journal.finish_action(reservation, "uncertain")
            raise
        journal.finish_action(reservation, "succeeded" if result.get("merged") is True else "uncertain")
        require(result.get("merged") is True, "GitHub did not merge the reviewed head")
        state.update(status="merged", merge_commit_sha=result["sha"], merge_recorded_at=now(), green_head_sha=state["head_sha"])
        save(directory / "integration.json", state)
        pr = github(f"pulls/{state['pr_number']}")
    require(pr.get("merged") is True and pr.get("merge_commit_sha"), "merged PR metadata unavailable; retry merge to reconcile")
    state.update(status="merged", merged_at=pr.get("merged_at"), merge_commit_sha=pr["merge_commit_sha"], green_head_sha=state["head_sha"])
    save(directory / "integration.json", state)
    ledger_path = common_state(ROOT) / "control.sqlite3"
    if ledger_path.exists():
        ledger = PROJECT.ledger(ledger_path)
        if any(row["task_id"] == task["id"] and row["batch_id"] == state["batch_id"]
               and row["state"] != "merged" for row in ledger.rows()):
            ledger.finish(task["id"], state["batch_id"], "merged", directory / "integration.json")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "prepare", "publish", "merge"])
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--reviewed-patch-sha")
    parser.add_argument("--reviewed-applied-patch-sha")
    parser.add_argument("--recovery-reviewed", action="store_true",
                        help="Explicitly review a bound interrupted-worker candidate; original failure remains unchanged")
    args = parser.parse_args()
    project = load_project(args.project)
    project.verify_origin()
    configure(project)
    directory = args.artifacts.resolve()
    task, report, digest = artifacts(directory, recovery_reviewed=args.recovery_reviewed)
    if args.recovery_reviewed and args.command != "plan":
        require(args.reviewed_patch_sha == digest, "reviewed recovery requires --reviewed-patch-sha")
    with integration_lock(common_state(ROOT)) if args.command in {"publish", "merge"} else nullcontext():
        if args.command == "plan":
            result = {"task": task["id"], "ownership": task["ownership"], "patch_sha256": digest, "issue": task["issue"]}
        elif args.command == "prepare":
            result = prepare(directory, task, report, digest, args.worktree,
                             recovery_reviewed=args.recovery_reviewed)
        else:
            state = load_review(directory, digest, args.reviewed_patch_sha, args.reviewed_applied_patch_sha)
            result = publish(directory, task, report, state) if args.command == "publish" else merge(directory, task, state)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
