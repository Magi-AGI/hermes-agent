"""Adopted from the Codex round-7 closure review of b959ce33b5 (codex-closure-r7/zero_cache).
R8 design change (Lake): a successful zero-tool publication WRITES an empty entry (``tools: []``,
fingerprint, that response's hints) instead of deleting it, so ``is None`` assertions became
"the entry lists no tools"; the I/O-failure probe asserts the stated limit (a WARNING)."""

import logging

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types as types

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
        "name": name, "description": description or name,
        "inputSchema": {"type": "object"},
    })


def page(tools, *, ttl=None):
    raw = {"tools": [t.model_dump(by_alias=True) for t in tools]}
    if ttl is not None:
        raw["ttlMs"] = ttl
    return types.ListToolsResult.model_validate(raw)


def seeded_server(name, config):
    server = MCPServerTask(name)
    server._config = config
    server._tools = [tool("alpha")]
    server._registered_tool_names = registration._register_server_tools(name, server, config)
    assert server._registered_tool_names == [f"mcp__{name}__alpha"]
    return server


def test_reconnect_without_tools_capability_removes_old_manifest(tmp_path):
    async def scenario():
        name = "r7_no_tools_capability"
        config = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = seeded_server(name, config)
        fingerprint = cache.config_fingerprint(config)
        assert cache.get_cached_entry(name, fingerprint) is not None

        async def forbidden_list(**_kwargs):
            raise AssertionError("tools/list must not be called without capability")

        server.session = SimpleNamespace(list_tools=forbidden_list)
        server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(tools=None))
        await server._discover_tools()
        assert server._registered_tool_names == [], server._registered_tool_names
        assert cache.get_cached_entry(name, fingerprint)["tools"] == []  # R8: empty entry

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())


def test_shared_adopter_cache_cleared_after_owner_zero_list(tmp_path):
    async def scenario():
        name = "r7_shared_empty"
        config = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        owner_home = tmp_path / "owner"
        adopter_home = tmp_path / "adopter"
        with profile_at(owner_home):
            owner_scope = ToolRegistry.current_scope_key()
            owner_key = (owner_scope, name)
            server = seeded_server(name, config)
            core._servers[owner_key] = server
            core._server_scope_keys[owner_key] = owner_scope

        async def empty_list(**_kwargs):
            return page([], ttl=60_000)

        server.session = SimpleNamespace(list_tools=empty_list)
        with profile_at(adopter_home):
            adopter_scope = ToolRegistry.current_scope_key()
            assert registration.register_connected_into_current_scope({name: config}) == 1
            assert adopter_scope in core._server_tool_scopes[owner_key]
            cache.write_cache_entry(name, cache.config_fingerprint(config), tools=[{
                "name": "alpha", "description": "alpha", "inputSchema": {"type": "object"},
            }])
            assert cache.get_cached_entry(name, cache.config_fingerprint(config)) is not None
        with profile_at(owner_home):
            await server._refresh_tools()
            assert cache.get_cached_entry(name, cache.config_fingerprint(config))["tools"] == []
        with profile_at(adopter_home):
            assert cache.get_cached_entry(name, cache.config_fingerprint(config))["tools"] == []

    with patch("agent.secret_scope.serves_routed_profile", return_value=True), patch(
        "tools.mcp_tool._servers", {}
    ), patch("tools.mcp_tool._server_tool_scopes", {}), patch(
        "tools.mcp_tool._server_scope_keys", {}
    ), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())


def test_clear_failure_does_not_leave_reusable_manifest(tmp_path, caplog):
    """R8: invalidation is a WRITE of the empty entry. If that write fails the previous manifest
    can survive (stated limit, P3); the failure must be visible at WARNING, not debug."""
    async def scenario():
        name = "r7_failed_clear"
        config = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = seeded_server(name, config)

        async def empty_list(**_kwargs):
            return page([])

        server.session = SimpleNamespace(list_tools=empty_list)
        with patch.object(cache, "write_cache_entry", side_effect=OSError("cannot replace cache")):
            await server._refresh_tools()
        assert server._registered_tool_names == []
        warned = [r for r in caplog.records if r.levelno >= logging.WARNING
                  and "schema cache write failed" in r.getMessage() and name in r.getMessage()]
        assert warned, [r.getMessage() for r in caplog.records]

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())


def test_schema_change_with_same_name_is_warned(tmp_path, caplog):
    async def scenario():
        name = "r7_schema_change"
        config = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = seeded_server(name, config)

        async def changed_list(**_kwargs):
            return page([tool("alpha", "meaning changed")])

        server.session = SimpleNamespace(list_tools=changed_list)
        await server._refresh_tools()
        assert cache._load_all()[name]["tools"][0]["description"] == "meaning changed"
        assert any("tools changed dynamically" in r.message for r in caplog.records)

    with profile_at(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())
