"""Sandbox helpers and a bounded agent wrapper; validation drops credentials."""

import argparse
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

MAX_BENCHMARK_RECEIPTS = 100
MAX_BENCHMARK_RECEIPT_BYTES = 1_000_000
MAX_BENCHMARK_TOTAL_BYTES = 5_000_000
MAX_BENCHMARK_ENTRIES = 1000
MAX_ACCEPTANCE_COMMANDS = 16
MAX_ACCEPTANCE_FILES = 32
MAX_ACCEPTANCE_BYTES = 1024 * 1024


def private_json(path, value):
    """Keep an atomic private checkpoint, including an interrupted agent's state."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def acceptance_commands(task):
    """Validate optional frozen task argv lists; never interpret a shell string."""
    if "acceptance_checks" not in task:
        return None
    commands = task["acceptance_checks"]
    shells = {"sh", "bash", "dash", "zsh", "fish", "ksh", "cmd", "cmd.exe", "powershell", "pwsh"}
    if (not isinstance(commands, list) or not 1 <= len(commands) <= MAX_ACCEPTANCE_COMMANDS
            or any(not isinstance(command, list) or not 1 <= len(command) <= 128
                   or any(not isinstance(arg, str) or not arg or len(arg) > 8192 or "\x00" in arg
                          for arg in command)
                   or Path(command[0]).name.lower() in shells for command in commands)):
        raise ValueError("acceptance_checks requires bounded argv lists without a shell interpreter")
    if sum(len(arg.encode()) for command in commands for arg in command) > 65536:
        raise ValueError("acceptance_checks exceeds the command size limit")
    return commands


def acceptance_files(task):
    files = task.get("acceptance_files", [])
    if (not isinstance(files, list) or len(files) > MAX_ACCEPTANCE_FILES
            or any(not valid_path(path) or any(ord(c) < 32 for c in path) for path in files)
            or len(set(files)) != len(files)):
        raise ValueError("acceptance_files requires unique repository-relative paths")
    if files and acceptance_commands(task) is None:
        raise ValueError("acceptance_files requires acceptance_checks")
    return files


def protected_acceptance_files(repo, base_sha, files):
    """Compare protected check inputs with the exact base, not an edited HEAD."""
    result = {}
    for name in files:
        entry = run(["git", "ls-tree", "-z", base_sha, "--", name], repo).stdout
        if not entry.startswith((b"100644 blob ", b"100755 blob ")) or entry.count(b"\0") != 1:
            raise ValueError("acceptance input must be a regular file at the frozen base")
        size = run(["git", "cat-file", "-s", f"{base_sha}:{name}"], repo).stdout.strip()
        if not size.isdigit() or int(size) > MAX_ACCEPTANCE_BYTES:
            raise ValueError("acceptance input exceeds the file size limit")
        expected = hashlib.sha256(run(["git", "show", f"{base_sha}:{name}"], repo).stdout).hexdigest()
        path = repo / name
        if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(repo.resolve())
                or path.stat().st_size > MAX_ACCEPTANCE_BYTES):
            raise ValueError("acceptance input changed or is unavailable")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("acceptance input changed from the frozen base")
        result[name] = expected
    return result


def run_acceptance(manifest, repo, artifacts):
    """Task-configured acceptance is separate from baseline and independent review."""
    commands = acceptance_commands(manifest["task"])
    protected = acceptance_files(manifest["task"])
    record = {"schema_version": 1, "configured": commands is not None,
              "source": "frozen_task_commands", "independent_product_acceptance": False,
              "checks_sha256": hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest(),
              "exit_code": None, "inputs_unchanged": None, "protected_input_sha256": {},
              "started_at_utc": datetime.now(UTC).isoformat()}
    start = time.monotonic()
    stage = "input_check"
    try:
        if commands is None:
            return None
        record["protected_input_sha256"] = protected_acceptance_files(repo, manifest["base_sha"], protected)
        record["inputs_unchanged"] = True
        stage = "commands"
        record["exit_code"] = run_commands(commands, repo, artifacts / "acceptance.log")
        stage = "input_recheck"
        protected_acceptance_files(repo, manifest["base_sha"], protected)
        return record["exit_code"]
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        record.update(exit_code=125, error_type=type(error).__name__)
        if stage != "commands":
            record["inputs_unchanged"] = False
        else:
            try:
                protected_acceptance_files(repo, manifest["base_sha"], protected)
            except (ValueError, OSError, subprocess.SubprocessError):
                record["inputs_unchanged"] = False
        return 125
    finally:
        record.update(finished_at_utc=datetime.now(UTC).isoformat(),
                      elapsed_seconds=round(time.monotonic() - start, 6))
        private_json(artifacts / "acceptance.json", record)


def source_fingerprint(repo, ownership, *, command_timeout=5):
    """Observe bounded owned-tree metadata, without logging source or commands.

    A fingerprint change is an edit signal, not evidence of useful/correct code.
    Untracked files are included, unlike a plain Git diff before collection.
    """
    raw = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *ownership],
              repo, timeout=command_timeout).stdout
    if len(raw) > 1024 * 1024:
        raise ValueError("source observation exceeds the metadata limit")
    rows = []
    entries = raw.split(b"\0")
    if len(entries) > 4096:
        raise ValueError("source observation exceeds the path limit")
    for entry in entries:
        if not entry:
            continue
        path = entry[3:].decode("utf-8")
        if not valid_path(path) or not owned(path, ownership):
            continue
        candidate = repo / path
        try:
            info = candidate.lstat()
            rows.append((path, info.st_mtime_ns, info.st_size, info.st_mode))
        except FileNotFoundError:
            rows.append((path, None, None, None))
    return hashlib.sha256(raw + json.dumps(rows, sort_keys=True).encode()).hexdigest()


def process_group_state(group_id):
    """Distinguish living owned-group members from Linux orphan zombies.

    A zombie holds no output descriptor and cannot change source. Without a
    trustworthy observation we retain uncertainty rather than claiming cleanup.
    """
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return "stopped"
    except OSError:
        return "unknown"
    if not Path("/proc").is_dir():
        return "unknown"
    try:
        with os.scandir("/proc") as entries:
            for index, entry in enumerate(entries):
                if index >= 32768:
                    return "unknown"
                if not entry.name.isdigit():
                    continue
                try:
                    if os.getpgid(int(entry.name)) != group_id:
                        continue
                    with (Path(entry.path) / "stat").open("rb") as stream:
                        fields = stream.read(8192).rpartition(b")")[2].split()
                    if not fields:
                        return "unknown"
                    if fields[0] not in {b"Z", b"X"}:
                        return "running"
                except ProcessLookupError:
                    continue
                except FileNotFoundError:
                    continue
                except OSError:
                    return "unknown"
    except OSError:
        return "unknown"
    return "stopped"


def run_agent(command, repo, artifacts, ownership, *, timeout, no_progress_timeout=None,
              poll_seconds=1.0, grace_seconds=5.0, task_id=None, base_sha=None, run_id=None):
    """Own one subprocess group and keep explicit, bounded closure evidence.

    Runs in the VM. Source observations and the stop reason are worker-supervisor
    measurements, not provider-authenticated timeout/OOM or semantic acceptance.
    No retry, new process after failure, extra coding allowance or budget change.
    """
    for number in (timeout, poll_seconds, grace_seconds):
        if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
            raise ValueError("agent supervision durations must be finite and positive")
    if no_progress_timeout is not None and (type(no_progress_timeout) not in (int, float)
            or not math.isfinite(no_progress_timeout) or not 0 < no_progress_timeout <= timeout):
        raise ValueError("no-progress timeout must fit within the coding deadline")
    if (not isinstance(command, list) or not 1 <= len(command) <= 128
            or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)
            or not isinstance(ownership, list) or not ownership or not all(valid_path(p) for p in ownership)):
        raise ValueError("agent supervision requires argv and owned repository paths")
    artifacts.mkdir(parents=True, exist_ok=True)
    start = last_change = last_checkpoint = time.monotonic()
    record = {"schema_version": 1, "source": "worker_supervisor_observed",
              "task_id": task_id, "base_sha": base_sha, "run_id": run_id,
              "started_at_utc": datetime.now(UTC).isoformat(), "finished_at_utc": None,
              "coding_limit_seconds": timeout, "no_progress_timeout_seconds": no_progress_timeout,
              "termination_grace_seconds": grace_seconds, "terminal_reason": None,
              "child_exit_code": None, "wrapper_exit_code": None, "source_change_count": 0,
              "first_source_change_seconds": None, "last_source_change_seconds": None,
              "source_observation_failures": 0, "warnings": [], "signals": [],
              "remote_provider_cause": None, "automatic_retries": 0,
              "useful_progress_verified": False, "checkpoint_failures": 0}
    record["process_group_cleanup"] = "not_started"
    fingerprint, child = None, None
    observed = True

    def checkpoint():
        record["elapsed_seconds"] = round(time.monotonic() - start, 6)
        try:
            private_json(artifacts / "supervision.json", record)
            return True
        except OSError:
            record["checkpoint_failures"] += 1
            return False

    def warn(code):
        if code not in record["warnings"]:
            record["warnings"].append(code)

    def sample():
        nonlocal fingerprint, last_change, observed
        try:
            current = source_fingerprint(repo, ownership,
                command_timeout=min(5, max(0.001, timeout - (time.monotonic() - start))))
        except (ValueError, OSError, UnicodeError, subprocess.SubprocessError):
            observed = False
            record["source_observation_failures"] += 1
            warn("source_observation_unavailable")
            return
        if not observed:
            # An observation gap cannot prove continuous absence of source work.
            last_change = time.monotonic()
            fingerprint, observed = current, True
            return
        if fingerprint is not None and current != fingerprint:
            last_change = time.monotonic()
            elapsed = round(last_change - start, 6)
            record["source_change_count"] += 1
            record["last_source_change_seconds"] = elapsed
            if record["first_source_change_seconds"] is None:
                record["first_source_change_seconds"] = elapsed
        fingerprint, observed = current, True

    def stop(reason):
        if record["terminal_reason"] is None:
            record["terminal_reason"] = reason

        def group_exists():
            return process_group_state(child.pid) != "stopped"

        for kind in (signal.SIGTERM, signal.SIGKILL):
            if not group_exists():
                break
            try:
                os.killpg(child.pid, kind)
                record["signals"].append(kind.name)
            except ProcessLookupError:
                break
            checkpoint()
            until = time.monotonic() + grace_seconds
            try:
                child.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                pass
            # Reaping the direct child does not establish that a subprocess it
            # spawned also stopped. Finish bounded group cleanup before checks.
            while group_exists() and time.monotonic() < until:
                time.sleep(min(0.01, max(0.001, until - time.monotonic())))

    try:
        sample()
        if not checkpoint():
            raise OSError("Initial supervision evidence could not be saved")
        if time.monotonic() - start >= timeout:
            record.update(terminal_reason="coding_deadline_reached", wrapper_exit_code=124)
            return 124
        child = subprocess.Popen(command, cwd=repo, start_new_session=True)
        record["process_group_cleanup"] = "pending"
        while child.poll() is None:
            sample()
            tick = time.monotonic()
            if tick - start >= timeout:
                stop("coding_deadline_reached")
                break
            if no_progress_timeout is not None and observed and tick - last_change >= no_progress_timeout:
                stop("configured_no_source_progress")
                break
            if record["first_source_change_seconds"] is None and tick - start >= timeout / 2:
                warn("no_source_change_observed")
            if tick - start >= max(0.0, timeout - min(120.0, timeout / 5)):
                warn("closure_window")
            if tick - last_checkpoint >= 5:
                checkpoint()
                last_checkpoint = tick
            time.sleep(min(poll_seconds, max(0.001, timeout - (tick - start))))
        record["child_exit_code"] = child.wait(timeout=grace_seconds)
        if record["terminal_reason"] is None:
            record["terminal_reason"] = "child_exited"
        # The direct child may exit naturally while its descendants still hold
        # stdout/stderr open. Always finish bounded cleanup before returning to
        # the controller, including when the natural exit code was zero.
        if process_group_state(child.pid) != "stopped" and not record["signals"]:
            stop(record["terminal_reason"])
        if process_group_state(child.pid) != "stopped":
            record["process_group_cleanup"] = "uncertain"
            raise RuntimeError("Agent process-group cleanup could not be verified")
        record["process_group_cleanup"] = "verified"
        code = record["child_exit_code"]
        record["wrapper_exit_code"] = code if code >= 0 else min(255, 128 - code)
        return record["wrapper_exit_code"]
    except BaseException as error:
        record["error_type"] = type(error).__name__
        if child is not None and child.poll() is None:
            stop("supervisor_interrupted")
        elif record["terminal_reason"] is None:
            record["terminal_reason"] = "spawn_failed"
        raise
    finally:
        record["finished_at_utc"] = datetime.now(UTC).isoformat()
        checkpoint()


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


def run(args, cwd, *, check=True, timeout=None):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY",
                       "OPENAI_ADMIN_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
    }
    env.update(GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(args, cwd=cwd, env=env, check=check, capture_output=True,
                          **({"timeout": timeout} if timeout is not None else {}))


def prepare_repository(manifest, repo, *, bundle_path=None):
    """Public clone or digest-bound offline source; no controller credential input."""
    source = manifest.get("source_bundle")
    if source is None:
        run(["git", "clone", "--no-checkout", manifest["repository"], str(repo)], repo.parent)
    else:
        if (not isinstance(source, dict) or type(source.get("schema_version")) is not int
                or source["schema_version"] != 1
                or source.get("transport") != "bundle" or source.get("base_sha") != manifest["base_sha"]
                or not isinstance(manifest["base_sha"], str) or not re.fullmatch(r"[a-f0-9]{40}", manifest["base_sha"])
                or source.get("worker_path") != "/tmp/swarm-source.bundle"
                or not isinstance(source.get("sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])
                or type(source.get("bytes")) is not int or not 0 < source["bytes"] <= 64 * 1024 * 1024
                or not isinstance(manifest["repository"], str)
                or not re.fullmatch(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.git", manifest["repository"])):
            raise ValueError("Invalid offline source bundle contract")
        path = Path(bundle_path or source["worker_path"])
        if path.is_symlink() or not path.is_file() or path.stat().st_size != source["bytes"]:
            raise ValueError("Source bundle is missing or has changed size")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != source["sha256"]:
                raise ValueError("Source bundle digest mismatch")
        git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "protocol.allow=never", "-c", "protocol.file.allow=always"]
        expected = (manifest["base_sha"] + " refs/heads/swarm-source\n").encode()
        if run([*git, "bundle", "list-heads", str(path)], repo.parent).stdout != expected:
            raise ValueError("Source bundle refs differ from the exact base")
        with tempfile.TemporaryDirectory(prefix="swarm-empty-template-") as template:
            run([*git, "clone", "--no-checkout", "--no-recurse-submodules", "--no-hardlinks",
                 f"--template={template}", "--branch", "swarm-source", "--", str(path), str(repo)], repo.parent)
        run([*git, "bundle", "verify", str(path)], repo)
        run([*git, "remote", "set-url", "origin", manifest["repository"]], repo)
    run(["git", "checkout", "--detach", manifest["base_sha"]], repo)
    if run(["git", "rev-parse", "--verify", "HEAD"], repo).stdout.decode().strip() != manifest["base_sha"]:
        raise ValueError("Prepared worker HEAD differs from the frozen base")
    run(["git", "switch", "-c", manifest["branch"]], repo)
    if source is not None and bundle_path is None:
        path.unlink()


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
    parser.add_argument("action", choices=["prepare", "collect", "check", "acceptance", "agent"])
    parser.add_argument("manifest")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text())
    repo = Path("/workspace/repo")
    artifacts = Path("/artifacts")
    artifacts.mkdir(exist_ok=True)
    if args.action == "agent":
        policy = manifest["supervision"]
        raise SystemExit(run_agent(manifest["agent_command"], repo, artifacts,
            manifest["task"]["ownership"], timeout=policy["coding_limit_seconds"],
            no_progress_timeout=policy.get("no_progress_timeout_seconds"),
            poll_seconds=policy.get("poll_seconds", 1.0),
            grace_seconds=policy.get("termination_grace_seconds", 5.0),
            task_id=manifest["task"]["id"], base_sha=manifest["base_sha"],
            run_id=manifest.get("run_id")))
    if args.action == "acceptance":
        raise SystemExit(run_acceptance(manifest, repo, artifacts) or 0)
    if args.action == "prepare":
        repo.parent.mkdir(exist_ok=True)
        versions = {
            tool: run([tool, "--version"], "/workspace").stdout.decode().strip()
            for tool in ("node", "npm", "codex", "git")
        }
        (artifacts / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
        prepare_repository(manifest, repo)
        raise SystemExit(run_commands(manifest.get("install_commands"), repo, artifacts / "install.log"))
    if args.action == "collect":
        collect(repo, manifest["base_sha"], manifest["task"]["ownership"], artifacts)
    if args.action == "check":
        raise SystemExit(run_commands(manifest.get("check_commands"), repo, artifacts / "checks.log"))


if __name__ == "__main__":
    main()
