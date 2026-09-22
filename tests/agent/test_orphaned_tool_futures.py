"""Orphaned-future detection in the concurrent tool batch wait loop.

2026-09-13: a turn wedged for 40+ minutes in ``await_completion`` while every pool worker sat
idle — the futures were never going to resolve (lost submit / lost wakeup). The wait loop must
fail such slots as orphaned once NOTHING is executing them, while never cutting short a tool
that is genuinely running (terminal, delegated claude/codex can legitimately take minutes).
"""

import concurrent.futures
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import tool_executor as te


class _Agent:
    _interrupt_requested = False
    log_prefix = ""

    def __init__(self):
        self._tool_worker_threads: set = set()
        self._tool_worker_threads_lock = threading.Lock()

    def _touch_activity(self, desc):
        pass

    def _vprint(self, msg, force=False):
        pass


def _batch(monkeypatch, n=1, grace="0.3"):
    monkeypatch.setenv("HERMES_TOOL_ORPHAN_GRACE_S", grace)
    batch = te._ConcurrentBatch.__new__(te._ConcurrentBatch)
    batch.agent = _Agent()
    batch.parsed_calls = [SimpleNamespace(name=f"skill_view_{i}") for i in range(n)]
    batch.timeout_s = None
    batch.gate = te._StartOrderGate(1.0)
    batch.authorization_gate = te._ConcurrentToolAuthorizationGate()
    batch.timed_out_indices = set()
    batch.orphaned_indices = set()
    batch.orphan_grace_s = te._resolve_orphaned_future_grace()
    batch._inflight_lock = threading.Lock()
    batch._inflight_workers = 0
    batch.poll_interval_s = 0.05
    return batch


def _await(batch, futures, budget=10.0):
    """Run await_completion on a thread so a regression fails the test instead of hanging it."""
    out = {}
    t = threading.Thread(
        target=lambda: out.setdefault("abandoned", batch.await_completion(futures, {f: i for i, f in enumerate(futures)}, None)),
        daemon=True,
    )
    t.start()
    t.join(budget)
    assert not t.is_alive(), "await_completion hung"
    return out["abandoned"]


def test_never_started_futures_are_failed_as_orphaned(monkeypatch):
    batch = _batch(monkeypatch, n=2)
    futures = [concurrent.futures.Future(), concurrent.futures.Future()]  # never picked up by a worker
    start = time.monotonic()
    assert _await(batch, futures) is True
    assert batch.orphaned_indices == {0, 1}
    assert batch.timed_out_indices == set()
    assert time.monotonic() - start < 8.0


def test_running_future_is_never_orphaned(monkeypatch):
    batch = _batch(monkeypatch)
    fut = concurrent.futures.Future()
    assert fut.set_running_or_notify_cancel()  # a worker is executing it
    threading.Timer(1.0, fut.set_result, args=(None,)).start()  # well past the 0.3s grace
    assert _await(batch, [fut]) is False
    assert batch.orphaned_indices == set()


def test_inflight_worker_blocks_orphan_detection(monkeypatch):
    """A worker parked at the start-order or approval gate is inside run_worker: not orphaned."""
    batch = _batch(monkeypatch)
    batch._inflight_workers = 1
    fut = concurrent.futures.Future()  # pending, not yet 'running'
    threading.Timer(1.0, fut.set_result, args=(None,)).start()
    assert _await(batch, [fut]) is False
    assert batch.orphaned_indices == set()


def test_idle_window_resets_when_work_resumes(monkeypatch):
    """Grace must be continuous idleness, not cumulative."""
    batch = _batch(monkeypatch, grace="0.5")
    fut = concurrent.futures.Future()

    def flap():
        for _ in range(6):
            time.sleep(0.3)
            with batch._inflight_lock:
                batch._inflight_workers = 1 - batch._inflight_workers
        with batch._inflight_lock:
            batch._inflight_workers = 1
        time.sleep(0.3)
        fut.set_result(None)

    threading.Thread(target=flap, daemon=True).start()
    assert _await(batch, [fut]) is False
    assert batch.orphaned_indices == set()


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_detection_can_be_disabled(monkeypatch, raw):
    monkeypatch.setenv("HERMES_TOOL_ORPHAN_GRACE_S", raw)
    assert te._resolve_orphaned_future_grace() is None


def test_grace_default_and_invalid(monkeypatch):
    monkeypatch.delenv("HERMES_TOOL_ORPHAN_GRACE_S", raising=False)
    assert te._resolve_orphaned_future_grace() == te._DEFAULT_ORPHANED_FUTURE_GRACE_S
    monkeypatch.setenv("HERMES_TOOL_ORPHAN_GRACE_S", "abc")
    assert te._resolve_orphaned_future_grace() == te._DEFAULT_ORPHANED_FUTURE_GRACE_S


def test_orphaned_result_is_distinct_and_retry_safe():
    agent = _Agent()
    ref = MagicMock()
    ref.name = "skill_view"
    result, duration, disposition = te._unfinished_tool_result(agent, ref, timed_out=False, timeout_s=None, orphaned=True)
    assert "never executed" in result and "orphaned" in result
    assert duration == 0.0 and disposition == "none"
    assert ref.emit_post.call_args.kwargs["error_type"] == "tool_orphaned_future"


def test_run_worker_tracks_inflight(monkeypatch):
    batch = _batch(monkeypatch)
    seen = []
    batch._run_worker_body = lambda i, o: seen.append(batch.inflight_workers())
    batch.run_worker(0, 0)
    assert seen == [1] and batch.inflight_workers() == 0

    def boom(i, o):
        raise RuntimeError("x")

    batch._run_worker_body = boom
    with pytest.raises(RuntimeError):
        batch.run_worker(0, 0)
    assert batch.inflight_workers() == 0


class _StartsOnCancel(concurrent.futures.Future):
    """A future a worker picks up in the window between the idle check and cancel()."""

    def cancel(self):
        self.set_running_or_notify_cancel()
        threading.Timer(0.2, lambda: self.set_result("done")).start()
        return False


def test_future_that_starts_during_cancel_is_not_reported_orphaned(monkeypatch, caplog):
    """Codex review #3: only a successful cancel proves a call never ran ("safe to retry")."""
    batch = _batch(monkeypatch, n=2, grace="0.1")
    racing, lost = _StartsOnCancel(), concurrent.futures.Future()
    with caplog.at_level("ERROR", logger="agent.tool_executor"):
        assert _await(batch, [racing, lost]) is False  # the racing call finished normally
    assert sum("failed as orphaned" in r.message for r in caplog.records) == 1
    assert batch.orphaned_indices == {1}
    assert racing.result(timeout=2) == "done"


def test_cancelled_call_does_not_hold_up_the_start_order_gate(monkeypatch):
    """Codex review round 3 #1: a cancelled earlier call never advances the start-order gate;
    a surviving later call must not wait out the gate timeout behind it."""
    batch = _batch(monkeypatch, n=2, grace="0.1")
    batch.gate = te._StartOrderGate(5.0)
    waited = {}

    class _StartsThenGates(concurrent.futures.Future):
        def cancel(self):
            self.set_running_or_notify_cancel()

            def _work():
                start = time.monotonic()
                batch.gate.begin_in_order(1, tool_name="survivor")
                waited["s"] = time.monotonic() - start
                self.set_result("done")

            threading.Thread(target=_work, daemon=True).start()
            return False

    lost, survivor = concurrent.futures.Future(), _StartsThenGates()
    batch.future_order = {lost: 0, survivor: 1}
    assert _await(batch, [lost, survivor]) is False
    assert batch.orphaned_indices == {0}
    assert waited["s"] < 1.0, f"survivor waited {waited['s']:.1f}s behind a cancelled call"
