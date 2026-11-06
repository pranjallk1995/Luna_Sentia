"""The monitor's memory: calls in flight, recent calls and samples, and rows waiting to be stored."""

from __future__ import annotations

import itertools
import threading
import time
from collections import Counter, deque
from typing import Any

from app.calls import Call, apply_timings
from app.config import MonitorConfig

SUMMED = ("gpu_util", "mem_util", "power_w", "sm_clock_mhz", "mem_clock_mhz", "fan_pct")
PEAKED = ("gpu_util", "vram_used_mb", "temp_c", "in_flight", "waiting", "ollama_vram_mb")


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


class LiveState:
    def __init__(self, parallel: int = 1, clock=time.time) -> None:
        self.parallel = parallel
        self.clock = clock
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self.in_flight: dict[int, Call] = {}
        self.calls: deque[tuple[int, Call]] = deque(maxlen=MonitorConfig.LIVE_CALLS)
        self.samples: deque[dict[str, Any]] = deque(maxlen=MonitorConfig.LIVE_SAMPLES)
        self.models: list[dict[str, Any]] = []
        self.gpu_name: str | None = None
        self.gpu_problem: str | None = None
        self.engine_problem: str | None = None
        # Rows for the database, taken by the flusher.
        self.pending_calls: list[Call] = []
        self.pending_samples: list[dict[str, Any]] = []
        self.pending_events: list[dict[str, Any]] = []
        self._bucket: list[dict[str, Any]] = []
        self._bucket_start: int | None = None

    # Calls

    def begin(self, app: str, endpoint: str, request: dict[str, Any]) -> Call:
        with self._lock:
            call = Call(id=next(self._ids), started_at=self.clock(), app=app, endpoint=endpoint, ahead=len(self.in_flight), **request)
            self.in_flight[call.id] = call
            return call

    def finish(self, call: Call, status: int | None, outcome: str, tail: bytes = b"", ttfb: float | None = None,
               response_bytes: int = 0, error: str | None = None, found: dict[str, int] | None = None) -> None:
        with self._lock:
            if call.id not in self.in_flight:
                return
            del self.in_flight[call.id]
            call.finished_at = self.clock()
            call.wall_ms = (call.finished_at - call.started_at) * 1000
            call.ttfb_ms = (ttfb - call.started_at) * 1000 if ttfb else None
            call.status = status
            call.outcome = outcome
            call.response_bytes = response_bytes
            call.error = error[:500] if error else None
            if found:
                apply_timings(call, found)
            self.calls.append((next(self._seq), call))
            self.pending_calls.append(call)
            del self.pending_calls[: -MonitorConfig.PENDING_LIMIT]

    def queue(self) -> tuple[int, int]:
        """Requests running and waiting: each model runs ``parallel`` at once and queues the rest."""
        with self._lock:
            per_model = Counter(call.model for call in self.in_flight.values())
        running = sum(min(count, self.parallel) for count in per_model.values())
        return running, sum(per_model.values()) - running

    # Models Ollama has loaded

    def set_models(self, models: list[dict[str, Any]]) -> None:
        now = self.clock()
        with self._lock:
            before = {model["name"]: model for model in self.models}
            after = {model["name"]: model for model in models}
            for name in after.keys() - before.keys():
                self.pending_events.append({"at": now, "model": name, "event": "loaded",
                                            "vram_mb": after[name]["vram_mb"], "context": after[name].get("context")})
            for name in before.keys() - after.keys():
                self.pending_events.append({"at": now, "model": name, "event": "unloaded",
                                            "vram_mb": before[name]["vram_mb"], "context": before[name].get("context")})
            self.models = models

    # Samples

    def add_sample(self, gpu: dict[str, Any] | None) -> dict[str, Any]:
        running, waiting = self.queue()
        with self._lock:
            ollama_vram = sum(model["vram_mb"] for model in self.models) if self.models else 0.0
        sample = {"at": self.clock(), **(gpu or {}), "running": running, "waiting": waiting,
                  "in_flight": running + waiting, "ollama_vram_mb": ollama_vram}
        with self._lock:
            self.samples.append(sample)
            self._summarize(sample)
        return sample

    def _summarize(self, sample: dict[str, Any]) -> None:
        """Fold one-second samples into the five-second rows that are stored."""
        size = MonitorConfig.STORE_BUCKET_SECONDS
        start = int(sample["at"] // size * size)
        if self._bucket_start is not None and start != self._bucket_start and self._bucket:
            self.pending_samples.append(self._row(self._bucket_start, self._bucket))
            del self.pending_samples[: -MonitorConfig.PENDING_LIMIT]
            self._bucket = []
        self._bucket_start = start
        self._bucket.append(sample)

    @staticmethod
    def _row(start: int, samples: list[dict[str, Any]]) -> dict[str, Any]:
        def values(key: str) -> list[float]:
            return [s[key] for s in samples if s.get(key) is not None]

        row: dict[str, Any] = {"at": start}
        for key in SUMMED:
            row[f"{key}_avg"] = mean(values(key))
        for key in PEAKED:
            found = values(key)
            row[f"{key}_max"] = max(found) if found else None
        row["vram_total_mb"] = max(values("vram_total_mb"), default=None)
        row["busy_pct"] = 100 * sum(1 for s in samples if s.get("in_flight")) / len(samples)
        return row

    def take_pending(self) -> tuple[list[Call], list[dict[str, Any]], list[dict[str, Any]]]:
        with self._lock:
            taken = self.pending_calls, self.pending_samples, self.pending_events
            self.pending_calls, self.pending_samples, self.pending_events = [], [], []
            return taken

    def put_back(self, calls: list[Call], samples: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
        """Rows the database refused, kept for the next try up to the limit."""
        with self._lock:
            self.pending_calls[:0] = calls
            self.pending_samples[:0] = samples
            self.pending_events[:0] = events
            for rows in (self.pending_calls, self.pending_samples, self.pending_events):
                del rows[: -MonitorConfig.PENDING_LIMIT]

    # What the live page reads

    def snapshot(self, after_call: int = 0, since: float = 0.0) -> dict[str, Any]:
        with self._lock:
            return {
                "now": self.clock(),
                "gpu": {"name": self.gpu_name, "problem": self.gpu_problem},
                "engine_problem": self.engine_problem,
                "parallel": self.parallel,
                "samples": [s for s in self.samples if s["at"] > since],
                "in_flight": [call.view() for call in self.in_flight.values()],
                "calls": [call.view() for seq, call in self.calls if seq > after_call],
                "last_call": self.calls[-1][0] if self.calls else 0,
                "models": list(self.models),
            }
