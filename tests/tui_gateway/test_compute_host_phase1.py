import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tui_gateway import compute_host, server
from tui_gateway.compute_host import ComputeHost, _default_workers
from tui_gateway.host_supervisor import (
    MUTATOR_ROUTE_TABLE,
    HostSupervisor,
    append_log_record,
)


def _json_lines(out: io.StringIO) -> list[dict]:
    frames = []
    for line in out.getvalue().splitlines():
        if line.strip():
            frames.append(json.loads(line))
    return frames


def _wait_for_frame(out: io.StringIO, predicate, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in _json_lines(out):
            if predicate(frame):
                return frame
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for frame; saw={_json_lines(out)}")


def test_compute_host_workers_inherit_tui_pool_env_or_8(monkeypatch):
    monkeypatch.delenv("HERMES_TUI_RPC_POOL_WORKERS", raising=False)
    monkeypatch.delenv("HERMES_COMPUTE_HOST_WORKERS", raising=False)
    assert _default_workers() == 8

    monkeypatch.setenv("HERMES_TUI_RPC_POOL_WORKERS", "11")
    assert _default_workers() == 11

    # Dead-RC tombstone: malformed env falls back to 8, not the old except-branch 4.
    monkeypatch.setenv("HERMES_TUI_RPC_POOL_WORKERS", "not-an-int")
    assert _default_workers() == 8


def test_mutator_route_table_matches_prd_inventory():
    assert MUTATOR_ROUTE_TABLE == {
        "prompt.submit": "turn-path",
        "session.interrupt": "turn-path",
        "reload.mcp": "run-concurrent",
        "session.save": "run-concurrent",
        "session.compress": "idle-gated",
        "prompt.submit.truncate": "idle-gated",
        "slash.model": "idle-gated",
        "slash.personality": "idle-gated",
        "slash.prompt": "idle-gated",
        "slash.compress": "idle-gated",
        "session.reset": "idle-gated",
        "session.history.reload": "idle-gated",
        "slash.retry": "idle-gated",
    }


def test_append_log_record_single_write_lines(tmp_path):
    path = tmp_path / "agent.log"

    def writer(i: int) -> None:
        append_log_record(path, f"line-{i:03d}-" + ("x" * 2000))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 32
    assert sorted(line.split("-", 2)[1] for line in lines) == [f"{i:03d}" for i in range(32)]
    assert all(line.endswith("x" * 2000) for line in lines)


def test_supervisor_startup_reconcile_pid_reuse_guard(tmp_path, monkeypatch):
    registry = tmp_path / "dashboard-compute-host.json"
    registry.write_text(json.dumps({"host_pid": os.getpid(), "boot_id": "stale"}), encoding="utf-8")

    killed: list[int] = []
    supervisor = HostSupervisor(registry_path=registry, argv=[sys.executable, "-c", ""], autostart=False)
    monkeypatch.setattr(supervisor, "_pid_matches_compute_host", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_terminate_pid", lambda pid, **_kw: killed.append(pid))

    result = supervisor.reconcile_startup_orphan()

    assert result == "pid-reuse-ignored"
    assert killed == []
    assert not registry.exists()


def _make_compress_host_session(events: list) -> dict:
    class _Agent:
        model = "host-model"
        provider = "host-provider"
        tools = []
        _cached_system_prompt = ""
        session_input_tokens = 1
        session_output_tokens = 1
        session_prompt_tokens = 1
        session_completion_tokens = 1
        session_total_tokens = 2
        session_api_calls = 1
        session_id = "rotated-id"

    agent = _Agent()
    agent.context_compressor = type("ContextEngineStub", (), {})()
    agent.context_compressor.on_session_start = (
        lambda *_args, **_kwargs: events.append("notify")
    )
    return {
        "agent": agent,
        "session_key": "before-key",
        "history": [
            {"role": "user", "content": "before"},
            {"role": "assistant", "content": "before"},
        ],
        "history_lock": threading.Lock(),
        "history_version": 2,
        "running": False,
        "manual_compression_lock": threading.Lock(),
    }


def _record_finalize(monkeypatch, events: list[str], *sids: str) -> None:
    """Give ``flush_all_sessions`` sessions and record which ones finalize."""
    keys = sids or ("s1",)
    monkeypatch.setattr(
        server,
        "_sessions",
        {sid: {"session_key": sid} for sid in keys},
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "_finalize_session",
        lambda _session, end_reason="tui_close": events.append(
            f"finalize:{_session['session_key']}:{end_reason}"
        ),
        raising=False,
    )


def _register_turn(host: ComputeHost, fn, sid: str = "s1") -> None:
    """Submit a turn exactly the way ``_handle_turn_start`` does."""
    host._track_turn_future(host._executor.submit(fn), sid)


def test_shutdown_drains_in_flight_turn_before_finalizing_sessions(monkeypatch):
    events: list[str] = []
    _record_finalize(monkeypatch, events)

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    running = threading.Event()

    def _turn() -> None:
        running.set()
        time.sleep(0.3)
        events.append("turn_end")

    _register_turn(host, _turn, sid="s1")
    assert running.wait(timeout=5.0)

    host.shutdown(reason="sigterm", wait=3.0)

    # ``_finalize_session`` latches on ``session["_finalized"]``, so its single
    # run has to observe the finished turn or the tail is unpersistable. A turn
    # that *did* drain must still finalize — the live-turn skip must not
    # over-reach into sessions whose work is done.
    assert events == ["turn_end", "finalize:s1:compute_host_sigterm"]

    # The done-callback still has to remove the entry now that the container is
    # a dict: ``set.discard`` was a valid bare callback, ``dict.pop`` is not.
    deadline = time.monotonic() + 2.0
    while host._turn_futures and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host._turn_futures == {}, "in-flight turns must not accumulate"


def test_shutdown_retains_a_live_turns_session_when_the_drain_deadline_expires(monkeypatch):
    wait = 1.0
    events: list[str] = []
    _record_finalize(monkeypatch, events, "live", "idle")

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        started = time.monotonic()
        host.shutdown(reason="sigterm", wait=wait)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    # ``_finalize_session`` is one-shot, and the ``shutdown(wait=False)`` that
    # follows does not join the turn. Spending "live"'s single latch mid-turn
    # would leave it permanently un-finalizable and release its active-session
    # lease out from under running work — the same lifecycle race the drain
    # exists to close, just moved past the deadline. It is retained unfinalized
    # for recovery instead. A turn outliving the window must not cost the flush
    # for anyone else, so "idle" still finalizes in the same pass.
    assert events == ["finalize:idle:compute_host_sigterm"]
    assert elapsed < wait


def test_shutdown_retains_live_sessions_within_the_stdin_closed_budget(monkeypatch):
    """The tightest real budget any caller uses is ``wait=2.0``.

    ``run_host`` finalizes through ``host.shutdown(reason="stdin_closed",
    wait=2.0)``, which is where the reserve — ``wait`` minus
    ``min(_FLUSH_RESERVE_SECS, wait / 2)`` — has the least room to work with.
    The retain-live-sessions rule must hold there without costing the flush for
    idle sessions and without pushing the call past the budget the supervisor's
    kill escalation is timed against.
    """
    wait = 2.0
    drain_budget = wait - min(compute_host._FLUSH_RESERVE_SECS, wait / 2.0)

    events: list[str] = []
    _record_finalize(monkeypatch, events, "live", "idle")

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        started = time.monotonic()
        host.shutdown(reason="stdin_closed", wait=wait)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert events == ["finalize:idle:compute_host_stdin_closed"]
    assert elapsed >= drain_budget - 1e-6, "the drain must use its full window"
    assert elapsed < wait


def test_shutdown_drain_sleep_never_overshoots_the_reserve(monkeypatch):
    """The drain's per-tick sleep must be bounded by the time left to it.

    A flat tick overshoots the drain deadline by up to one tick, eating the
    reserve held back for ``flush_all_sessions``; for a small ``wait`` that is
    the whole reserve. Asserting on the *requested* sleep totals rather than on
    wall-clock keeps this deterministic: each sleep is clamped to the remaining
    time, so the sum can never exceed the drain budget however the scheduler
    interleaves.
    """
    wait = 0.34
    drain_budget = wait - min(compute_host._FLUSH_RESERVE_SECS, wait / 2.0)

    events: list[str] = []
    _record_finalize(monkeypatch, events, "idle")

    slept: list[float] = []
    real_sleep = time.sleep

    def _recording_sleep(seconds: float) -> None:
        slept.append(seconds)
        real_sleep(seconds)

    monkeypatch.setattr(compute_host.time, "sleep", _recording_sleep)

    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    release = threading.Event()
    running = threading.Event()

    def _stuck_turn() -> None:
        running.set()
        release.wait(timeout=30.0)

    _register_turn(host, _stuck_turn, sid="live")
    assert running.wait(timeout=5.0)

    try:
        host.shutdown(reason="sigterm", wait=wait)
    finally:
        release.set()

    assert events == ["finalize:idle:compute_host_sigterm"]
    assert slept, "the drain loop should have ticked at least once"
    assert sum(slept) <= drain_budget + 1e-6


# --- Code-0 / zombie fix: parent guard + supervisor classify/watchdog ----------

def test_parent_guard_exits_18_after_debounce(monkeypatch):
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)  # guard thread NOT auto-started
    host._guard_poll_secs = 0.01
    monkeypatch.setattr(host, "_parent_alive", lambda: False)
    monkeypatch.setattr(host, "shutdown", lambda **_k: None)
    seen = {}

    def _fake_exit(code):
        seen["code"] = code
        host._closed.set()          # break the loop
        raise SystemExit(code)      # unwind the guard thread

    monkeypatch.setattr(compute_host.os, "_exit", _fake_exit)
    t = threading.Thread(target=host._parent_guard_loop, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert seen["code"] == compute_host._ORPHAN_EXIT_CODE
    assert host._exit_code == compute_host._ORPHAN_EXIT_CODE   # set BEFORE shutdown


def test_parent_guard_ignores_transient_dead_reading(monkeypatch):
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    host._guard_poll_secs = 0.01
    seq = iter([False, True, False, False, True])  # never 3 consecutive dead
    monkeypatch.setattr(host, "_parent_alive", lambda: next(seq, True))
    exited = {}
    monkeypatch.setattr(compute_host.os, "_exit", lambda c: exited.setdefault("code", c))
    t = threading.Thread(target=host._parent_guard_loop, daemon=True)
    t.start()
    time.sleep(0.15)
    host._closed.set()
    t.join(timeout=1.0)
    assert "code" not in exited      # fewer than N consecutive -> no exit


def test_classify_exit_distinguishes_orphan_clean_crash():
    assert HostSupervisor._classify_exit(compute_host._ORPHAN_EXIT_CODE)[0] == "host_orphan_exit"
    assert HostSupervisor._classify_exit(0)[0] == "host_exit"    # NOT "crash"
    assert HostSupervisor._classify_exit(1)[0] == "crash"


def test_watchdog_kills_silent_host(monkeypatch):
    monkeypatch.setenv("HERMES_HOST_HEARTBEAT_TIMEOUT_SECS", "5")
    sup = HostSupervisor(heartbeat_secs=1, autostart=False, rpc_sink=lambda _o: None)
    assert sup._hb_timeout_secs == 5.0            # floor 3*1=3 -> 5 stands

    class _FakeProc:
        pid = 999999

        def poll(self):
            return None                           # alive

    fake = _FakeProc()
    with sup._lock:
        sup._proc = fake
        sup._last_hb_monotonic = time.monotonic() - 100.0   # long-silent
    killed = {}
    monkeypatch.setattr(sup, "_kill_process_tree", lambda pid: killed.setdefault("pid", pid))
    sup._watchdog_check_once(sup._hb_timeout_secs)
    assert killed["pid"] == fake.pid
    sup._closing = True                           # stop the daemon watchdog loop
    with sup._lock:
        sup._proc = None


def test_watchdog_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_HOST_HEARTBEAT_TIMEOUT_SECS", raising=False)
    sup = HostSupervisor(heartbeat_secs=1, autostart=False, rpc_sink=lambda _o: None)
    assert sup._hb_timeout_secs == 0.0


def test_watchdog_env_malformed_does_not_brick(monkeypatch):
    monkeypatch.setenv("HERMES_HOST_HEARTBEAT_TIMEOUT_SECS", "not-a-number")
    sup = HostSupervisor(heartbeat_secs=1, autostart=False, rpc_sink=lambda _o: None)
    assert sup._hb_timeout_secs == 0.0            # construction survives


# ── RelayBuffer integration on the supervisor (relay extraction) ───────────

# #6: recovery — child exit while rpc_sink THROWS must still respawn + fail turns.
def test_recovery_survives_throwing_sink():
    def sink(_msg):
        raise RuntimeError("WS down")

    sup = HostSupervisor(autostart=False, rpc_sink=sink, expected_build_sha="test")
    respawned = []
    sup._maybe_respawn_after_crash = lambda: respawned.append(True)
    completed = []
    with sup._lock:
        sup._pending_turns["req1"] = ("sX", lambda frame: completed.append(frame))

    class _FakeProc:
        def wait(self):
            return 1

    sup._proc = _FakeProc()
    try:
        sup._wait_for_exit(sup._proc)
        assert respawned, "respawn was NOT initiated after child exit"
        assert len(completed) == 1, f"pending turn not failed exactly once: {completed}"
        assert completed[0].get("type") == "turn.error", completed[0]
        assert sup._relay._thread.is_alive(), "relay thread died during recovery"
    finally:
        sup._relay.stop()


# #24 (R4): shutdown() with proc is None still stops the relay AND removes the registry.
def test_shutdown_stops_relay_when_proc_none(tmp_path):
    registry = tmp_path / "reg.json"
    registry.write_text("{}", encoding="utf-8")
    sup = HostSupervisor(
        registry_path=registry, autostart=False, rpc_sink=lambda _o: None, expected_build_sha="test"
    )
    assert sup._proc is None
    assert sup._relay._thread.is_alive()
    sup.shutdown()
    assert sup._relay._stopping is True, "relay not stopped on proc-is-None shutdown"
    assert not sup._relay._thread.is_alive(), "relay thread not joined on shutdown"
    assert not registry.exists(), "registry not removed on proc-is-None shutdown"


# #25 (R5): _wait_for_exit enqueues ALL old-turn errors BEFORE respawn, and every
# completion callback fires AFTER respawn; plus code-18 classifies as orphan (Q3).
def test_wait_for_exit_enqueues_errors_before_callbacks(monkeypatch):
    order: list = []

    class _FakeRelay:
        def enqueue(self, kind, frame):
            order.append(("enqueue", frame.get("sid")))

        def stop(self, timeout: float = 2.0):
            pass

    sup = HostSupervisor(autostart=False, rpc_sink=lambda _o: None, expected_build_sha="test")
    sup._relay.stop()          # retire the real relay thread
    sup._relay = _FakeRelay()  # observe ordering deterministically
    monkeypatch.setattr(sup, "_maybe_respawn_after_crash", lambda: order.append(("respawn", None)))

    def mk_cb():
        def cb(frame):
            order.append(("callback", frame.get("sid")))
        return cb

    with sup._lock:
        sup._pending_turns["r1"] = ("sA", mk_cb())
        sup._pending_turns["r2"] = ("sB", mk_cb())

    class _FakeProc:
        def wait(self):
            return 1

    sup._proc = _FakeProc()
    sup._wait_for_exit(sup._proc)

    kinds = [o[0] for o in order]
    assert kinds.count("enqueue") == 2 and kinds.count("callback") == 2, order
    respawn_idx = kinds.index("respawn")
    assert all(k == "enqueue" for k in kinds[:respawn_idx]), f"error not enqueued before respawn: {order}"
    assert all(k == "callback" for k in kinds[respawn_idx + 1:]), f"callback ran before respawn: {order}"
    # Q3: code-18 exit classifies as orphan, not crash.
    assert HostSupervisor._classify_exit(18)[0] == "host_orphan_exit"


# #26: a failing autostart must tear down the already-started relay thread — the
# caller gets no object to shut down, so an uncaught start() would leak it.
def test_constructor_cleans_up_relay_on_startup_failure(monkeypatch):
    def _live_relay_threads():
        return [t for t in threading.enumerate() if t.name == "compute-host-relay" and t.is_alive()]

    before = len(_live_relay_threads())

    def _boom(self):
        raise RuntimeError("spawn failed (no hello)")

    monkeypatch.setattr(HostSupervisor, "start", _boom)
    with pytest.raises(RuntimeError, match="spawn failed"):
        HostSupervisor(autostart=True, rpc_sink=lambda _o: None, expected_build_sha="test")

    # stop() joins synchronously before the re-raise, so the relay thread is gone.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(_live_relay_threads()) > before:
        time.sleep(0.01)
    assert len(_live_relay_threads()) == before, "relay thread leaked on failed startup"
