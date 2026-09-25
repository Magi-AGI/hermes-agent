"""A parked server's unattended self-probe backs off (gate item G9).

Every ``_PARKED_RETRY_INTERVAL`` a server that is simply not there (a Unity bridge with no
editor running) respawned its launcher, spent its whole connect timeout and flooded the stderr
log, for the life of the process. Failed probes now double the wait up to a cap; an explicit
reconnect request, or a healthy session, starts over.
"""

import asyncio

import tools.mcp_tool as mcp_tool
from tools.mcp_tool import MCPServerTask


def test_failed_self_probes_back_off_to_the_cap_and_an_explicit_reconnect_resets(monkeypatch):
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 10)
    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_MAX_INTERVAL", 60, raising=False)
    server = MCPServerTask("unity")
    monkeypatch.setattr(server, "_deregister_tools", lambda: None)
    monkeypatch.setattr(server, "_still_configured_enabled", lambda: True)
    waits: list = []
    outcomes = iter(["self-probe"] * 5 + ["reconnect", "self-probe"])

    async def fake_wait(timeout=None):
        waits.append(timeout)
        return next(outcomes)

    monkeypatch.setattr(server, "_wait_for_reconnect_or_shutdown", fake_wait)

    async def park_repeatedly():
        for _ in range(7):
            assert await server._park("probe failed") is False

    asyncio.run(park_repeatedly())
    assert waits == [10, 20, 40, 60, 60, 60, 10]
