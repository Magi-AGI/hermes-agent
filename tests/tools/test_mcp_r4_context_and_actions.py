"""Round-4 probes: a login's forced OAuth context must not become a long-lived server task's
context (AG-3), and no connector action other than authorize starts OAuth under
``mcp.interactive_oauth: false`` (AG-6)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _write_config(text: str) -> None:
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _flow():
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    return DashboardOAuthFlow(flow_id="f", server_name="srv", profile=None, hermes_home=".",
                              redirect_uri="https://example.invalid/cb")


# --- AG-3: long-lived registration never inherits a login's forced context -----------------

def test_long_lived_server_registered_inside_a_login_context_is_not_forced():
    """A server registered for the session (``_discover_and_register_server``: lazy first use,
    discovery) while the calling context is a forced login must not carry ``forced`` into its
    run task: that task's first transport serves for hours, and a 401 on it would otherwise run
    a browser flow the key forbids. The login's own probe (``_probe_single_server``) is not this
    path and keeps the forced context."""
    from tools import mcp_tool_discovery as disc
    from tools.mcp_dashboard_oauth import dashboard_oauth_flow, get_dashboard_oauth_flow
    from tools.mcp_oauth import _oauth_interactive_forced, force_interactive_oauth
    from tools.mcp_tool import MCPServerTask

    observed = []

    async def patched_run_stdio(self_srv, config):
        observed.append((_oauth_interactive_forced.get(), get_dashboard_oauth_flow() is not None))
        self_srv.session = MagicMock()
        self_srv._tools = []
        self_srv._ready.set()
        await self_srv._shutdown_event.wait()

    async def _go():
        with force_interactive_oauth(), dashboard_oauth_flow(_flow()):
            with patch.object(MCPServerTask, "_run_stdio", patched_run_stdio), \
                    patch("tools.mcp_tool_registration._register_server_tools", return_value=[]), \
                    patch.object(disc, "_adopt_server"):
                await disc._discover_and_register_server("pytest_r4_forced", {"command": "test"})
            # the caller (the login) keeps its own context
            caller = (_oauth_interactive_forced.get(), get_dashboard_oauth_flow() is not None)
        return caller

    caller = asyncio.run(asyncio.wait_for(_go(), timeout=10))
    assert observed, "the run task never started"
    assert observed[0] == (False, False), f"long-lived task inherited the login context: {observed[0]}"
    assert caller == (True, True)


def test_login_probe_still_runs_forced():
    """Control: the explicit login's own probe connect keeps forced (the login must work)."""
    from tools.mcp_oauth import _oauth_interactive_forced, force_interactive_oauth
    from tools.mcp_tool import MCPServerTask

    observed = []

    async def patched_run_stdio(self_srv, config):
        observed.append(_oauth_interactive_forced.get())
        self_srv._shutdown_event.set()
        await self_srv._shutdown_event.wait()

    async def _go():
        server = MCPServerTask("srv")
        with patch.object(MCPServerTask, "_run_stdio", patched_run_stdio), \
                patch("asyncio.sleep", new_callable=AsyncMock):
            with force_interactive_oauth():
                task = asyncio.ensure_future(server.run({"command": "test"}))
            await task

    asyncio.run(_go())
    assert observed[0] is True


# --- AG-6: enable / install never start OAuth under key false ------------------------------

class _RecordingBackend:
    """enable / non-OAuth install record whether an OAuth browser flow could run where they do."""

    def __init__(self):
        self.seen = []

    def _record(self, what):
        from tools.mcp_oauth import _is_interactive, _oauth_interactive_forced

        self.seen.append((what, _is_interactive(), _oauth_interactive_forced.get()))

    def required_env(self, name):
        return []

    def installs_with_oauth(self, name):
        return False

    def install(self, name, env):
        self._record("install")
        return ["t"]

    def enable(self, name):
        self._record("enable")


@pytest.mark.parametrize("action,target,card", [
    ("enable", "paper", True), ("enable", "paper", False),
    ("install", "figma", True), ("install", "figma", False),
])
def test_key_off_enable_and_install_run_with_no_interactive_oauth(monkeypatch, action, target, card):
    import tools.mcp_oauth as mod
    from tools.connectors import live
    from tools.connectors import mcp as connectors_mcp
    from tools.connectors.mcp import apply_answer
    from tools.connectors.tool import manage_connections

    _write_config("mcp:\n  interactive_oauth: false\n")
    monkeypatch.setattr(mod, "_stdin_is_console", lambda: True)  # worst case: a real console
    monkeypatch.setattr(connectors_mcp, "_catalog_names", lambda: ["figma"])
    monkeypatch.setattr(connectors_mcp, "_configured_names", lambda: ["paper"])
    monkeypatch.setattr(connectors_mcp, "_catalog_instructions", lambda name: "")
    backend = _RecordingBackend()

    def register(runner, tgt, name):
        backend._record("register")
        return [], ""

    monkeypatch.setattr(connectors_mcp, "_register_connected", register)
    live.reset_for_tests()
    session = f"r4-{action}-{card}"

    def card_cb(payload):
        def click():
            op = live.get(session, payload["op_id"])
            apply_answer(op, json.dumps({"targets": [{"name": target, "status": "approved"}]}))
        import threading
        threading.Timer(0.05, click).start()

    with patch("tools.connectors.run.WATCH_INTERVAL_SECONDS", 0.05):
        manage_connections({"action": action, "connectors": [{"name": target, "mcp": True}]},
                           connection_callback=card_cb if card else None, session_id=session,
                           mcp_backend=backend)
    live.reset_for_tests()
    assert backend.seen, "the action never ran"
    assert all(interactive is False and forced is False for _, interactive, forced in backend.seen), backend.seen
