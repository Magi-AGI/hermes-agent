"""Sync->async bridges release their caller at the bound (futures audit, gate item G10).

``with ThreadPoolExecutor() as pool: pool.submit(f).result(timeout=t)`` never bounded anything: the
``with`` exit runs ``shutdown(wait=True)``, which joins the still-running call.
"""

import asyncio
import threading
import time

import pytest

from agent.deadline import DeadlineExpired


def _blocking_coro(release: threading.Event, seconds: float):
    async def _wait():
        release.wait(seconds)  # a sync stall inside the coroutine: no asyncio timer can fire
    return _wait()


def test_homeassistant_bridge_releases_caller_inside_running_loop(monkeypatch):
    from tools import homeassistant_tool

    monkeypatch.setattr(homeassistant_tool, "_ASYNC_BRIDGE_TIMEOUT_S", 0.3, raising=False)
    release = threading.Event()

    async def handler_on_loop():
        start = time.monotonic()
        with pytest.raises(DeadlineExpired):
            homeassistant_tool._run_async(_blocking_coro(release, 4.0))
        return time.monotonic() - start

    try:
        elapsed = asyncio.run(handler_on_loop())
    finally:
        release.set()
    assert elapsed < 2.5


def test_edge_tts_releases_caller_at_bound(monkeypatch):
    from tools import tts_tool

    release = threading.Event()

    async def stalled_edge(text, file_str, cfg):
        release.wait(4.0)

    monkeypatch.setattr(tts_tool, "_generate_edge_tts", stalled_edge)
    monkeypatch.setattr(tts_tool, "_EDGE_TTS_TIMEOUT_S", 0.3, raising=False)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            tts_tool._run_edge_tts("hi", "out.mp3", {})
    finally:
        release.set()
    assert time.monotonic() - start < 2.5
