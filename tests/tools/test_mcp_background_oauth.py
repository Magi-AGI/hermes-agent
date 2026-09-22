"""MCP background OAuth (2026-09-22 pass, queue item 11b).

- Background OAuth: a long-lived server task must never run an interactive authorization-code
  flow on a reconnect, even when it was created inside a login (forced-interactive / dashboard)
  context. Live evidence: a keepalive-triggered reconnect bound the loopback listener and waited
  out the full 300 s callback timeout (claudetriad errors.log, 2026-09-16 08:31:33 -> 08:35:35).
- Suppression beats a dashboard flow at both OAuth boundaries.
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _flow():
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    return DashboardOAuthFlow(flow_id="f", server_name="srv", profile=None, hermes_home=".",
                              redirect_uri="https://example.invalid/cb")


# --- suppression beats a dashboard flow ------------------------------------------------------

def test_redirect_handler_suppression_beats_dashboard_flow():
    from tools.mcp_dashboard_oauth import dashboard_oauth_flow
    from tools.mcp_oauth import OAuthNonInteractiveError, _make_redirect_handler, suppress_interactive_oauth

    flow = _flow()
    handler = _make_redirect_handler(0)

    async def _go():
        with suppress_interactive_oauth(), dashboard_oauth_flow(flow):
            await handler("https://auth.invalid/authorize?state=abc")

    with pytest.raises(OAuthNonInteractiveError):
        asyncio.run(_go())
    assert flow.authorization_url is None  # never published to the dashboard


def test_callback_waiter_suppression_beats_dashboard_flow_and_fails_fast():
    from tools.mcp_dashboard_oauth import dashboard_oauth_flow
    from tools.mcp_oauth import (OAuthNonInteractiveError, _make_callback_waiter, force_interactive_oauth,
                                 suppress_interactive_oauth)

    flow = _flow()
    waiter = _make_callback_waiter(0, timeout=300.0)

    async def _go():
        with force_interactive_oauth(), dashboard_oauth_flow(flow), suppress_interactive_oauth():
            await waiter()

    start = time.monotonic()
    with pytest.raises(OAuthNonInteractiveError):
        asyncio.run(_go())
    assert time.monotonic() - start < 5.0  # never the 300 s wait


def test_dashboard_flow_still_used_when_not_suppressed():
    """Foreground dashboard login is unchanged."""
    from tools.mcp_dashboard_oauth import dashboard_oauth_flow
    from tools.mcp_oauth import _make_redirect_handler

    flow = _flow()
    handler = _make_redirect_handler(0)

    async def _go():
        with dashboard_oauth_flow(flow):
            await handler("https://auth.invalid/authorize?state=abc")

    asyncio.run(_go())
    assert flow.authorization_url == "https://auth.invalid/authorize?state=abc"


# --- background pin ---------------------------------------------------------------------------

def test_enter_background_oauth_mode_is_task_scoped():
    from tools.mcp_dashboard_oauth import dashboard_oauth_flow, get_dashboard_oauth_flow
    from tools.mcp_oauth import _is_interactive, enter_background_oauth_mode, force_interactive_oauth

    seen = {}

    async def _task_body():
        seen["before"] = (_is_interactive(), get_dashboard_oauth_flow() is not None)
        enter_background_oauth_mode()
        seen["after"] = (_is_interactive(), get_dashboard_oauth_flow() is not None)

    async def _go():
        with force_interactive_oauth(), dashboard_oauth_flow(_flow()):
            await asyncio.ensure_future(_task_body())  # task gets a context COPY
            seen["caller"] = (_is_interactive(), get_dashboard_oauth_flow() is not None)

    asyncio.run(_go())
    assert seen["before"] == (True, True)
    assert seen["after"] == (False, False)
    assert seen["caller"] == (True, True)  # the login context itself is untouched


def test_server_task_reconnect_runs_non_interactive_even_if_created_in_login_context():
    from tools.mcp_dashboard_oauth import dashboard_oauth_flow, get_dashboard_oauth_flow
    from tools.mcp_oauth import _is_interactive, force_interactive_oauth
    from tools.mcp_tool import MCPServerTask

    observed = []

    async def patched_run_stdio(self_srv, config):
        observed.append((_is_interactive(), get_dashboard_oauth_flow() is not None))
        if len(observed) == 1:
            self_srv.session = MagicMock()
            self_srv._tools = []
            self_srv._ready.set()
            raise ConnectionError("keepalive failed")  # -> reconnect
        self_srv._shutdown_event.set()
        await self_srv._shutdown_event.wait()

    async def _go():
        server = MCPServerTask("srv")
        with patch.object(MCPServerTask, "_run_stdio", patched_run_stdio), \
                patch("asyncio.sleep", new_callable=AsyncMock):
            with force_interactive_oauth(), dashboard_oauth_flow(_flow()):
                task = asyncio.ensure_future(server.run({"command": "test"}))
            await task

    asyncio.run(_go())
    assert len(observed) >= 2
    assert observed[0] == (True, True)  # first connect keeps the creator's (login) context
    assert all(o == (False, False) for o in observed[1:])  # every reconnect is background
