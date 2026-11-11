"""Bounded worker phase measurements and safe controller-observed event counters."""

import copy
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock

PHASES = frozenset({"provision", "prepare", "barrier", "agent", "collect", "check",
                    "recover", "artifacts", "terminate"})
EVENTS = frozenset({"thread.started", "turn.started", "turn.completed", "turn.failed",
                    "error", "item.started", "item.completed"})
ITEMS = frozenset({"command_execution", "file_change", "agent_message", "reasoning",
                   "mcp_tool_call", "web_search"})


class WorkerProgress:
    """Observe known signals without copying IDs, messages, commands or model text.

    Phase durations use the controller's monotonic clock. Event times describe
    receipt, not provider inference, command execution or continuous useful work.
    Event checkpointing is bounded to one write per five seconds; phase changes
    are written immediately. A process exit never establishes a remote kill cause.
    """

    def __init__(self, checkpoint=None, *, utc_now=None, monotonic=None):
        self._utc = utc_now or (lambda: datetime.now(UTC).isoformat())
        self._monotonic = monotonic or time.monotonic
        self._checkpoint = checkpoint
        self._lock = RLock()
        self._last_checkpoint = None
        self._data = {
            "schema_version": 1,
            "phases": [],
            "event_counts": {},
            "item_counts": {},
            "last_event_kind": None,
            "last_event_received_at_utc": None,
            "agent_process_outcome": "not_started",
            "remote_termination_cause": None,
            "event_clock": "controller_receipt_not_provider_execution",
            "model_service_seconds": None,
            "request_queue_seconds": None,
            "checkpoint_failures": 0,
        }

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def _flush(self, *, force=False):
        tick = self._monotonic()
        if force or self._last_checkpoint is None or tick - self._last_checkpoint >= 5:
            self._last_checkpoint = tick
            if self._checkpoint is not None:
                try:
                    self._checkpoint(self.snapshot())
                except Exception:  # noqa: BLE001 - an observer failure must not bypass VM cleanup
                    self._data["checkpoint_failures"] = min(
                        1000000, self._data["checkpoint_failures"] + 1
                    )

    @contextmanager
    def phase(self, name):
        if name not in PHASES:
            raise ValueError("unknown worker phase")
        with self._lock:
            if len(self._data["phases"]) >= 32:
                raise ValueError("worker phase limit exceeded")
            tick = self._monotonic()
            row = {"phase": name, "started_at_utc": self._utc(), "finished_at_utc": None,
                   "wall_seconds": None, "outcome": "running"}
            self._data["phases"].append(row)
            if name == "agent":
                self._data["agent_process_outcome"] = "running"
            self._flush(force=True)
        try:
            yield row
        except BaseException as error:
            with self._lock:
                row["outcome"] = "raised"
                if name == "agent":
                    self._data["agent_process_outcome"] = (
                        "wait_timeout_observed" if isinstance(error, TimeoutError) else "unknown"
                    )
            raise
        else:
            with self._lock:
                row["outcome"] = (
                    "nonzero_exit" if type(row.get("process_exit")) is int
                    and row["process_exit"] != 0 else "returned"
                )
        finally:
            with self._lock:
                row["finished_at_utc"] = self._utc()
                row["wall_seconds"] = round(max(0.0, self._monotonic() - tick), 6)
                self._flush(force=True)

    def agent_exit(self, code):
        with self._lock:
            if type(code) is int and code == 0:
                outcome = "successful_exit"
            elif type(code) is int and code > 0:
                outcome = "nonzero_exit"
            else:
                outcome = "unknown"
            self._data["agent_process_outcome"] = outcome
            self._flush(force=True)

    def event(self, value, received_at):
        if not isinstance(value, dict):
            return
        with self._lock:
            kind = value.get("type")
            kind = kind if isinstance(kind, str) and kind in EVENTS else "other"
            counts = self._data["event_counts"]
            counts[kind] = min(1000000, counts.get(kind, 0) + 1)
            item = value.get("item")
            if kind in {"item.started", "item.completed"} and isinstance(item, dict):
                name = item.get("type")
                name = name if isinstance(name, str) and name in ITEMS else "other"
                counts = self._data["item_counts"]
                key = kind.removeprefix("item.") + ":" + name
                counts[key] = min(1000000, counts.get(key, 0) + 1)
            self._data["last_event_kind"] = kind
            self._data["last_event_received_at_utc"] = received_at
            self._flush()
