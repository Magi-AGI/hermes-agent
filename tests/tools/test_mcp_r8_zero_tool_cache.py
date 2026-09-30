"""R8: a server that legitimately lists ZERO tools (the Unity MCP bridge while the Unity Editor is
closed) is cached as an EMPTY entry, not deleted.

Why (Lake's install): 3 of 8 cached MCP servers are ``unity-*``. Each live connect spawns a
``relay_win.exe`` (~1.4 GiB commit), and relay accumulation has driven the machine to 98% commit.
Deleting the entry would make every startup live-connect them; an empty entry keeps the lazy
startup free while still preventing removed tools from being resurrected.

Contract:
1. a successful zero-tool list writes ``tools: []`` with the fingerprint (and the response's own
   hints, none here, so TTL-less);
2. the next lazy startup registers nothing and makes NO connection;
3. a later real connect, or a ``tools/list_changed`` refresh, publishes the real list and rewrites
   the entry.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types as types
import pytest

import tools.mcp_tool as mcp
from tools import mcp_schema_cache as cache
from tools import mcp_tool_registration as registration
from tools.mcp_tool import MCPServerTask
from tools.registry import ToolRegistry

_NAME = "unity_editor_bridge"
_CFG = {"command": "unity-relay", "args": ["--stdio"], "lazy": True}
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


def _tool(name):
    return types.Tool.model_validate({"name": name, "description": name, "inputSchema": {"type": "object"}})


def _server(tools):
    server = MCPServerTask(_NAME)
    server._config = dict(_CFG)
    server._tools = list(tools)
    server._list_cache_meta = {}
    return server


def _lazy_startup(connect):
    """The real suppressed startup discovery -> register_mcp_servers lazy pass, with the network
    connect replaced by *connect* (it must not be reached for a cache hit)."""
    from hermes_cli import mcp_startup

    def _run_inline(factory, timeout=30):
        return asyncio.run(factory() if callable(factory) else factory)

    with (
        patch("tools.mcp_tool_config._load_mcp_config", return_value={_NAME: dict(_CFG)}),
        patch.object(mcp, "_ensure_mcp_sdk", return_value=True),
        patch("tools.mcp_tool_loop._ensure_mcp_loop"),
        patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=_run_inline),
        patch("tools.mcp_tool_discovery._discover_and_register_server", side_effect=connect),
    ):
        mcp_startup._discover_mcp_tools_without_interactive_oauth()


def test_zero_tool_server_is_cached_empty_and_lazy_startup_never_connects():
    with patch("tools.registry.registry", ToolRegistry()), \
            patch.object(registration, "_select_utility_schemas", return_value=[]):
        # A previously cached tool (the Editor was open last time).
        registration._register_server_tools(_NAME, _server([_tool("manage_scene")]), _CFG)
        fingerprint = cache.config_fingerprint(_CFG)
        assert [t["name"] for t in cache.get_cached_entry(_NAME, fingerprint)["tools"]] == ["manage_scene"]

        # Editor closed: a successful live list with zero tools.
        assert registration._register_server_tools(_NAME, _server([]), _CFG) == []
        entry = cache.get_cached_entry(_NAME, fingerprint)
        assert entry is not None, "the entry must be written empty, not deleted"
        assert entry["tools"] == [] and entry["fingerprint"] == fingerprint
        assert "ttl_ms" not in entry and "written_at" not in entry, "TTL-less: no hint in the response"

    # Next startup (a fresh process: new registry, empty connect bookkeeping): a lazy hit that
    # registers nothing and connects nothing.
    for name in _STATE:
        if hasattr(mcp, name):
            getattr(mcp, name).clear()
    connects = []

    async def must_not_connect(name, cfg):
        connects.append(name)
        raise AssertionError("an empty cached manifest must not trigger a live connect")

    with patch("tools.registry.registry", ToolRegistry()) as fresh:
        _lazy_startup(must_not_connect)
        assert connects == []
        assert fresh.get_tool_names_for_toolset(f"mcp-{_NAME}") == []


def test_later_real_connect_or_refresh_publishes_the_real_list():
    with patch("tools.registry.registry", ToolRegistry()) as reg, \
            patch.object(registration, "_select_utility_schemas", return_value=[]):
        fingerprint = cache.config_fingerprint(_CFG)
        server = _server([])
        server._registered_tool_names = registration._register_server_tools(_NAME, server, _CFG)
        assert cache.get_cached_entry(_NAME, fingerprint)["tools"] == []

        # Editor opened: the server announces tools/list_changed; the refresh publishes the list.
        async def listing(**_kwargs):
            return types.ListToolsResult.model_validate(
                {"tools": [_tool("manage_gameobject").model_dump(by_alias=True)], "ttlMs": 300_000})

        server.session = SimpleNamespace(list_tools=listing)
        server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(tools=object()))
        asyncio.run(server._refresh_tools())
        assert server._registered_tool_names == [f"mcp__{_NAME}__manage_gameobject"]
        assert reg.get_tool_names_for_toolset(f"mcp-{_NAME}") == [f"mcp__{_NAME}__manage_gameobject"]
        entry = cache.get_cached_entry(_NAME, fingerprint)
        assert [t["name"] for t in entry["tools"]] == ["manage_gameobject"]
        assert entry["ttl_ms"] == 300_000

        # A fresh real connect (initial registration path) publishes likewise.
        fresh = _server([_tool("manage_asset")])
        assert registration._register_server_tools(_NAME, fresh, _CFG)
        assert "manage_asset" in [t["name"] for t in cache.get_cached_entry(_NAME, fingerprint)["tools"]]
