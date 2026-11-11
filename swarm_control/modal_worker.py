"""Trusted sandbox helper. Never receives model or GitHub credentials."""

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path, PurePosixPath

MAX_BENCHMARK_RECEIPTS = 100
MAX_BENCHMARK_RECEIPT_BYTES = 1_000_000
MAX_BENCHMARK_TOTAL_BYTES = 5_000_000
MAX_BENCHMARK_ENTRIES = 1000


def benchmark_record(text):
    def reject_constant(_value):
        raise ValueError("benchmark_receipt_json_invalid")

    try:
        record = json.loads(text, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise ValueError("benchmark_receipt_json_invalid") from None
    if not isinstance(record, dict):
        raise ValueError("benchmark_receipt_json_invalid")  # noqa: TRY004 - invalid evidence contract
    return record


def benchmark_bundle_summary(bundle):
    """Validate private, worker-reported receipts and return only safe aggregates."""
    if (not isinstance(bundle, dict) or bundle.get("format_version") != 1
            or bundle.get("source") != "worker-reported" or bundle.get("status") != "complete"
            or not isinstance(bundle.get("receipts"), list)
            or len(bundle["receipts"]) > MAX_BENCHMARK_RECEIPTS):
        raise ValueError("benchmark_bundle_invalid")
    total, hashes, names = 0, [], set()
    for receipt in bundle["receipts"]:
        if not isinstance(receipt, dict) or not isinstance(receipt.get("text"), str):
            raise ValueError("benchmark_receipt_invalid")  # noqa: TRY004 - invalid evidence contract
        relative = receipt.get("relative_path")
        if (not valid_path(relative) or not relative.endswith(".json") or relative in names
                or receipt.get("name") != PurePosixPath(relative).name):
            raise ValueError("benchmark_receipt_path_invalid")
        names.add(relative)
        raw = receipt["text"].encode("utf-8")
        if len(raw) > MAX_BENCHMARK_RECEIPT_BYTES or receipt.get("byte_count") != len(raw):
            raise ValueError("benchmark_receipt_size_invalid")
        checksum = hashlib.sha256(raw).hexdigest()
        if receipt.get("sha256") != checksum:
            raise ValueError("benchmark_receipt_hash_invalid")
        benchmark_record(raw)
        total += len(raw)
        hashes.append(checksum)
    if (total > MAX_BENCHMARK_TOTAL_BYTES or bundle.get("receipt_count") != len(hashes)
            or bundle.get("total_bytes") != total):
        raise ValueError("benchmark_bundle_size_invalid")
    return {"source": "worker-reported", "receipt_count": len(hashes),
            "total_bytes": total, "receipt_sha256": hashes}


def collect_benchmarks(repo, destination):
    """Bundle only bounded JSON receipts; never copy a ledger, raw log or symlink."""
    bundle = {"format_version": 1, "source": "worker-reported", "status": "complete",
              "receipt_count": 0, "total_bytes": 0, "receipts": []}
    state, directory = repo / ".swarm", repo / ".swarm/benchmarks"
    scanned = 0

    def walk(current, depth=0):
        nonlocal scanned
        if depth > 16:
            raise ValueError("benchmark_scan_limit")
        with os.scandir(current) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_BENCHMARK_ENTRIES:
                    raise ValueError("benchmark_scan_limit")
                if entry.is_symlink():
                    raise ValueError("benchmark_symlink_prohibited")
                path = Path(entry.path)
                if not path.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("benchmark_path_escape")
                if entry.is_dir(follow_symlinks=False):
                    walk(path, depth + 1)
                    continue
                if path.suffix != ".json":
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise ValueError("benchmark_receipt_not_regular")
                if len(bundle["receipts"]) >= MAX_BENCHMARK_RECEIPTS:
                    raise ValueError("benchmark_receipt_count_limit")
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("benchmark_receipt_not_regular")
                    raw = source.read(MAX_BENCHMARK_RECEIPT_BYTES + 1)
                    after = os.fstat(source.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("benchmark_receipt_changed")
                if len(raw) > MAX_BENCHMARK_RECEIPT_BYTES:
                    raise ValueError("benchmark_receipt_size_limit")
                if bundle["total_bytes"] + len(raw) > MAX_BENCHMARK_TOTAL_BYTES:
                    raise ValueError("benchmark_total_size_limit")
                try:
                    text = raw.decode("utf-8")
                    benchmark_record(text)
                except (UnicodeError, ValueError, RecursionError):
                    raise ValueError("benchmark_receipt_json_invalid") from None
                bundle["receipts"].append({"name": path.name,
                    "relative_path": path.relative_to(directory).as_posix(),
                    "byte_count": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "text": text})
                bundle["receipt_count"] += 1
                bundle["total_bytes"] += len(raw)

    try:
        for parent in (state, directory):
            if parent.is_symlink():
                raise ValueError("benchmark_symlink_prohibited")
            if parent.exists() and not parent.is_dir():
                raise ValueError("benchmark_directory_invalid")
        if directory.exists():
            walk(directory)
        bundle["receipts"].sort(key=lambda item: item["relative_path"])
        summary = benchmark_bundle_summary(bundle)
    except (OSError, ValueError) as error:
        bundle.update(status="failed", error_code=str(error) if isinstance(error, ValueError)
                      else "benchmark_receipt_unreadable")
        raise ValueError(bundle["error_code"]) from None
    finally:
        (destination / "benchmarks.json").write_text(json.dumps(bundle, ensure_ascii=True, indent=2) + "\n")
    return summary


def run(args, cwd, *, check=True):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY",
                       "OPENAI_ADMIN_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
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
    result["benchmark_receipts"] = collect_benchmarks(repo, destination)
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
