"""The late MCP refresh surfaces each server's tools as they register (gate item G8).

A session built while discovery runs snapshots only what is registered then. The late refresh
used to wait for the WHOLE pass (giving up after 30 s), so one dead server kept every healthy
server's tools out of the sessions opened meanwhile.
"""

import threading
import time
from types import SimpleNamespace

import hermes_cli.mcp_startup as startup
from hermes_constants import hermes_home_key


def test_tools_registered_mid_pass_reach_a_waiting_session(monkeypatch):
    import tui_gateway.server as server
    from tools.registry import registry

    stuck = threading.Event()  # the pass is stuck behind a dead server for the whole test
    discovery = threading.Thread(target=lambda: stuck.wait(20.0), daemon=True)
    discovery.start()
    monkeypatch.setattr(startup, "_mcp_discovery_thread", {hermes_home_key(): discovery})

    agent = SimpleNamespace(_user_turn_count=0, _api_call_count=0)
    monkeypatch.setitem(server._sessions, "s1", {"agent": agent})
    emitted = threading.Event()
    registered = {"mcp__fast__ping"}
    monkeypatch.setattr("tools.mcp_tool_agent.refresh_agent_mcp_tools",
                        lambda _agent, quiet_mode=True: set(registered))
    monkeypatch.setattr(server, "_session_info", lambda _agent, _session: {"tools": sorted(registered)})
    monkeypatch.setattr(server, "_emit", lambda *_a: emitted.set())
    try:
        server._schedule_mcp_late_refresh("s1", agent)
        monkeypatch.setattr(registry, "_generation", registry._generation + 1)  # a healthy server registers
        assert emitted.wait(8.0), "tools registered mid-pass never reached the session"
        assert discovery.is_alive()
    finally:
        stuck.set()
        discovery.join(timeout=5.0)
