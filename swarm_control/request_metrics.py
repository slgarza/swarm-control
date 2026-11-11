"""Private bounded gateway timing receipts; never a billing or delivery counter."""

import asyncio
import copy
import math
import re
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock

PHASES = frozenset({"authenticate", "body", "capacity", "reserve", "count_pacing",
                    "count_http", "record_count", "inference_pacing", "recheck",
                    "inference_send", "stream", "stream_auth", "cleanup", "settlement"})
MARKS = frozenset({"first_byte", "completion_event", "cancelled", "cleanup_finished"})
FLAGS = frozenset({"reservation_admitted", "count_http_attempted", "inference_http_attempted",
                   "completion_event_seen", "settlement_returned", "cleanup_finished"})
OUTCOMES = frozenset({"unknown", "authentication_rejected", "request_rejected",
                      "capacity_rejected", "reservation_rejected", "pacing_rejected",
                      "count_rejected", "upstream_rejected", "stream_required",
                      "connection_failed", "stream_interrupted", "cancelled",
                      "completion_observed", "usage_unverified", "handler_failed",
                      "response_not_consumed"})


def _identity(value):
    if (not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is None
            or re.match(r"(?:sk-|gh[pousr]_|github_pat_)", value, re.IGNORECASE)
            or re.fullmatch(r"[a-fA-F0-9]{64}", value)):
        return None
    return value


class RequestMetrics:
    """Retain a bounded process-local window and optionally emit private logs.

    A restart loses in-memory state. Summary counts cover the retained window;
    they do not establish a complete run, provider inference sends, billed cost,
    useful simultaneous workers or qualified deliveries. Sinks receive only
    generated IDs, authenticated public identities and fixed timing categories.
    """

    def __init__(self, *, emit=None, maximum_receipts=128, maximum_emissions=20000,
                 utc_now=None, monotonic=None):
        if type(maximum_receipts) is not int or not 1 <= maximum_receipts <= 1000:
            raise ValueError("invalid timing receipt retention limit")
        if type(maximum_emissions) is not int or not 0 <= maximum_emissions <= 100000:
            raise ValueError("invalid timing log limit")
        self._records = deque(maxlen=maximum_receipts)
        self._maximum_emissions = maximum_emissions
        self._emit = emit
        self._utc = utc_now or (lambda: datetime.now(UTC).isoformat())
        self._monotonic = monotonic or time.monotonic
        self._lock = RLock()
        self._started = self._finished = self._emitted = self._sink_failures = self._dropped_emissions = 0
        self._observer_failures = 0

    def _tick(self):
        try:
            value = self._monotonic()
            return float(value) if type(value) in {int, float} and math.isfinite(value) else None
        except Exception:  # noqa: BLE001 - metrics cannot break accounting or response cleanup
            return None

    def _timestamp(self):
        try:
            value = self._utc()
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(UTC).isoformat() if parsed.utcoffset() is not None else None
        except Exception:  # noqa: BLE001 - failed observation is unknown, not request failure
            return None

    def begin(self):
        with self._lock:
            self._started += 1
        return RequestTiming(self)

    def _publish(self, record, *, terminal):
        with self._lock:
            if terminal:
                self._finished += 1
                self._records.append(copy.deepcopy(record))
            if self._emit is None:
                return
            if self._emitted >= self._maximum_emissions:
                self._dropped_emissions += 1
                return
            self._emitted += 1
            try:
                self._emit(copy.deepcopy(record))
            except Exception:  # noqa: BLE001 - logging must never change money or cleanup
                self._sink_failures += 1

    def snapshot(self, run_id=None):
        with self._lock:
            rows = [copy.deepcopy(r) for r in self._records if run_id is None or r["run_id"] == run_id]
            totals, outcomes = {}, {}
            for row in rows:
                outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
                for name, phase in row["phase_summary"].items():
                    value = totals.setdefault(name, {"observations": 0, "measured_seconds": 0.0,
                                                      "unknown_durations": 0})
                    value["observations"] += phase["observations"]
                    value["measured_seconds"] += phase["measured_seconds"]
                    value["unknown_durations"] += phase["unknown_durations"]
            return {"schema_version": 1, "kind": "gateway_process_timing_window",
                    "scope": "retained_process_local_receipts_not_complete_run",
                    "retained_receipts": len(rows), "completed_receipts_evicted": max(0, self._finished - len(self._records)),
                    "requests_observed_in_process": self._started,
                    "inflight_observations_in_process": self._started - self._finished,
                    "sink_failures": self._sink_failures, "dropped_log_emissions": self._dropped_emissions,
                    "observer_failures": self._observer_failures,
                    "outcomes": outcomes, "phase_summary": totals, "receipts": rows,
                    "provider_inference_cpu_seconds": None, "actual_billed_cost": None,
                    "worker_deliveries": None, "phase_durations_are_not_additive": True}


class RequestTiming:
    def __init__(self, owner):
        self.owner = owner
        self._origin = owner._tick()
        self._finished = False
        self._finish_requested = False
        self._active_phases = 0
        self._bound = False
        self._data = {
            "schema_version": 1, "kind": "gateway_request_timing", "record_stage": "started",
            "metrics_request_id": uuid.uuid4().hex, "run_id": None, "task_id": None,
            "started_at_utc": owner._timestamp(), "finished_at_utc": None, "wall_seconds": None,
            "phases": [], "phase_summary": {}, "marks": {}, "flags": {},
            "outcome": "unknown", "omitted_phase_details": 0,
            "clock": "gateway_controller_monotonic_and_UTC_not_provider_execution",
            "phase_durations_are_not_additive": True,
            "scope": "HTTP_observation_not_billing_or_worker_delivery",
            "inference_call_to_first_byte_seconds": None,
        }

    def bind(self, run_id, task_id):
        try:
            self._data["run_id"], self._data["task_id"] = _identity(run_id), _identity(task_id)
            if not self._bound:
                self._bound = True
                self.owner._publish(self._data, terminal=False)
        except Exception:  # noqa: BLE001 - observer only
            self.owner._observer_failures += 1

    def flag(self, name):
        if name in FLAGS:
            self._data["flags"][name] = True
            self._try_finish()

    def outcome(self, value):
        if not self._finish_requested:
            self._data["outcome"] = value if value in OUTCOMES else "unknown"

    @property
    def current_outcome(self):
        return self._data["outcome"]

    def mark(self, name):
        if name not in MARKS or name in self._data["marks"]:
            return
        self._data["marks"][name] = {"at_utc": self.owner._timestamp(),
                                      "elapsed_seconds": self._elapsed(self._origin)}

    def _elapsed(self, start):
        end = self.owner._tick()
        return round(end - start, 6) if start is not None and end is not None and end >= start else None

    @contextmanager
    def phase(self, name):
        if name not in PHASES:
            yield
            return
        start = self.owner._tick()
        self._active_phases += 1
        row = {"phase": name, "started_at_utc": self.owner._timestamp(),
               "started_elapsed_seconds": self._elapsed(self._origin),
               "finished_at_utc": None, "wall_seconds": None, "outcome": "running"}
        retained = len(self._data["phases"]) < 32
        if retained:
            self._data["phases"].append(row)
        else:
            self._data["omitted_phase_details"] += 1
        try:
            yield
        except BaseException as error:
            row["outcome"] = "cancelled" if isinstance(error, asyncio.CancelledError) else "raised"
            raise
        else:
            row["outcome"] = "returned"
        finally:
            row["finished_at_utc"] = self.owner._timestamp()
            row["wall_seconds"] = self._elapsed(start)
            total = self._data["phase_summary"].setdefault(
                name, {"observations": 0, "measured_seconds": 0.0, "unknown_durations": 0})
            total["observations"] += 1
            if row["wall_seconds"] is None:
                total["unknown_durations"] += 1
            else:
                total["measured_seconds"] += row["wall_seconds"]
            self._active_phases -= 1
            self._try_finish()

    def finish(self, outcome=None):
        if self._finished or self._finish_requested:
            return
        if outcome == "response_not_consumed" and self.current_outcome != "unknown":
            outcome = None
        if outcome is not None:
            self.outcome(outcome)
        self._finish_requested = True
        self._try_finish()

    def _try_finish(self):
        if self._finished or not self._finish_requested or self._active_phases:
            return
        if (self._data["flags"].get("reservation_admitted")
                and not self._data["flags"].get("cleanup_finished")):
            return
        self._finished = True
        self._data.update(record_stage="finished", finished_at_utc=self.owner._timestamp(),
                          wall_seconds=self._elapsed(self._origin))
        sent = next((p["started_elapsed_seconds"] for p in self._data["phases"]
                     if p["phase"] == "inference_send"), None)
        first = self._data["marks"].get("first_byte", {}).get("elapsed_seconds")
        if sent is not None and first is not None and first >= sent:
            self._data["inference_call_to_first_byte_seconds"] = round(first - sent, 6)
        self.owner._publish(self._data, terminal=True)
