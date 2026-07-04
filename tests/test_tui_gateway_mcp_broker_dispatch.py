"""R4: mcp.call must run off the fast-path dispatcher on a dedicated bounded
broker executor, so slow proxied MCP calls never stall the reader thread or the
shared long-handler pool, and a fast RPC stays prompt under broker load."""

import threading
import time

import tools.mcp_tool as mcp_tool
from tui_gateway import server


class FakeTransport:
    """Captures async worker responses and signals when one arrives."""

    def __init__(self):
        self.writes = []
        self.wrote = threading.Event()

    def write(self, obj) -> bool:
        self.writes.append(obj)
        self.wrote.set()
        return True

    def close(self) -> None:
        return None


def _mcp_call_req(rid, call_id):
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "mcp.call",
        "params": {
            "profile": "claudetriad",
            "call_id": call_id,
            "tool": "mcp_magi_archive_search_cards",
            "arguments": {"query": "x"},
        },
    }


def test_default_broker_timeout_is_120s():
    assert mcp_tool._BROKER_DEFAULT_TIMEOUT_SECONDS == 120.0


def test_mcp_call_is_routed_to_the_broker_executor_async(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")
    monkeypatch.setattr(
        "tools.mcp_tool.call_mcp_broker_tool",
        lambda tool_name, arguments, **kwargs: {"call_id": kwargs.get("call_id"), "result": "ok", "queue_wait_ms": 0},
    )
    assert "mcp.call" in server._BROKER_HANDLERS

    transport = FakeTransport()
    # dispatch returns None for async-routed handlers (worker writes the response).
    resp = server.dispatch(_mcp_call_req("r1", "c1"), transport=transport)

    assert resp is None
    assert transport.wrote.wait(timeout=5.0), "broker worker never wrote a response"
    assert transport.writes[0]["result"] == {"call_id": "c1", "result": "ok", "queue_wait_ms": 0}


def test_slow_broker_calls_do_not_starve_a_fast_rpc(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")
    release = threading.Event()

    def blocking_call(tool_name, arguments, **kwargs):
        # Simulate a slow proxied MCP call occupying a broker worker.
        release.wait(timeout=10.0)
        return {"call_id": kwargs.get("call_id"), "result": "done", "queue_wait_ms": 0}

    monkeypatch.setattr("tools.mcp_tool.call_mcp_broker_tool", blocking_call)
    # A fast, inline (non-broker, non-long) RPC used as the starvation probe.
    monkeypatch.setattr(
        "tools.mcp_tool.export_mcp_broker_catalog",
        lambda origin_profile=None: {"profile": origin_profile, "tools": []},
    )

    transports = [FakeTransport() for _ in range(10)]
    try:
        # Fire 10 slow broker calls; each dispatch must return immediately (None)
        # rather than blocking the caller/reader thread on the broker work.
        for i, transport in enumerate(transports):
            t0 = time.monotonic()
            resp = server.dispatch(_mcp_call_req(f"r{i}", f"c{i}"), transport=transport)
            assert resp is None
            assert time.monotonic() - t0 < 1.0, "dispatch of mcp.call blocked the fast path"

        # With every broker worker saturated/queued, a fast RPC must still return
        # promptly inline — proving broker load does not starve the fast path.
        probe = FakeTransport()
        t0 = time.monotonic()
        fast = server.dispatch(
            {"jsonrpc": "2.0", "id": "fast", "method": "mcp.catalog", "params": {"profile": "claudetriad"}},
            transport=probe,
        )
        elapsed = time.monotonic() - t0

        assert fast is not None, "fast RPC should run inline and return a response dict"
        assert fast["result"] == {"profile": "claudetriad", "tools": []}
        assert elapsed < 1.0, f"fast RPC starved by broker load ({elapsed:.2f}s)"
        # The slow broker calls are still pending (none released yet).
        assert not any(tr.wrote.is_set() for tr in transports)
    finally:
        release.set()

    # After release, every broker call completes and writes its response.
    for i, transport in enumerate(transports):
        assert transport.wrote.wait(timeout=10.0), f"broker call {i} never completed"
        assert transport.writes[0]["result"]["result"] == "done"
