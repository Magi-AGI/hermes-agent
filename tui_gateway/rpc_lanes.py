"""Bounded RPC lanes and single-flight coalescing for the shared backend.

One ``serve`` backend carries every session window of a profile (20-27 in daily use). Its slow
RPCs used to share ONE 8-worker FIFO, so a burst of one slow read class (model catalogs 09-08,
config loads 09-13) starved ``session.resume`` for every window. Each class now runs in its own
bounded lane: resume never queues behind catalog work, and a lane that is full answers
:data:`BUSY_CODE` at once instead of accepting work that would wait unboundedly.

``BUSY_CODE`` is distinct from every failure code (5036 is session enumeration/deletion failure).
Its ``data.retry`` tells the client what it may do: ``"auto"`` only for idempotent reads (bounded,
jittered retry), ``"none"`` for anything that mutates (the error is surfaced to the user).
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Hashable

logger = logging.getLogger(__name__)

BUSY_CODE = 5037
_STATS_LOG_INTERVAL_S = 30.0


@dataclass(frozen=True)
class LaneSpec:
    name: str
    workers: int
    #: Requests allowed to WAIT beyond the running ones; one more is refused with BUSY_CODE.
    max_queued: int
    #: Every method in the lane is an idempotent read the client may retry automatically.
    auto_retry: bool
    retry_after_ms: int


class Lane:
    """A thread pool with admission: never more than ``workers + max_queued`` requests accepted."""

    def __init__(self, spec: LaneSpec) -> None:
        self.spec = spec
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=spec.workers, thread_name_prefix=f"tui-rpc-{spec.name}")
        self._lock = threading.Lock()
        self._accepted = 0
        self._running = 0
        self._stats = _LaneStats()

    @property
    def capacity(self) -> int:
        return self.spec.workers + self.spec.max_queued

    def try_submit(self, method: str, fn: Callable[[], None]) -> concurrent.futures.Future | None:
        """Schedule *fn*, or return None (caller answers BUSY) when the lane is full."""
        with self._lock:
            if self._accepted >= self.capacity:
                self._stats.refused += 1
                return None
            self._accepted += 1
        enqueued = time.monotonic()

        def run() -> None:
            started = time.monotonic()
            with self._lock:
                self._running += 1
            try:
                fn()
            finally:
                finished = time.monotonic()
                with self._lock:
                    self._running -= 1
                    self._stats.record(method, started - enqueued, finished - started)
                self._maybe_log(finished)

        try:
            future = self._executor.submit(run)
        except BaseException:
            self._release()
            raise
        future.add_done_callback(lambda _f: self._release())
        return future

    def _release(self) -> None:
        with self._lock:
            self._accepted -= 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"lane": self.spec.name, "accepted": self._accepted, "running": self._running,
                    "queued": max(0, self._accepted - self._running), **self._stats.summary()}

    def _maybe_log(self, now: float) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        with self._lock:
            if now - self._stats.logged_at < _STATS_LOG_INTERVAL_S:
                return
            self._stats.logged_at = now
        logger.debug("rpc lane stats %s", self.snapshot())

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class _LaneStats:
    """Bounded rolling samples (wait + run seconds) for DEBUG stats and the concurrency harness."""

    _MAX_SAMPLES = 512

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.runs: dict[str, list[float]] = {}
        self.refused = 0
        self.completed = 0
        self.logged_at = 0.0

    def record(self, method: str, wait_s: float, run_s: float) -> None:
        self.completed += 1
        self.waits.append(wait_s)
        del self.waits[:-self._MAX_SAMPLES]
        samples = self.runs.setdefault(method, [])
        samples.append(run_s)
        del samples[:-self._MAX_SAMPLES]

    def summary(self) -> dict[str, Any]:
        return {"completed": self.completed, "refused": self.refused,
                "wait_s": _percentiles(self.waits),
                "run_s": {method: _percentiles(v) for method, v in self.runs.items()}}


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
    return {"p50": round(pick(0.50), 4), "p95": round(pick(0.95), 4), "max": round(ordered[-1], 4)}


# Lane table. Interactive work (resume) and user-typed commands never share a queue with catalog
# reads or with minutes-long maintenance/relay work. Sizes are totals for the whole backend.
RESUME = LaneSpec("resume", workers=4, max_queued=60, auto_retry=True, retry_after_ms=500)
EXEC = LaneSpec("exec", workers=4, max_queued=16, auto_retry=False, retry_after_ms=1000)
# Minutes-long model calls (manual /compress, one-shot LLM): never in front of a window's slash command.
LLM = LaneSpec("llm", workers=4, max_queued=16, auto_retry=False, retry_after_ms=2000)
CATALOG = LaneSpec("catalog", workers=4, max_queued=32, auto_retry=True, retry_after_ms=750)
MAINTENANCE = LaneSpec("maintenance", workers=3, max_queued=16, auto_retry=False, retry_after_ms=2000)
RELAY = LaneSpec("relay", workers=2, max_queued=8, auto_retry=False, retry_after_ms=2000)

_METHOD_LANES: dict[str, LaneSpec] = {
    **dict.fromkeys(("session.resume", "session.branch"), RESUME),
    **dict.fromkeys(("slash.exec", "command.dispatch", "shell.exec", "cli.exec"), EXEC),
    **dict.fromkeys(("session.compress", "llm.oneshot"), LLM),
    # Idempotent reads only: a full lane answers BUSY and the client retries on its own.
    **dict.fromkeys((
        "model.options", "session.list", "session.active_list", "setup.status", "setup.runtime_check",
        "profiles.list", "profiles.describe", "profiles.get_asset", "billing.state", "subscription.state",
        "subscription.preview", "usage.bars", "session.usage", "pet.cells", "pet.gallery", "pet.info",
        "pet.thumb", "learning.frames", "process.list", "projects.discover_repos", "projects.for_cwd",
        "projects.tree", "projects.project_sessions", "session.foreign.list", "session.foreign.preview",
        "wake.status", "complete.path", "complete.slash"), CATALOG),
    **dict.fromkeys(("bot_relay.roster.sync", "bot_relay.outbox.drain", "bot_relay.deliver",
                     "bot_relay.reply"), RELAY),
}


def lane_spec_for(method: str) -> LaneSpec:
    """Every long handler not named above mutates or is slow and rare: the maintenance lane."""
    return _METHOD_LANES.get(method, MAINTENANCE)


class LaneSet:
    def __init__(self, specs: tuple[LaneSpec, ...] = (RESUME, EXEC, LLM, CATALOG, MAINTENANCE, RELAY)) -> None:
        self._lanes = {spec.name: Lane(spec) for spec in specs}

    def for_method(self, method: str) -> Lane:
        return self._lanes[lane_spec_for(method).name]

    def snapshot(self) -> list[dict[str, Any]]:
        return [lane.snapshot() for lane in self._lanes.values()]

    def shutdown(self) -> None:
        for lane in self._lanes.values():
            lane.shutdown()


def busy_error(rid: Any, method: str, lane: Lane) -> dict:
    spec = lane.spec
    return {"jsonrpc": "2.0", "id": rid, "error": {
        "code": BUSY_CODE,
        "message": f"backend busy: the {spec.name} lane is full; retry shortly",
        "data": {"lane": spec.name, "method": method, "retry": "auto" if spec.auto_retry else "none",
                 "retry_after_ms": spec.retry_after_ms}}}


class SingleFlight:
    """Concurrent callers with the same key share ONE in-flight computation. Nothing is cached after
    it finishes: a caller arriving later starts a fresh one, so a joiner receives exactly what it
    would have got by arriving when the shared computation started. The result is shared READ-ONLY
    (each caller wraps it in its own response frame); callers must not mutate it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: dict[Hashable, concurrent.futures.Future] = {}
        self.joined = 0
        self.led = 0

    def run(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        with self._lock:
            shared = self._inflight.get(key)
            if shared is None:
                shared = concurrent.futures.Future()
                self._inflight[key] = shared
                self.led += 1
                leader = True
            else:
                self.joined += 1
                leader = False
        if not leader:
            return shared.result()
        try:
            result = compute()
        except BaseException as exc:
            shared.set_exception(exc)
            raise
        else:
            shared.set_result(result)
            return result
        finally:
            with self._lock:
                self._inflight.pop(key, None)
