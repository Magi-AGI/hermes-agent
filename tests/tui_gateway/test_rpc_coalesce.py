"""Identical concurrent reads share one build; a changed input never joins a stale build.

20+ windows open the model picker or refetch the session list within the same second. Each used to
run the full build (2026-09-13: every tui-rpc worker inside a model.options build). Drives the real
``model.options`` handler against a temp HERMES_HOME; only the payload builder is stubbed so the
test can count builds and hold one in flight.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

_original_stdout = sys.stdout


@pytest.fixture()
def options_handler(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: openrouter\n  default: a/b\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import inventory
    import tui_gateway.server as server

    sys.stdout = _original_stdout
    builds, gate = {"n": 0}, threading.Event()

    def fake_build(ctx, **_kw):
        builds["n"] += 1
        gate.wait(timeout=10)
        return {"providers": [], "model": ctx.current_model, "provider": ctx.current_provider}

    monkeypatch.setattr(inventory, "build_model_options_payload", fake_build)
    yield server._methods["model.options"], builds, gate, home
    gate.set()


def _call_async(handler, rid: str, results: dict) -> threading.Thread:
    thread = threading.Thread(target=lambda: results.__setitem__(rid, handler(rid, {})))
    thread.start()
    return thread


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_windows_opening_the_picker_together_share_one_build(options_handler):
    handler, builds, gate, _home = options_handler
    results: dict = {}
    threads = [_call_async(handler, f"w{i}", results) for i in range(8)]
    assert _wait(lambda: builds["n"] >= 1)
    time.sleep(0.2)  # let the other windows arrive while the first build is in flight
    gate.set()
    for t in threads:
        t.join(timeout=10)

    assert builds["n"] == 1
    assert sorted(r["id"] for r in results.values()) == sorted(f"w{i}" for i in range(8)), (
        "each window must get its own response frame")


def test_a_request_after_a_config_write_never_joins_the_earlier_build(options_handler):
    handler, builds, gate, home = options_handler
    results: dict = {}
    first = _call_async(handler, "before-write", results)
    assert _wait(lambda: builds["n"] == 1)

    config = home / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8") + "display:\n  compact: true\n", encoding="utf-8")
    second = _call_async(handler, "after-write", results)

    assert _wait(lambda: builds["n"] == 2), (
        "a model.options request issued after config.yaml changed joined a build that started "
        "before the change")
    gate.set()
    first.join(timeout=10)
    second.join(timeout=10)


class _Capture:
    def __init__(self) -> None:
        self.frames: dict = {}
        self.arrived = threading.Condition()

    def write(self, obj: dict) -> bool:
        with self.arrived:
            self.frames[obj.get("id")] = obj
            self.arrived.notify_all()
        return True

    def close(self) -> None:
        pass


def test_twenty_windows_share_one_build_and_leave_the_lane_free(options_handler, monkeypatch):
    """Joiners must not park lane workers on the shared build: with 4 catalog workers, parked
    joiners used to fill the lane (other catalog reads starved) and requests queued behind them
    each started a NEW build once the first finished (a convoy of 5 builds for 20 windows)."""
    import tui_gateway.server as server
    from tui_gateway import rpc_coalesce
    from tui_gateway.rpc_lanes import LaneSet, SingleFlight

    _handler, builds, gate, _home = options_handler
    monkeypatch.setattr(server, "_lanes", LaneSet())
    monkeypatch.setattr(rpc_coalesce, "MODEL_OPTIONS", SingleFlight())
    for name in ("model.options", "complete.path"):
        monkeypatch.delitem(server._contracts.METHODS, name, raising=False)
    monkeypatch.setitem(server._methods, "complete.path",
                        lambda rid, params: {"jsonrpc": "2.0", "id": rid, "result": {"items": []}})
    transport = _Capture()

    for i in range(20):
        assert server.dispatch({"id": f"w{i}", "method": "model.options", "params": {}}, transport) is None
    assert _wait(lambda: builds["n"] >= 1)
    server.dispatch({"id": "typing", "method": "complete.path", "params": {}}, transport)
    assert _wait(lambda: "typing" in transport.frames), "a catalog read starved behind parked joiners"

    gate.set()
    assert _wait(lambda: all(f"w{i}" in transport.frames for i in range(20)), timeout=10)
    assert all("result" in transport.frames[f"w{i}"] for i in range(20))
    assert builds["n"] == 1, f"{builds['n']} builds for 20 identical requests"
    server._lanes.shutdown()


def test_a_request_after_a_managed_config_write_never_joins_the_earlier_build(options_handler, tmp_path, monkeypatch):
    handler, builds, gate, _home = options_handler
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("model_overrides: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    results: dict = {}
    first = _call_async(handler, "before-managed-write", results)
    assert _wait(lambda: builds["n"] == 1)

    (managed / "config.yaml").write_text(
        "model_overrides:\n  openrouter:\n    a/b:\n      supports_reasoning: true\n", encoding="utf-8")
    second = _call_async(handler, "after-managed-write", results)

    assert _wait(lambda: builds["n"] == 2), "a request after a MANAGED config write joined the older build"
    gate.set()
    first.join(timeout=10)
    second.join(timeout=10)


class _SlowPeer(_Capture):
    """A window whose socket is backed up: every write blocks for a while (WSTransport waits up to 10 s)."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def write(self, obj: dict) -> bool:
        time.sleep(self.delay)
        return super().write(obj)


def test_one_slow_window_never_delays_the_others_or_keeps_the_finished_build_joinable(options_handler, monkeypatch):
    import tui_gateway.server as server
    from tui_gateway import rpc_coalesce
    from tui_gateway.rpc_lanes import LaneSet, SingleFlight

    _handler, builds, gate, _home = options_handler
    monkeypatch.setattr(server, "_lanes", LaneSet())
    monkeypatch.setattr(rpc_coalesce, "MODEL_OPTIONS", SingleFlight())
    monkeypatch.delitem(server._contracts.METHODS, "model.options", raising=False)
    healthy, slow = _Capture(), _SlowPeer(delay=5.0)

    server.dispatch({"id": "leader", "method": "model.options", "params": {}}, healthy)
    assert _wait(lambda: builds["n"] == 1)
    server.dispatch({"id": "slow", "method": "model.options", "params": {}}, slow)
    for i in range(5):
        server.dispatch({"id": f"w{i}", "method": "model.options", "params": {}}, healthy)
    time.sleep(0.2)
    released = time.monotonic()
    gate.set()

    assert _wait(lambda: all(k in healthy.frames for k in ("leader", "w0", "w1", "w2", "w3", "w4")), timeout=2.0), (
        "healthy windows waited behind one slow window's write")
    assert time.monotonic() - released < 2.0
    # The build is finished; the slow window is still being written to. A new request must build afresh.
    gate.clear()
    server.dispatch({"id": "later", "method": "model.options", "params": {}}, healthy)
    assert _wait(lambda: builds["n"] == 2, timeout=2.0), "a later request joined the finished build"
    gate.set()
    assert _wait(lambda: "slow" in slow.frames and "later" in healthy.frames, timeout=10)
    server._lanes.shutdown()
