"""Adopted from the Codex round-8 closure review of 273b2d4272 (codex-closure-r8): offline probes for round-8 MCP publication."""

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
def profile(path):
    path.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(path)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def tool(name):
    return types.Tool.model_validate({
        "name": name, "description": name,
        "inputSchema": {"type": "object"},
    })


def test_adopter_live_registry_matches_owner_new_list(tmp_path):
    async def scenario(registry):
        name = "r8_adopter_changed"
        config = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        owner_home = tmp_path / "owner"
        adopter_home = tmp_path / "adopter"
        with profile(owner_home):
            owner_scope = ToolRegistry.current_scope_key()
            owner_key = (owner_scope, name)
            server = MCPServerTask(name)
            server._config = config
            server._tools = [tool("alpha")]
            server._registered_tool_names = registration._register_server_tools(name, server, config)
            core._servers[owner_key] = server
            core._server_scope_keys[owner_key] = owner_scope

        async def beta_list(**_kwargs):
            return types.ListToolsResult.model_validate({
                "tools": [tool("beta").model_dump(by_alias=True)], "ttlMs": 90000,
            })

        server.session = SimpleNamespace(list_tools=beta_list)
        with profile(adopter_home):
            adopter_scope = ToolRegistry.current_scope_key()
            assert registration.register_connected_into_current_scope({name: config}) == 1
            assert registry.get_toolset_for_tool(f"mcp__{name}__alpha") == f"mcp-{name}"
            cache.write_cache_entry(name, cache.config_fingerprint(config), tools=[{
                "name": "alpha", "description": "alpha", "inputSchema": {"type": "object"},
            }])

        with profile(owner_home):
            await server._refresh_tools()
            assert registry.get_toolset_for_tool(f"mcp__{name}__beta") == f"mcp-{name}"
        with profile(adopter_home):
            adopter_cache = cache.get_cached_entry(name, cache.config_fingerprint(config))
            observed = (
                registry.get_toolset_for_tool(f"mcp__{name}__alpha"),
                registry.get_toolset_for_tool(f"mcp__{name}__beta"),
                [row["name"] for row in adopter_cache["tools"]],
                adopter_scope in core._server_tool_scopes[owner_key],
            )
            assert observed == (None, f"mcp-{name}", ["beta"], True), observed

    with patch("agent.secret_scope.serves_routed_profile", return_value=True), patch(
        "tools.mcp_tool._servers", {}
    ), patch("tools.mcp_tool._server_tool_scopes", {}), patch(
        "tools.mcp_tool._server_scope_keys", {}
    ), patch("tools.registry.registry", ToolRegistry()) as reg, patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario(reg))


def test_capability_drop_after_empty_list_removes_prior_hints(tmp_path):
    async def scenario():
        name = "r8_empty_then_no_capability"
        config = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        server = MCPServerTask(name)
        server._config = config
        server._tools = []
        server._list_cache_meta = {"ttl_ms": 12345, "cache_scope": "private"}
        server._registered_tool_names = registration._register_server_tools(name, server, config)
        assert server._registered_tool_names == []
        assert cache.get_cached_entry(name, cache.config_fingerprint(config))["ttl_ms"] == 12345

        async def forbidden_list(**_kwargs):
            raise AssertionError("no tools capability must not call tools/list")

        server.session = SimpleNamespace(list_tools=forbidden_list)
        server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(tools=None))
        server._ready.set()
        await server._discover_tools()
        entry = cache.get_cached_entry(name, cache.config_fingerprint(config))
        assert server._list_cache_meta == {}, server._list_cache_meta
        assert "ttl_ms" not in entry and "cache_scope" not in entry, entry

    with profile(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())


def test_owner_cache_write_does_not_enable_adopter_disabled_resource_utilities(tmp_path):
    async def scenario(registry):
        name = "r8_adopter_resource_policy"
        owner_config = {"url": "https://mcp.example.invalid/mcp", "lazy": True,
                        "tools": {"resources": True}}
        adopter_config = {"url": "https://mcp.example.invalid/mcp", "lazy": True,
                          "tools": {"resources": False}}
        assert cache.config_fingerprint(owner_config) == cache.config_fingerprint(adopter_config)
        owner_home = tmp_path / "owner"
        adopter_home = tmp_path / "adopter"
        with profile(owner_home):
            owner_scope = ToolRegistry.current_scope_key()
            owner_key = (owner_scope, name)
            server = MCPServerTask(name)
            server._config = owner_config
            server._tools = [tool("alpha")]
            server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(
                tools=object(), resources=object(), prompts=None))
            server.session = SimpleNamespace(list_resources=lambda: None, read_resource=lambda: None)
            server._registered_tool_names = registration._register_server_tools(name, server, owner_config)
            core._servers[owner_key] = server
            core._server_scope_keys[owner_key] = owner_scope
            assert f"mcp__{name}__list_resources" in server._registered_tool_names

        async def alpha_list(**_kwargs):
            return types.ListToolsResult.model_validate({
                "tools": [tool("alpha").model_dump(by_alias=True)],
            })

        server.session = SimpleNamespace(list_tools=alpha_list)
        with profile(adopter_home):
            assert registration.register_connected_into_current_scope({name: adopter_config}) == 1
            assert registry.get_toolset_for_tool(f"mcp__{name}__list_resources") is None
            cache.write_cache_entry(name, cache.config_fingerprint(adopter_config), tools=[{
                "name": "alpha", "description": "alpha", "inputSchema": {"type": "object"},
            }], utility_tools=[])

        with profile(owner_home):
            await server._refresh_tools()
        with profile(adopter_home):
            entry = cache.get_cached_entry(name, cache.config_fingerprint(adopter_config))
            leaked_rows = [row["handler_key"] for row in entry["utility_tools"]]
            with patch("tools.registry.registry", ToolRegistry()) as fresh_registry:
                lazy_names = registration._register_from_cache_sync(name, adopter_config, entry)
                leaked_live = fresh_registry.get_toolset_for_tool(f"mcp__{name}__list_resources")
            assert (leaked_rows, "list_resources" in [n.rsplit("__", 1)[-1] for n in lazy_names], leaked_live) == ([], False, None)

    with patch("agent.secret_scope.serves_routed_profile", return_value=True), patch(
        "tools.mcp_tool._servers", {}
    ), patch("tools.mcp_tool._server_tool_scopes", {}), patch(
        "tools.mcp_tool._server_scope_keys", {}
    ), patch("tools.registry.registry", ToolRegistry()) as reg:
        asyncio.run(scenario(reg))


def test_adopter_same_name_schema_is_refreshed_in_live_registry(tmp_path):
    async def scenario(registry):
        name = "r8_adopter_schema"
        config = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
        owner_home, adopter_home = tmp_path / "owner", tmp_path / "adopter"
        with profile(owner_home):
            owner_scope = ToolRegistry.current_scope_key()
            owner_key = (owner_scope, name)
            server = MCPServerTask(name)
            server._config = config
            server._tools = [tool("alpha")]
            server._registered_tool_names = registration._register_server_tools(name, server, config)
            core._servers[owner_key] = server
            core._server_scope_keys[owner_key] = owner_scope

        async def changed_list(**_kwargs):
            return types.ListToolsResult.model_validate({"tools": [{
                "name": "alpha", "description": "new meaning",
                "inputSchema": {"type": "object", "properties": {"new_arg": {"type": "string"}}},
            }]})

        server.session = SimpleNamespace(list_tools=changed_list)
        with profile(adopter_home):
            adopter_scope = ToolRegistry.current_scope_key()
            assert registration.register_connected_into_current_scope({name: config}) == 1
            before = registry.snapshot_registration(f"mcp__{name}__alpha", scope=adopter_scope)
            assert before is not None and before.schema["description"] == "alpha"
            cache.write_cache_entry(name, cache.config_fingerprint(config), tools=[{
                "name": "alpha", "description": "alpha", "inputSchema": {"type": "object"},
            }])

        with profile(owner_home):
            await server._refresh_tools()
            now_owner = registry.snapshot_registration(f"mcp__{name}__alpha", scope=owner_scope)
            assert now_owner.schema["description"] == "new meaning"
        with profile(adopter_home):
            now_adopter = registry.snapshot_registration(f"mcp__{name}__alpha", scope=adopter_scope)
            cached = cache.get_cached_entry(name, cache.config_fingerprint(config))
            assert (now_adopter.schema["description"], cached["tools"][0]["description"]) == (
                "new meaning", "new meaning")

    with patch("agent.secret_scope.serves_routed_profile", return_value=True), patch(
        "tools.mcp_tool._servers", {}
    ), patch("tools.mcp_tool._server_tool_scopes", {}), patch(
        "tools.mcp_tool._server_scope_keys", {}
    ), patch("tools.registry.registry", ToolRegistry()) as reg, patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario(reg))
