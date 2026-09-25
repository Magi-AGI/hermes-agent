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
