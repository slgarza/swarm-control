"""Bounded local argv execution for explicitly requested desktop operations."""

import os
import re
import selectors
import signal
import subprocess
import time


def redact(text, environment=()):
    if isinstance(environment, dict):
        for name, value in environment.items():
            if value and re.search(r"TOKEN|SECRET|API_KEY", name, re.IGNORECASE):
                text = text.replace(value, "[credential removed]")
    return re.sub(r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|Bearer\s+[^\s\"']+)",
                  "[credential removed]", text, flags=re.IGNORECASE)


def execute(argv, *, cwd, environment, timeout=1200, maximum=4 * 1024 * 1024, capture_bytes=False):
    """No shell or target stdin; cap capture and stop the owned process group.

    Raw capture exists only in bounded memory. Only redacted output is returned
    to the caller for optional private persistence and operator display.
    """
    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    output = bytearray()
    deadline = time.monotonic() + timeout
    reason = None
    os.set_blocking(process.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while process.poll() is None:
            if time.monotonic() >= deadline:
                reason = "deadline_reached"
                break
            for _key, _events in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                remaining = maximum - len(output)
                output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    reason = "output_limit_reached"
                    break
            if reason:
                break
        # A naturally exited parent can leave descendants holding stdout open.
        # End only the group created for this operation, including that case.
        for stop_signal in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, stop_signal)
            except ProcessLookupError:
                break
            if stop_signal == signal.SIGTERM:
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                time.sleep(0.05)
        process.wait(timeout=2)
        drain_deadline = time.monotonic() + 1
        while selector.get_map() and time.monotonic() < drain_deadline:
            for _key, _events in selector.select(0.05):
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                remaining = maximum - len(output)
                output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    reason = reason or "output_limit_reached"
        result = {"exit_code": process.returncode if reason is None else 124 if reason == "deadline_reached" else 125,
                "output": redact(output.decode("utf-8", "replace"), environment), "stop_reason": reason,
                "output_truncated": reason == "output_limit_reached"}
        if capture_bytes:
            result["bytes"] = bytes(output)
        return result
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)


def validation_environment(environment):
    return {key: value for key, value in environment.items()
            if not re.search(r"TOKEN|SECRET|(?:^|_)API_KEY$|^OPENAI_ADMIN", key, re.IGNORECASE)}
