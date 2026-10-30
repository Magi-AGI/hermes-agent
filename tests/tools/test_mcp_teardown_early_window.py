"""MCP-P2-2, round 2: the EARLY teardown window — an adopter that lands *during*
``await server.shutdown()``, before ``_servers.pop``.

``shutdown_mcp_servers`` built ``_core._orphaned_adopters`` once, inside its FIRST ``_core._lock``
block, while the key stays in ``_core._servers`` with a live session until the awaited tail runs.
An adoption landing in that window:

  * passes ``_set_profile_policy``'s ``require_live`` re-validation (the session is still non-null),
    so it records a policy and a membership — correctly, at that moment;
  * is absent from the orphan snapshot taken before the await;
  * has its policy swept moments later by the tail's ``_forget_connection_profiles_locked``.

Net effect: no ghost record (round 1 closed that), but the adopter is tool-less and nothing
re-registers it until its own next discovery — the original #106005 symptom. The round-1 teardown
tests all destroy the connection BEFORE the policy write, so they exercise only the LATE window.

This drives the real ordering: a real MCP loop runs the real awaited tail, and the adoption happens
inside the server's own ``shutdown()`` coroutine — i.e. exactly at the await. Fake transport, no
network.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types as types

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import mcp_tool as core
from tools import mcp_tool_lifecycle as lifecycle
from tools import mcp_tool_loop as mcp_loop
from tools import mcp_tool_registration as registration
from tools.registry import ToolRegistry


@contextmanager
def profile(path):
    path.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(path)
    try:
        yield ToolRegistry.current_scope_key()
    finally:
        reset_hermes_home_override(token)


def _tool():
    return types.Tool.model_validate(
        {"name": "alpha", "description": "alpha", "inputSchema": {"type": "object"}})


class _AdoptingServer:
    """A connection whose ``shutdown()`` lets another profile adopt it before it is popped.

    Stands in for the real ``MCPServerTask``: the adoption is the thing the real await leaves room
    for, so running it inside ``shutdown()`` reproduces the window without a transport.
    """

    tool_timeout = 30
    initialize_result = None
    _sampling = None
    _list_published = True

    def __init__(self, name, cfg, adopt):
        self.name = name
        self._config = cfg
        self._tools = [_tool()]
        self.session = SimpleNamespace()
        self._registered_tool_names: list = []
        self._adopt = adopt

    async def shutdown(self):
        # Still live here (session non-null, key present in _servers): require_live admits the
        # adoption, which is correct — the connection has not been destroyed yet.
        self._adopt()
        self.session = None


def test_an_adopter_that_lands_during_the_shutdown_await_is_queued_for_re_registration(tmp_path):
    name = "r12_early_teardown_window"
    cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.mcp_tool._orphaned_adopters", {}), \
            patch("tools.registry.registry", ToolRegistry()):
        adopter_home = tmp_path / "adopter"
        with profile(adopter_home) as scope:
            adopter_scope = scope  # the registry's normalized scope key, not the raw path

        def adopt_mid_teardown():
            with profile(adopter_home):
                registration.register_connected_into_current_scope({name: cfg})

        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = _AdoptingServer(name, cfg, adopt_mid_teardown)
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, cfg)

        mcp_loop._ensure_mcp_loop()
        try:
            with profile(tmp_path / "owner") as owner_again:
                lifecycle.shutdown_mcp_servers(scope=owner_again)
        finally:
            mcp_loop._stop_mcp_loop()

        # The real tail ran: the connection is gone and the adopter's policy went with it.
        assert key not in core._servers, "the awaited tail did not pop the torn-down connection"
        assert (key, adopter_scope) not in core._server_scope_configs, (
            "a policy survived for a destroyed connection")
        # The contract under test: the adopter is queued, so the owner's rediscovery re-registers
        # it instead of leaving it tool-less behind a healthy-looking status.
        assert name in core._orphaned_adopters.get(adopter_scope, set()), (
            "an adopter that joined during the shutdown await was never queued for orphan "
            "re-registration: its tools vanish until its own next discovery pass")
