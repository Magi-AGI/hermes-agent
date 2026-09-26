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


def test_the_refresh_follows_the_sessions_own_profile(monkeypatch, tmp_path):
    """Round 1 (Codex, pre-existing): a bare thread dropped the caller's context, so a profile-B
    session joined profile A's discovery (or none) and stopped before B's tools landed."""
    import tui_gateway.entry as entry
    import tui_gateway.server as server
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    homes = []

    def join(timeout=None):
        homes.append(hermes_home_key())
        return True

    monkeypatch.setattr(entry, "mcp_discovery_in_flight", lambda: True)
    monkeypatch.setattr(entry, "join_mcp_discovery", join)
    agent = SimpleNamespace(_user_turn_count=0, _api_call_count=0)
    monkeypatch.setitem(server._sessions, "s-b", {"agent": agent})
    monkeypatch.setattr("tools.mcp_tool_agent.refresh_agent_mcp_tools", lambda _agent, quiet_mode=True: set())
    profile_b = tmp_path / "profiles" / "b"
    profile_b.mkdir(parents=True)
    token = set_hermes_home_override(profile_b)
    try:
        expected = hermes_home_key()
        server._schedule_mcp_late_refresh("s-b", agent)
    finally:
        reset_hermes_home_override(token)
    deadline = time.monotonic() + 10
    while not homes and time.monotonic() < deadline:
        time.sleep(0.05)
    assert homes and homes[0] == expected
