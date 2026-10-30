"""Adopted from the Codex round-7 closure review of b959ce33b5 (codex-closure-r7/race):
offline closure probes for MCP H-A."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types as types
import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import mcp_schema_cache as cache
from tools import mcp_tool as core
from tools import mcp_tool_registration as registration
from tools.mcp_tool import MCPServerTask
from tools.registry import ToolRegistry


@contextmanager
def profile_at(path):
    path.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(path)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def tool(name, description=None):
    return types.Tool.model_validate({
        "name": name,
        "description": description or name,
        "inputSchema": {"type": "object"},
    })


def page(tools, *, ttl=None):
    raw = {"tools": [item.model_dump(by_alias=True) for item in tools]}
    if ttl is not None:
        raw["ttlMs"] = ttl
    return types.ListToolsResult.model_validate(raw)


def initially_registered(name, cfg):
    server = MCPServerTask(name)
    server._config = cfg
    server._tools = [tool("alpha")]
    server._registered_tool_names = registration._register_server_tools(name, server, cfg)
    assert server._registered_tool_names == [f"mcp__{name}__alpha"]
    return server


def test_refresh_waiting_behind_reconnect_deregisters_reconnect_only_tool(tmp_path):
    """A refresh must compare against the names current when it publishes, not before lock wait."""
    async def scenario(registry):
        name = "r7_waiting"
        cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = initially_registered(name, cfg)
        entered_discovery = asyncio.Event()
        release_discovery = asyncio.Event()
        list_count = 0

        async def successor_list(**_kwargs):
            nonlocal list_count
            list_count += 1
            if list_count == 1:
                entered_discovery.set()
                await release_discovery.wait()
                return page([tool("beta")], ttl=120_000)
            return page([tool("gamma")], ttl=180_000)

        async def handshake(_server, _session, _timeout):
            return SimpleNamespace(capabilities=SimpleNamespace(tools=object()))

        async def lifecycle(_server):
            return "shutdown"

        successor_session = SimpleNamespace(list_tools=successor_list)
        server.session = None  # run() does this at the prior transport teardown
        with patch.object(MCPServerTask, "_negotiate_session", new=handshake), patch.object(
            MCPServerTask, "_wait_for_lifecycle_event", new=lifecycle
        ):
            reconnect = asyncio.create_task(server._serve_session(successor_session, 1))
            await asyncio.wait_for(entered_discovery.wait(), 2)
            assert server.session is successor_session
            assert server._rpc_lock.locked()
            queued_refresh = asyncio.create_task(server._refresh_tools())
            await asyncio.sleep(0)
            assert server._refresh_lock.locked()
            release_discovery.set()
            await asyncio.wait_for(asyncio.gather(reconnect, queued_refresh), 2)
        assert list_count == 2
        assert server._registered_tool_names == [f"mcp__{name}__gamma"]
        assert [t.name for t in server._tools] == ["gamma"]
        entry = cache._load_all()[name]
        assert [t["name"] for t in entry["tools"]] == ["gamma"]
        assert entry.get("ttl_ms") == 180_000
        beta_name = f"mcp__{name}__beta"
        with patch.dict(core._servers, {registration._server_key_for_task(server): server}, clear=True):
            definitions = registry.get_definitions({beta_name})
        observed = (
            registry.get_toolset_for_tool(beta_name),
            [item["function"]["name"] for item in definitions],
        )
        assert observed == (None, []), observed

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()) as reg, patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario(reg))


def test_reconnect_without_tools_capability_clears_old_tools(tmp_path):
    """A newly prompt-only server cannot keep a prior connection's tool manifest."""
    async def scenario(registry):
        name = "r7_no_tools_cap"
        cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = initially_registered(name, cfg)

        async def forbidden_list(**_kwargs):
            raise AssertionError("tools/list must be skipped when capability is absent")

        async def handshake(_server, _session, _timeout):
            return SimpleNamespace(capabilities=SimpleNamespace(tools=None))

        async def lifecycle(_server):
            return "shutdown"

        successor_session = SimpleNamespace(list_tools=forbidden_list)
        server.session = None  # run() does this at the prior transport teardown
        with patch.object(MCPServerTask, "_negotiate_session", new=handshake), patch.object(
            MCPServerTask, "_wait_for_lifecycle_event", new=lifecycle
        ):
            await server._serve_session(successor_session, 1)
        assert server.session is successor_session
        assert server._ready.is_set()
        assert server._tools == []
        entry = cache.get_cached_entry(name, cache.config_fingerprint(cfg))
        alpha_name = f"mcp__{name}__alpha"
        with patch.dict(core._servers, {registration._server_key_for_task(server): server}, clear=True):
            definitions = registry.get_definitions({alpha_name})
        observed = (
            server._registered_tool_names,
            registry.get_toolset_for_tool(alpha_name),
            [item["name"] for item in entry["tools"]] if entry else [],
            [item["function"]["name"] for item in definitions],
        )
        assert observed == ([], None, [], []), observed

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()) as reg, patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario(reg))


def test_reconnect_same_names_updates_cache_without_change_warning(tmp_path, caplog):
    """The new connection's hints/content publish even when the names are unchanged."""
    async def scenario():
        name = "r7_same_names"
        cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = initially_registered(name, cfg)

        async def successor_list(**_kwargs):
            return page([tool("alpha", "new description")], ttl=75_000)

        server.session = SimpleNamespace(list_tools=successor_list)
        await server._discover_tools()
        entry = cache._load_all()[name]
        assert entry["ttl_ms"] == 75_000
        assert entry["tools"][0]["description"] == "new description"
        assert [t.name for t in server._tools] == ["alpha"]

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())
    assert not any("tools changed dynamically" in record.getMessage() for record in caplog.records)


def test_failed_reconnect_discovery_preserves_previous_manifest(tmp_path):
    """An unsuccessful list must not be mistaken for a successful empty list."""
    async def scenario(registry):
        name = "r7_failed_discovery"
        cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = initially_registered(name, cfg)

        async def failing_list(**_kwargs):
            raise RuntimeError("test discovery failure")

        server.session = SimpleNamespace(list_tools=failing_list)
        with pytest.raises(RuntimeError, match="test discovery failure"):
            await server._discover_tools()
        assert server._registered_tool_names == [f"mcp__{name}__alpha"]
        assert registry.get_toolset_for_tool(f"mcp__{name}__alpha") == f"mcp-{name}"
        assert [t["name"] for t in cache._load_all()[name]["tools"]] == ["alpha"]

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()) as reg, patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario(reg))
