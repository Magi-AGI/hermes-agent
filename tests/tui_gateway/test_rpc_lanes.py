"""Long RPCs run in bounded per-class lanes: one saturated class never starves another.

Regression for the shared-backend freezes (2026-09-08 catalog prefetch, 2026-09-13 config convoy):
every slow handler shared ONE 8-worker FIFO, so 8 stuck ``model.options`` builds queued
``session.resume`` for every window of the profile. Drives the real ``dispatch()``.
"""

from __future__ import annotations

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture()
def server(monkeypatch):
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib
        mod = importlib.import_module("tui_gateway.server")
    from tui_gateway.rpc_lanes import LaneSet

    methods = dict(mod._methods)
    # Fresh lanes per test: a stuck handler from one test must not occupy the next test's lanes.
    monkeypatch.setattr(mod, "_lanes", LaneSet(), raising=False)
    for name in ("model.options", "session.resume", "reload.mcp", "session.list", "session.compress", "slash.exec"):
        monkeypatch.delitem(mod._contracts.METHODS, name, raising=False)
    release = threading.Event()
    yield mod, release
    release.set()
    getattr(mod, "_lanes", None) and mod._lanes.shutdown()
    mod._methods.clear()
    mod._methods.update(methods)
    sys.stdout = _original_stdout


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

    def wait_for(self, rid, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        with self.arrived:
            while rid not in self.frames:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.arrived.wait(remaining)
            return self.frames[rid]


def _stuck(release: threading.Event):
    def handler(rid, params):
        release.wait(timeout=30)
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return handler


def _fill(mod, transport, method: str, count: int) -> list:
    return [mod.dispatch({"id": f"{method}-{i}", "method": method, "params": {}}, transport)
            for i in range(count)]


def test_resume_is_served_while_catalog_work_is_stuck(server):
    mod, release = server
    transport = _Capture()
    mod._methods["model.options"] = _stuck(release)
    mod._methods["session.resume"] = lambda rid, params: {"jsonrpc": "2.0", "id": rid, "result": {"resumed": True}}

    # Every window opens the model picker at once and every build hangs (a deliberately stuck job).
    _fill(mod, transport, "model.options", 12)

    assert mod.dispatch({"id": "resume", "method": "session.resume", "params": {}}, transport) is None
    frame = transport.wait_for("resume", timeout=5)
    assert frame is not None and frame["result"] == {"resumed": True}, (
        "session.resume waited behind stuck model.options builds: one slow read class starves "
        "every window's resume")


def test_a_full_read_lane_answers_busy_with_auto_retry_and_releases_admission(server):
    from hermes_cli.backend_retirement import retirement
    from tui_gateway.rpc_lanes import BUSY_CODE

    mod, release = server
    transport = _Capture()
    mod._methods["model.options"] = _stuck(release)
    capacity = mod._lanes.for_method("model.options").capacity
    active_before = retirement.active_count()

    accepted = _fill(mod, transport, "model.options", capacity)
    refused = mod.dispatch({"id": "over", "method": "model.options", "params": {}}, transport)

    assert all(r is None for r in accepted)
    assert refused["error"]["code"] == BUSY_CODE
    assert refused["error"]["code"] != 5036, "5036 already means session enumeration failed"
    assert refused["error"]["data"]["retry"] == "auto"
    assert refused["error"]["data"]["retry_after_ms"] > 0
    # A refusal holds no admission: retirement can still drain once the accepted work finishes.
    assert retirement.active_count() == active_before + capacity
    release.set()
    for i in range(capacity):
        assert transport.wait_for(f"model.options-{i}", timeout=10) is not None
    deadline = time.monotonic() + 5
    while retirement.active_count() != active_before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert retirement.active_count() == active_before


def test_a_full_mutating_lane_is_refused_without_auto_retry(server):
    mod, release = server
    transport = _Capture()
    mod._methods["reload.mcp"] = _stuck(release)
    capacity = mod._lanes.for_method("reload.mcp").capacity

    _fill(mod, transport, "reload.mcp", capacity)
    refused = mod.dispatch({"id": "over", "method": "reload.mcp", "params": {}}, transport)

    assert refused["error"]["data"]["retry"] == "none", (
        "a mutating RPC must never be retried automatically; the busy error is surfaced instead")


def test_concurrent_identical_reads_share_one_computation():
    from tui_gateway.rpc_lanes import SingleFlight

    flight, started, release = SingleFlight(), threading.Event(), threading.Event()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        started.set()
        release.wait(timeout=10)
        return {"payload": True}

    results: list = []
    leader = threading.Thread(target=lambda: results.append(flight.run("k", compute)))
    leader.start()
    assert started.wait(timeout=5)
    joiners = [threading.Thread(target=lambda: results.append(flight.run("k", compute))) for _ in range(5)]
    for t in joiners:
        t.start()
    deadline = time.monotonic() + 5
    while flight.joined < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    release.set()
    for t in (leader, *joiners):
        t.join(timeout=5)

    assert calls["n"] == 1 and len(results) == 6
    # Nothing is cached once the shared computation finishes: a later caller computes afresh.
    flight.run("k", compute)
    assert calls["n"] == 2


def test_a_window_slash_command_is_served_while_compactions_run(server):
    mod, release = server
    transport = _Capture()
    mod._methods["session.compress"] = _stuck(release)
    mod._methods["slash.exec"] = lambda rid, params: {"jsonrpc": "2.0", "id": rid, "result": {"output": "ok"}}

    # Several windows run a manual /compress at once; each is a minutes-long model call.
    _fill(mod, transport, "session.compress", 6)

    assert mod.dispatch({"id": "slash", "method": "slash.exec", "params": {}}, transport) is None
    frame = transport.wait_for("slash", timeout=5)
    assert frame is not None and frame["result"] == {"output": "ok"}, (
        "a slash command waited behind other windows' compactions")
