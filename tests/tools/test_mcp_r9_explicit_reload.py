"""R9: an explicit, user-requested ``/reload-mcp`` bypasses the lazy schema cache and live-connects;
every automatic reload keeps the lazy cache.

Why: the live ``unity-*`` servers are ``enabled: true, lazy: true`` and cache ``tools: []`` while the
Unity Editor is closed (R8 writes an empty entry so no relay is spawned at startup). Without an
explicit bypass their tools could never come back after the Editor opens.

Contract:
- ``discover_mcp_tools(bypass_lazy_cache=True)`` connects a lazy server whose cached entry is empty
  and publishes the real list; without the flag nothing connects;
- the flag is passed ONLY by user-command entry points (CLI ``/reload-mcp``, gateway ``/reload-mcp``,
  the TUI ``reload.mcp`` RPC with ``explicit: true``), never by the config watcher or config sync;
- the live connect is not a login: with ``mcp.interactive_oauth: false`` no browser opens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mcp.types as types
import pytest

import tools.mcp_tool as mcp
from tools import mcp_schema_cache as cache
from tools import mcp_tool_discovery as disc
from tools import mcp_tool_registration as registration
from tools.mcp_tool import MCPServerTask
from tools.registry import ToolRegistry

_NAME = "unity_bridge"
_CFG = {"command": "unity-relay", "args": ["--stdio"], "lazy": True, "connect_timeout": 5}
_STATE = ("_servers", "_server_connecting", "_server_connect_failures", "_server_connect_errors",
          "_server_connect_retry_after", "_server_scope_keys",
          "_lazy_server_configs", "_lazy_server_fingerprints", "_lazy_server_tool_names")


@pytest.fixture(autouse=True)
def _isolated_mcp_state():
    saved = {name: getattr(mcp, name).copy() for name in _STATE if hasattr(mcp, name)}
    for name in _STATE:
        if hasattr(mcp, name):
            getattr(mcp, name).clear()
    yield
    for name, value in saved.items():
        getattr(mcp, name).clear()
        getattr(mcp, name).update(value)


def _write_config(text: str) -> None:
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _tool(name):
    return types.Tool.model_validate({"name": name, "description": name, "inputSchema": {"type": "object"}})


def _seed_empty_entry():
    server = MCPServerTask(_NAME)
    server._config = dict(_CFG)
    server._tools = []
    server._list_cache_meta = {}
    registration._register_server_tools(_NAME, server, _CFG)
    entry = cache.get_cached_entry(_NAME, cache.config_fingerprint(_CFG))
    assert entry is not None and entry["tools"] == []


def _discover(connect, **kwargs):
    def _run_inline(factory, timeout=30):
        return asyncio.run(factory() if callable(factory) else factory)

    with (
        patch("tools.mcp_tool_config._load_mcp_config", return_value={_NAME: dict(_CFG)}),
        patch.object(mcp, "_ensure_mcp_sdk", return_value=True),
        patch("tools.mcp_tool_loop._ensure_mcp_loop"),
        patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=_run_inline),
        patch("tools.mcp_tool_discovery._discover_and_register_server", side_effect=connect),
    ):
        return disc.discover_mcp_tools(**kwargs)


def test_explicit_reload_connects_a_lazy_server_with_an_empty_entry_and_publishes():
    with patch("tools.registry.registry", ToolRegistry()) as reg, \
            patch.object(registration, "_select_utility_schemas", return_value=[]):
        _seed_empty_entry()
        connects = []

        async def editor_open_connect(name, cfg):
            connects.append(name)
            server = MCPServerTask(name)
            server._config = dict(cfg)
            server._tools = [_tool("manage_scene")]
            server._list_cache_meta = {}
            server._registered_tool_names = registration._register_server_tools(name, server, cfg)
            return server._registered_tool_names

        _discover(editor_open_connect, bypass_lazy_cache=True)
        assert connects == [_NAME]
        assert reg.get_tool_names_for_toolset(f"mcp-{_NAME}") == [f"mcp__{_NAME}__manage_scene"]
        entry = cache.get_cached_entry(_NAME, cache.config_fingerprint(_CFG))
        assert [t["name"] for t in entry["tools"]] == ["manage_scene"]


def test_explicit_reload_also_promotes_a_server_registered_lazily_with_tools():
    """A lazily registered server (tools from cache, never connected) is live-connected too, and a
    cached tool the live server no longer serves does not survive."""
    with patch("tools.registry.registry", ToolRegistry()) as reg, \
            patch.object(registration, "_select_utility_schemas", return_value=[]):
        registration._register_from_cache_sync(_NAME, _CFG, {"tools": [
            {"name": "old_tool", "description": "old", "inputSchema": {"type": "object"}}], "utility_tools": []})
        assert reg.get_tool_names_for_toolset(f"mcp-{_NAME}") == [f"mcp__{_NAME}__old_tool"]

        async def connect(name, cfg):
            server = MCPServerTask(name)
            server._config = dict(cfg)
            server._tools = [_tool("new_tool")]
            server._list_cache_meta = {}
            return registration._register_server_tools(name, server, cfg)

        _discover(connect, bypass_lazy_cache=True)
        assert reg.get_tool_names_for_toolset(f"mcp-{_NAME}") == [f"mcp__{_NAME}__new_tool"]


def test_automatic_reload_keeps_the_lazy_cache_and_never_connects():
    with patch("tools.registry.registry", ToolRegistry()) as reg, \
            patch.object(registration, "_select_utility_schemas", return_value=[]):
        _seed_empty_entry()
        connects = []

        async def must_not_connect(name, cfg):
            connects.append(name)
            raise AssertionError("an automatic reload must not live-connect a lazy server")

        _discover(must_not_connect)
        assert connects == []
        assert reg.get_tool_names_for_toolset(f"mcp-{_NAME}") == []


def test_explicit_reload_with_oauth_and_interactive_oauth_false_opens_no_browser(monkeypatch):
    import tools.mcp_oauth as oauth

    _write_config("mcp:\n  interactive_oauth: false\n")
    monkeypatch.setattr(oauth, "_stdin_is_console", lambda: True)  # worst case: a real console
    monkeypatch.setattr(oauth, "_can_open_browser", lambda: True)
    browser = MagicMock(return_value=True)
    monkeypatch.setattr("webbrowser.open", browser)
    outcomes = []

    async def oauth_required_connect(name, cfg):
        # What the SDK does on a 401 with no usable token: ask the redirect handler to authorize.
        try:
            await oauth._make_redirect_handler(0)("https://idp.example.invalid/authorize?state=r9")
        except oauth.OAuthNonInteractiveError as exc:
            outcomes.append("refused")
            raise
        outcomes.append("authorized")
        return []

    with patch("tools.registry.registry", ToolRegistry()), \
            patch.object(registration, "_select_utility_schemas", return_value=[]):
        _seed_empty_entry()
        _discover(oauth_required_connect, bypass_lazy_cache=True)
    assert outcomes == ["refused"]
    browser.assert_not_called()


# --- the flag comes only from user-command entry points -----------------------------------------

def _cli_stub():
    return SimpleNamespace(_command_running=True, agent=None, enabled_toolsets=None)


@pytest.mark.parametrize("explicit", [True, False])
def test_cli_reload_passes_the_flag_only_when_explicit(explicit):
    from hermes_cli.cli_info_mixin import CLIInfoMixin

    seen = []
    with patch("tools.mcp_tool_lifecycle.shutdown_mcp_servers"), \
            patch("tools.mcp_tool_agent.reprobe_tool_availability"), \
            patch("tools.mcp_tool_discovery.discover_mcp_tools",
                  side_effect=lambda *a, **kw: seen.append(kw) or []):
        if explicit:
            CLIInfoMixin._reload_mcp(_cli_stub(), explicit=True)
        else:
            CLIInfoMixin._reload_mcp(_cli_stub())  # the config watcher's call
    assert seen == [{"bypass_lazy_cache": True} if explicit else {}]


def test_cli_confirmed_slash_command_reloads_explicitly(monkeypatch):
    """Drives the real /reload-mcp confirm handler (R10: behavioural, not a source grep)."""
    from contextlib import nullcontext

    from hermes_cli import cli_info_mixin

    calls = []
    stub = SimpleNamespace(
        _busy_command=lambda _status: nullcontext(), _slow_command_status=lambda _cmd: "reloading",
        _reload_mcp=lambda **kw: calls.append(kw))
    monkeypatch.setattr(cli_info_mixin, "_gated_confirm", lambda *a, **kw: "once")
    cli_info_mixin.CLIInfoMixin._confirm_and_reload_mcp(stub, "/reload-mcp")
    assert calls == [{"explicit": True}]

    calls.clear()
    monkeypatch.setattr(cli_info_mixin, "_gated_confirm", lambda *a, **kw: None)  # Cancel
    cli_info_mixin.CLIInfoMixin._confirm_and_reload_mcp(stub, "/reload-mcp")
    assert calls == []


def test_cli_config_watcher_reloads_without_the_explicit_flag(monkeypatch):
    """Drives the real config watcher on an mcp_servers edit: its automatic reload keeps the lazy
    cache (called with no explicit flag)."""
    from hermes_cli import cli_info_mixin
    from hermes_cli.config import get_config_path

    path = Path(get_config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("mcp_servers:\n  unity_bridge:\n    command: unity-relay\n", encoding="utf-8")
    calls = []

    class _InlineThread:
        def __init__(self, target, daemon=None, **_kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(cli_info_mixin.threading, "Thread", _InlineThread)
    stub = SimpleNamespace(_last_config_check=float("-inf"), _config_sig=None, _config_mcp_servers={},
                           _reload_mcp=lambda *a, **kw: calls.append((a, kw)))
    cli_info_mixin.CLIInfoMixin._check_config_mcp_changes(stub)
    assert calls == [((), {})]


@pytest.mark.parametrize("explicit", [True, False])
def test_tui_reload_rpc_passes_the_flag_from_its_params(explicit):
    import tui_gateway.server as srv

    seen = []
    with patch("tools.mcp_tool_lifecycle.shutdown_mcp_servers"), \
            patch("tools.mcp_tool_agent.reprobe_tool_availability"), \
            patch("tools.mcp_tool_discovery.discover_mcp_tools",
                  side_effect=lambda *a, **kw: seen.append(kw) or []):
        params = {"confirm": True, **({"explicit": True} if explicit else {"rev": "rev-1"})}
        out = srv._methods["reload.mcp"](1, params)
    assert "error" not in out, out
    assert seen and all(kw == ({"bypass_lazy_cache": True} if explicit else {}) for kw in seen), seen


def test_compute_host_forwards_the_explicit_flag():
    from tui_gateway.host_supervisor import HostSupervisor

    sent = []
    stub = SimpleNamespace(control=lambda sid, **kw: sent.append(kw) or {})
    HostSupervisor.reload_mcp(stub, "s1", request_id="r1", explicit=True)
    HostSupervisor.reload_mcp(stub, "s1", request_id="r2")
    assert [kw["payload"]["explicit"] for kw in sent] == [True, False]


def test_gateway_reload_is_explicit(monkeypatch):
    """Drives the real gateway /reload-mcp execution (R10: behavioural, not a source grep)."""
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.config import Platform
    from tools import mcp_tool_discovery as _mcp_discovery
    from tools import mcp_tool_lifecycle as _mcp_lifecycle

    seen = []
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._async_session_store = SimpleNamespace(
        get_or_create_session=MagicMock(side_effect=RuntimeError("skip transcript")))
    monkeypatch.setattr(_mcp_lifecycle, "shutdown_mcp_servers", lambda **_kw: None)
    monkeypatch.setattr(_mcp_discovery, "discover_mcp_tools", lambda *a, **kw: seen.append(kw) or [])
    event = MessageEvent(text="/reload-mcp", message_id="m1", source=SessionSource(
        platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", chat_type="dm"))
    asyncio.run(runner._execute_mcp_reload(event))
    assert seen == [{"bypass_lazy_cache": True}]
