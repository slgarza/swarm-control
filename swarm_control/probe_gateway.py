"""One budgeted Codex/tool round trip through the deployed gateway.

This command performs a paid probe only when invoked explicitly. It shares the
pilot's cumulative local allowance; a failed probe never releases its allocation.
"""

import argparse
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .modal_runner import (
    codex_command,
    consume,
    controller_lock,
    now,
    redact,
    write_json,
)
from .pilot_state import common_state
from .project import load_project

MODEL = "gpt-6.1-sol"
TASK_ID = "PROBE"
MODEL_CENTS = 50
INFRA_CENTS = 135
PROMPT = (
    "This is a connectivity probe. Execute exactly one shell command: "
    "printf 'gateway-ready\\n'. Do not inspect or modify files, run other commands, "
    "use the network, commit, or spawn agents. After the command succeeds, "
    "respond with exactly READY.\n"
)
SETUP = """
import pathlib
import subprocess
pathlib.Path('/tmp/probe').mkdir(parents=True, exist_ok=True)
pathlib.Path('/artifacts').mkdir(parents=True, exist_ok=True)
subprocess.run(['git', 'init', '-q', '/tmp/probe'], check=True)
print('probe-workspace-ready')
"""
VERIFY = """
import json
import subprocess
result = subprocess.run(['git', '-C', '/tmp/probe', 'status', '--porcelain'],
                        capture_output=True, text=True, check=True)
print(json.dumps({'workspace_clean': result.stdout == ''}))
"""


def gateway_origin(value):
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".modal.run")
        or parsed.path not in ("", "/")
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("gateway must be an HTTPS modal.run origin")
    return value.rstrip("/"), parsed.hostname


def probe_command(gateway_url):
    command = codex_command(MODEL, gateway_url)
    command[command.index("--cd") + 1] = "/tmp/probe"
    command[command.index("--output-last-message") + 1] = "/artifacts/last-message.txt"
    return command


def event_proof(path):
    """Summarize typed events, without trusting assistant prose as command proof."""
    commands = []
    usage = []
    changes = 0
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item", {})
            if not isinstance(item, dict):
                continue
            if event.get("type") == "item.completed":
                if item.get("type") == "command_execution":
                    output = item.get("aggregated_output", "")
                    command = item.get("command", "")
                    commands.append(
                        {
                            "exit_code": item.get("exit_code"),
                            "marker_seen": isinstance(output, str)
                            and output.strip() == "gateway-ready",
                            "printf_seen": isinstance(command, str)
                            and "printf" in command,
                        }
                    )
                elif item.get("type") == "file_change":
                    changes += 1
            if event.get("type") == "turn.completed" and isinstance(
                event.get("usage"), dict
            ):
                usage.append(
                    {
                        key: value
                        for key, value in event["usage"].items()
                        if key
                        in {
                            "input_tokens",
                            "output_tokens",
                            "cached_input_tokens",
                            "cache_write_input_tokens",
                            "reasoning_output_tokens",
                        }
                        and type(value) is int
                        and value >= 0
                    }
                )
    return {
        "commands_completed": len(commands),
        "command_proof": commands,
        "command_completed": len(commands) == 1
        and commands[0]["exit_code"] == 0
        and commands[0]["marker_seen"]
        and commands[0]["printf_seen"],
        "file_change_events": changes,
        "token_usage": usage,
    }


def run_probe(gateway_url, project, environment=None):
    project.require_live()
    project.verify_origin()
    environment = environment or project.environment
    import modal

    gateway_url, hostname = gateway_origin(gateway_url)
    state = common_state(project.root)
    run_id = "probe-" + uuid.uuid4().hex[:16]
    directory = state / "probes" / run_id
    with controller_lock(state):
        # Allocation must precede creation, image building, or any cloud work.
        project.ledger(state / "control.sqlite3").allocate(
            run_id, MODEL_CENTS, infra_cents=INFRA_CENTS
        )
        directory.mkdir(parents=True, mode=0o700)
        started = time.monotonic()
        report = {
            "run_id": run_id,
            "task_id": TASK_ID,
            "status": "starting",
            "started_at": now(),
            "model": MODEL,
            "model_allowance_usd": MODEL_CENTS / 100,
            "infrastructure_allowance_usd": INFRA_CENTS / 100,
            "evidence_directory": str(directory),
            "sandbox_id": None,
            "sandbox_terminated": None,
            "gateway_stopped": False,
            "agent_exit": None,
            "workspace_clean": False,
            "final_ready": False,
            "cleanup_errors": [],
        }
        write_json(directory / "report.json", report)
        functions = {
            name: modal.Function.from_name(
                project.gateway_app, name, environment_name=environment
            )
            for name in ("create_run", "run_status", "stop_run")
        }
        sandbox = None
        token = None
        admission_attempted = False
        interrupted = None
        try:
            admission_attempted = True
            admission = functions["create_run"].remote(
                run_id, [TASK_ID], MODEL_CENTS / 100, 600
            )
            tokens = admission.pop("tokens")
            token = tokens.pop(TASK_ID)
            tokens.clear()
            write_json(directory / "budget-start.json", admission)
            image = (
                modal.Image.from_registry(
                    "node:24.6.0-bookworm-slim", add_python="3.12"
                )
                .apt_install("git", "ripgrep", "ca-certificates")
                .run_commands("npm install --global npm@11.16.0 @openai/codex@0.166.0")
                .add_local_file(
                    Path(__file__).with_name("modal_worker.py"),
                    "/opt/swarm/modal_worker.py",
                )
            )
            sandbox = modal.Sandbox.create(
                app=modal.App.lookup(
                project.worker_app, environment_name=environment, create_if_missing=True
                ),
                image=image,
                timeout=300,
                runtime="vm",
                cpu=(0.5, 2),
                memory=(512, 2048),
                outbound_domain_allowlist=[hostname],
                tags={"batch": run_id, "ticket": TASK_ID, "mode": "gateway-probe"},
            )
            report.update(sandbox_id=sandbox.object_id, status="preparing")
            write_json(directory / "report.json", report)
            setup = sandbox.exec("python3", "-c", SETUP, timeout=30)
            if consume(setup, directory, "setup") != 0:
                raise RuntimeError("probe_setup_failed")
            report.update(status="working", agent_started_at=now())
            write_json(directory / "report.json", report)
            worker_secret = modal.Secret.from_dict({"CODEX_API_KEY": token})
            token = None
            process = sandbox.exec(
                *probe_command(gateway_url), secrets=[worker_secret], timeout=180
            )
            process.stdin.write(PROMPT)
            process.stdin.write_eof()
            process.stdin.drain()
            report["agent_exit"] = consume(process, directory, "agent")
            report["agent_finished_at"] = now()
        except Exception as exc:  # noqa: BLE001 - persist a safe failure class, never credential-bearing messages
            report.update(status="failed", error=type(exc).__name__)
        except (KeyboardInterrupt, SystemExit) as exc:
            report.update(status="interrupted", error=type(exc).__name__)
            interrupted = exc
        finally:
            token = None
            if sandbox is not None:
                try:
                    final = redact(
                        sandbox.filesystem.read_text("/artifacts/last-message.txt")
                    )
                    (directory / "last-message.txt").write_text(final)
                    report["final_ready"] = final.strip() == "READY"
                except Exception as exc:  # noqa: BLE001 - missing output is expected after failed startup
                    report["cleanup_errors"].append(
                        "final_message:" + type(exc).__name__
                    )
                try:
                    verification = sandbox.exec("python3", "-c", VERIFY, timeout=15)
                    if consume(verification, directory, "verify") == 0:
                        result = json.loads((directory / "verify.jsonl").read_text())
                        report["workspace_clean"] = (
                            result.get("workspace_clean") is True
                        )
                except Exception as exc:  # noqa: BLE001 - continue independent termination after recovery failure
                    report["cleanup_errors"].append("verify:" + type(exc).__name__)
            if admission_attempted:
                try:
                    status = functions["run_status"].remote(run_id)
                    write_json(directory / "budget-status.json", status)
                    report["budget"] = status
                except Exception as exc:  # noqa: BLE001 - stop remains mandatory even when status retrieval fails
                    report["cleanup_errors"].append(
                        "gateway_status:" + type(exc).__name__
                    )
                try:
                    stopped = functions["stop_run"].remote(run_id)
                    write_json(directory / "budget-end.json", stopped)
                    report["budget"] = stopped
                    report["gateway_stopped"] = stopped.get("state") == "stopped"
                except Exception as exc:  # noqa: BLE001 - retain allowance and still terminate the worker
                    report["cleanup_errors"].append(
                        "gateway_stop:" + type(exc).__name__
                    )
            if sandbox is not None:
                try:
                    sandbox.terminate()
                    report["sandbox_terminated"] = True
                except Exception as exc:  # noqa: BLE001 - remote timeout remains the final resource bound
                    report["sandbox_terminated"] = False
                    report["cleanup_errors"].append(
                        "sandbox_terminate:" + type(exc).__name__
                    )
            report.update(event_proof(directory / "agent.jsonl"))
            passed = (
                report["agent_exit"] == 0
                and report["command_completed"]
                and report["file_change_events"] == 0
                and report["workspace_clean"]
                and report["final_ready"]
                and report["sandbox_terminated"] is True
                and report["gateway_stopped"]
                and not report["cleanup_errors"]
            )
            if report["status"] != "interrupted":
                report["status"] = "probe_passed" if passed else "failed"
            report.update(
                finished_at=now(), wall_seconds=round(time.monotonic() - started, 3)
            )
            write_json(directory / "report.json", report)
        if interrupted is not None:
            raise interrupted
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--environment")
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    report = run_probe(args.gateway_url, load_project(args.project), args.environment)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "probe_passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
