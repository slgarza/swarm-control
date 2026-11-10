"""Trusted sandbox helper. Never receives model or GitHub credentials."""

import argparse
import json
import os
import subprocess
from pathlib import Path, PurePosixPath


def run(args, cwd, *, check=True):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CODEX_API_KEY", "OPENAI_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
    }
    return subprocess.run(args, cwd=cwd, env=env, check=check, capture_output=True)


def owned(path, ownership):
    return any(path == root or path.startswith(root + "/") for root in ownership)


def valid_path(path):
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and all(
            part not in {"", ".", "..", ".git", ".swarm"} for part in path.split("/")
        )
    )


def collect(repo, base_sha, ownership, destination):
    """Capture tracked, deleted and new files even when an agent exits unsuccessfully."""
    destination.mkdir(parents=True, exist_ok=True)
    # Intent-to-add includes new files in the binary diff without committing them.
    run(["git", "add", "--intent-to-add", "--all", "--", "."], repo)
    changes = run(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            base_sha,
            "--",
        ],
        repo,
    ).stdout
    paths = sorted(filter(None, changes.decode().split("\0")))
    patch = run(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--binary",
            base_sha,
            "--",
        ],
        repo,
    ).stdout
    (destination / "changes.patch").write_bytes(patch)
    violations = [
        path for path in paths if not valid_path(path) or not owned(path, ownership)
    ]
    # Do not accept symlinks or credentials as a task's implementation.
    prohibited = [
        path
        for path in paths
        if (repo / path).is_symlink()
        or any(part.startswith(".env") for part in PurePosixPath(path).parts)
        or Path(path).name in {"auth.json", ".modal.toml"}
    ]
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.decode().strip()
    result = {
        "changed_files": paths,
        "ownership_violations": violations,
        "prohibited_files": prohibited,
        "head_unchanged": head == base_sha,
        "patch_bytes": len(patch),
    }
    (destination / "changes.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def run_commands(commands, repo, log):
    """Run trusted project argv lists without shell interpolation."""
    if not isinstance(commands, (list, tuple)) or not commands:
        raise ValueError("manifest needs nonempty validation commands")
    with log.open("wb") as output:
        for command in commands:
            if not isinstance(command, (list, tuple)) or not command or not all(
                isinstance(arg, str) and arg and "\x00" not in arg for arg in command
            ):
                raise ValueError("validation commands must be argument lists")
            result = run(command, repo, check=False)
            output.write(result.stdout + result.stderr)
            if result.returncode:
                return result.returncode
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "collect", "check"])
    parser.add_argument("manifest")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    repo = Path("/workspace/repo")
    artifacts = Path("/artifacts")
    artifacts.mkdir(exist_ok=True)
    if args.action == "prepare":
        repo.parent.mkdir(exist_ok=True)
        versions = {
            tool: run([tool, "--version"], "/workspace").stdout.decode().strip()
            for tool in ("node", "npm", "codex", "git")
        }
        (artifacts / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
        run(
            ["git", "clone", "--no-checkout", manifest["repository"], str(repo)],
            "/workspace",
        )
        run(["git", "checkout", "--detach", manifest["base_sha"]], repo)
        run(["git", "switch", "-c", manifest["branch"]], repo)
        raise SystemExit(run_commands(manifest.get("install_commands"), repo, artifacts / "install.log"))
    if args.action == "collect":
        collect(repo, manifest["base_sha"], manifest["task"]["ownership"], artifacts)
    if args.action == "check":
        raise SystemExit(run_commands(manifest.get("check_commands"), repo, artifacts / "checks.log"))


if __name__ == "__main__":
    main()
