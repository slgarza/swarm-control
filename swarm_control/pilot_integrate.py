"""Review-gated integration: plan/prepare/publish/merge ARTIFACT_DIR.

Publish and merge require --reviewed-patch-sha SHA256. Prepare runs local checks
on current origin/main; merge refuses if that base has since changed. Worktrees
and logs remain available after any failure. No command force-pushes or bypasses CI.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .modal_worker import owned, valid_path
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


def command(argv, cwd, *, data=None, log=None):
    env = {key: value for key, value in os.environ.items()
           if key not in {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"} and not key.endswith("_API_KEY")}
    if argv[0] != "gh":
        env = {key: value for key, value in env.items()
               if key not in {"GH_TOKEN", "GITHUB_TOKEN"} and not key.endswith("_API_KEY")}
    result = subprocess.run(argv, cwd=cwd, env=env, input=data, capture_output=True, check=False)
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


def artifacts(directory):
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
    require(re.fullmatch(r"[a-f0-9]{40}", manifest.get("base_sha", ""))
            and manifest["base_sha"] == report.get("base_sha"), "invalid worker base")
    changes = report.get("changes", {})
    require(report.get("status") == "validated"
            and type(report.get("agent_exit")) is int and report["agent_exit"] == 0
            and type(report.get("check_exit")) is int and report["check_exit"] == 0
            and changes.get("head_unchanged") is True
            and changes.get("ownership_violations") == []
            and changes.get("prohibited_files") == []
            and not report.get("recovery_errors") and not report.get("patch_redacted"), "worker did not validate")
    require(patch and digest == report.get("checked_patch_sha256") == report.get("patch_sha256"),
            "checked/downloaded patch digest mismatch")
    metadata = {"task": task, "timings": [report.get(key) for key in ("started_at", "finished_at", "wall_seconds")]}
    require(not SECRET.search(patch) and not SECRET.search(json.dumps(metadata).encode()),
            "credential content prohibited in artifacts")
    return task, report, digest


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
    if committed:
        require(git(root, "rev-parse", "HEAD^{tree}") == state["tested_tree"], "committed tree differs from validation")
        require(git(root, "rev-parse", "HEAD^") == state["tested_base_sha"], "commit parent differs from tested base")
    return root


def prepare(directory, task, report, digest, worktree=None):
    require(not (directory / "integration.json").exists(), "integration already exists; preserve it and use a new artifact copy for a new attempt")
    git(ROOT, "fetch", "origin", f"{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}")
    base = git(ROOT, "rev-parse", f"origin/{BASE_BRANCH}")
    branch = f"swarm/{task['id'].lower()}-integrate-{uuid.uuid4().hex[:8]}"
    root = (worktree or common_state(ROOT) / "integration-worktrees" / branch.split("/")[-1]).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    git(ROOT, "worktree", "add", "-b", branch, str(root), base)
    patch = (directory / "changes.patch").read_bytes()
    require(hashlib.sha256(patch).hexdigest() == digest, "patch changed before apply")
    command(["git", "apply", "--check", "--index", "-"], root, data=patch)
    command(["git", "apply", "--index", "-"], root, data=patch)
    paths = audit(root, base, task["ownership"])
    tree = git(root, "write-tree")
    started = now()
    require(PROJECT is not None, "select a project before integration")
    for phase, commands in (("install", PROJECT.install), ("checks", PROJECT.checks)):
        for index, argv in enumerate(commands):
            command(argv, root, log=directory / f"integration-{phase}-{index + 1}.log")
    state = {"task_id": task["id"], "batch_id": directory.parent.name, "status": "prepared",
             "patch_sha256": digest, "worker_base_sha": report["base_sha"], "tested_base_sha": base,
             "tested_tree": tree, "worktree": str(root), "branch": branch, "changed_files": paths,
             "checks_started_at": started, "checks_finished_at": now()}
    candidate(state, task)
    save(directory / "integration.json", state)
    return state


def load_review(directory, digest, reviewed):
    require(reviewed == digest, "explicit --reviewed-patch-sha must match the verified worker patch")
    state = json.loads((directory / "integration.json").read_text())
    require(state.get("patch_sha256") == digest, "integration artifact changed")
    return state


def publish(directory, task, report, state):
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
    git(root, "push", "--set-upstream", "origin", f"HEAD:refs/heads/{state['branch']}")
    body = "\n".join([
        task["title"], "", "Acceptance:", *(f"- {item}" for item in task["acceptance"]), "",
        "Validation: all project-configured install and check commands passed on the candidate tree.",
        f"Ownership/public contract: limited to {', '.join(task['ownership'])}; acceptance defines the contract.",
        f"Reviewed patch SHA256: {state['patch_sha256']}", f"Tested base: {state['tested_base_sha']}",
        f"Tested tree: {state['tested_tree']}", f"Source: Modal worker {task['id']}, batch {state['batch_id']}.",
        f"Worker started: {report.get('started_at')}; finished: {report.get('finished_at')}; seconds: {report.get('wall_seconds')}.",
        f"Integration checks: {state['checks_started_at']} through {state['checks_finished_at']}.",
        "Artifacts and raw logs remain local.", "", f"Closes #{task['issue']}",
    ])
    require(not SECRET.search(body.encode()), "credential content prohibited in PR metadata")
    existing = github(f"pulls?state=open&head={REPO.split('/')[0]}:{state['branch']}&base={BASE_BRANCH}")
    pr = existing[0] if existing else github("pulls", method="POST", body={
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


def merge(directory, task, state):
    require(state["status"] in {"published", "merged"}, "integration has not been published")
    candidate(state, task, committed=True)
    pr = github(f"pulls/{state['pr_number']}")
    require(not pr.get("draft") and pr["base"]["ref"] == BASE_BRANCH
            and pr["head"]["sha"] == state["head_sha"] and pr["head"]["ref"] == state["branch"], "PR changed or is not mergeable work")
    green_checks(github(f"commits/{state['head_sha']}/check-runs?per_page=100"), state["head_sha"], PROJECT.required_checks if PROJECT else None)
    if not pr.get("merged"):
        require(pr.get("state") == "open" and state["status"] == "published", "PR is not open")
        require(github(f"git/ref/heads/{BASE_BRANCH}")["object"]["sha"] == state["tested_base_sha"], "main changed; prepare and test again")
        result = github(f"pulls/{state['pr_number']}/merge", method="PUT", body={
            "merge_method": "squash", "sha": state["head_sha"], "commit_title": f"[{task['id']}] {task['title']}"})
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
    args = parser.parse_args()
    project = load_project(args.project)
    project.verify_origin()
    configure(project)
    directory = args.artifacts.resolve()
    task, report, digest = artifacts(directory)
    if args.command == "plan":
        result = {"task": task["id"], "ownership": task["ownership"], "patch_sha256": digest, "issue": task["issue"]}
    elif args.command == "prepare":
        result = prepare(directory, task, report, digest, args.worktree)
    else:
        state = load_review(directory, digest, args.reviewed_patch_sha)
        result = publish(directory, task, report, state) if args.command == "publish" else merge(directory, task, state)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
