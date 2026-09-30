"""Adopted from the Codex round-6 closure review of cb0ed01374 (codex-closure-r6): MCP
schema-cache / tools/list_changed refresh probes."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types as types

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import mcp_schema_cache as cache
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


def tool(name):
    return types.Tool.model_validate({
        "name": name, "description": name, "inputSchema": {"type": "object"},
    })


def page(tools, *, ttl=None, cursor=None):
    raw = {"tools": [t.model_dump(by_alias=True) for t in tools]}
    if ttl is not None:
        raw["ttlMs"] = ttl
    if cursor is not None:
        raw["nextCursor"] = cursor
    return types.ListToolsResult.model_validate(raw)


def server_with_cached_tool(name, cfg, initial_tool):
    server = MCPServerTask(name)
    server._config = cfg
    server._tools = [initial_tool]
    server._registered_tool_names = registration._register_server_tools(name, server, cfg)
    assert server._registered_tool_names
    return server


def test_refresh_first_page_wins_then_missing_hints_clear_previous_hint(tmp_path):
    """Control: page two cannot override page one's TTL; a later hint-free refresh clears it."""
    async def scenario():
        name = "r6_pages"
        cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        first_pages = iter((page([], ttl=120_000, cursor="more"), page([tool("alpha")], ttl=0)))
        async def first_list(**_kwargs):
            return next(first_pages)
        server = server_with_cached_tool(name, cfg, tool("alpha"))
        server.session = SimpleNamespace(list_tools=first_list)
        await server._refresh_tools()
        assert cache._load_all()[name]["ttl_ms"] == 120_000
        assert server._list_cache_meta == {"ttl_ms": 120_000}

        async def second_list(**_kwargs):
            return page([tool("alpha")])
        server.session = SimpleNamespace(list_tools=second_list)
        await server._refresh_tools()
        assert server._list_cache_meta == {}
        assert "ttl_ms" not in cache._load_all()[name]

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())


def test_refresh_to_empty_list_invalidates_cached_manifest(tmp_path):
    """A live zero-tool list must not let the next lazy startup resurrect old tools."""
    async def scenario():
        name = "r6_empty"
        cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = server_with_cached_tool(name, cfg, tool("alpha"))
        fingerprint = cache.config_fingerprint(cfg)
        assert [t["name"] for t in cache.get_cached_entry(name, fingerprint)["tools"]] == ["alpha"]
        async def empty_list(**_kwargs):
            return page([], ttl=60_000)
        server.session = SimpleNamespace(list_tools=empty_list)
        await server._refresh_tools()
        assert server._registered_tool_names == []
        entry = cache.get_cached_entry(name, fingerprint)
        assert entry is None or entry["tools"] == [], entry

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())


def test_old_refresh_cannot_publish_after_reconnect(tmp_path):
    """An old list response must not overwrite the new session's registry/cache."""
    async def scenario():
        name = "r6_reconnect"
        cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = server_with_cached_tool(name, cfg, tool("alpha"))
        entered = asyncio.Event()
        release = asyncio.Event()

        async def old_list(**_kwargs):
            entered.set()
            await release.wait()
            return page([tool("alpha")], ttl=0)

        async def new_list(**_kwargs):
            return page([tool("beta")], ttl=120_000)

        old_session = SimpleNamespace(list_tools=old_list)
        new_session = SimpleNamespace(list_tools=new_list)
        server.session = old_session
        refresh = asyncio.create_task(server._refresh_tools())
        await entered.wait()
        # run() clears session outside _rpc_lock during teardown, then _serve_session
        # installs the new session before _discover_tools waits for that lock.
        server.session = None
        server.session = new_session
        discover = asyncio.create_task(server._discover_tools())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(refresh, discover)
        entry = cache._load_all()[name]
        assert [t.name for t in server._tools] == ["beta"]
        assert [t["name"] for t in entry["tools"]] == ["beta"], entry
        assert entry.get("ttl_ms") == 120_000, entry
        assert server._registered_tool_names == [f"mcp__{name}__beta"]

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())
