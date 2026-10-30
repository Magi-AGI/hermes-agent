"""Adopted from the Codex round-9 closure review of fd5c4bdb98 (codex-closure-r9): adopted-profile publication probes."""

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
        "name": name, "description": name, "inputSchema": {"type": "object"},
    })


def test_adopter_policy_change_reconciles_live_overlay_without_server_refresh(tmp_path):
    name = "r9_policy_change"
    owner_config = {"url": "https://mcp.example.invalid/mcp", "lazy": True,
                    "tools": {"resources": False}}
    adopter_old = {"url": owner_config["url"], "lazy": True,
                   "tools": {"resources": True}}
    adopter_new = {"url": owner_config["url"], "lazy": True,
                   "tools": {"resources": False}}
    assert cache.config_fingerprint(owner_config) == cache.config_fingerprint(adopter_old) == cache.config_fingerprint(adopter_new)
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), patch(
        "tools.mcp_tool._servers", {}
    ), patch("tools.mcp_tool._server_tool_scopes", {}), patch(
        "tools.mcp_tool._server_scope_keys", {}
    ), patch("tools.mcp_tool._server_scope_configs", {}), patch(
        "tools.registry.registry", ToolRegistry()) as registry:
        with profile(tmp_path / "owner"):
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
        with profile(tmp_path / "adopter"):
            adopter_scope = ToolRegistry.current_scope_key()
            assert registration.register_connected_into_current_scope({name: adopter_old}) == 1
            utility = f"mcp__{name}__list_resources"
            assert registry.snapshot_registration(utility, scope=adopter_scope) is not None
            assert registration.register_connected_into_current_scope({name: adopter_new}) == 0
            assert core._server_scope_configs[(owner_key, adopter_scope)] == adopter_new
            assert registry.snapshot_registration(utility, scope=adopter_scope) is None, (
                "the adopter disabled resources but its prior utility remains live")


def test_adopter_removal_then_readoption_uses_new_policy(tmp_path):
    name = "r9_readopt_policy"
    owner_config = {"url": "https://mcp.example.invalid/mcp", "lazy": True,
                    "tools": {"resources": False}}
    adopter_old = {"url": owner_config["url"], "lazy": True,
                   "tools": {"resources": True}}
    adopter_new = {"url": owner_config["url"], "lazy": True,
                   "tools": {"resources": False}}
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), patch(
        "tools.mcp_tool._servers", {}
    ), patch("tools.mcp_tool._server_tool_scopes", {}), patch(
        "tools.mcp_tool._server_scope_keys", {}
    ), patch("tools.mcp_tool._server_scope_configs", {}), patch(
        "tools.registry.registry", ToolRegistry()) as registry:
        with profile(tmp_path / "owner"):
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
        with profile(tmp_path / "adopter"):
            adopter_scope = ToolRegistry.current_scope_key()
            assert registration.register_connected_into_current_scope({name: adopter_old}) == 1
            assert registration.register_connected_into_current_scope({}) == 0
            assert adopter_scope not in core._server_tool_scopes[owner_key]
            assert (owner_key, adopter_scope) not in core._server_scope_configs
            assert registration.register_connected_into_current_scope({name: adopter_new}) == 1
            assert registry.snapshot_registration(f"mcp__{name}__list_resources", scope=adopter_scope) is None
            assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=adopter_scope) is not None


def test_same_capability_reconnect_keeps_adopter_only_utility(tmp_path):
    async def scenario(registry):
        name = "r9_adopter_only_utility"
        owner_config = {"url": "https://mcp.example.invalid/mcp", "lazy": True,
                        "tools": {"resources": False}}
        adopter_config = {"url": owner_config["url"], "lazy": True,
                          "tools": {"resources": True}}
        with profile(tmp_path / "owner"):
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

        async def same_list(**_kwargs):
            return types.ListToolsResult.model_validate({
                "tools": [tool("alpha").model_dump(by_alias=True)],
            })

        with profile(tmp_path / "adopter"):
            adopter_scope = ToolRegistry.current_scope_key()
            server.session = SimpleNamespace(list_tools=same_list)
            assert registration.register_connected_into_current_scope({name: adopter_config}) == 1
            utility = f"mcp__{name}__list_resources"
            assert registry.snapshot_registration(utility, scope=adopter_scope) is not None
            cache.write_cache_entry(name, cache.config_fingerprint(adopter_config), tools=[{
                "name": "alpha", "description": "alpha", "inputSchema": {"type": "object"},
            }])

        with profile(tmp_path / "owner"):
            # A first reconnect still advertises resources. Its republish overwrites the
            # unioned `_registered_tool_names` with the owner's alpha-only list.
            await server._discover_tools()
            assert server._registered_tool_names == [f"mcp__{name}__alpha"]
            assert registry.snapshot_registration(utility, scope=adopter_scope) is not None, (
                "successor still supports resources but adopter-only utility vanished")

    with patch("agent.secret_scope.serves_routed_profile", return_value=True), patch(
        "tools.mcp_tool._servers", {}
    ), patch("tools.mcp_tool._server_tool_scopes", {}), patch(
        "tools.mcp_tool._server_scope_keys", {}
    ), patch("tools.mcp_tool._server_scope_configs", {}), patch(
        "tools.registry.registry", ToolRegistry()) as registry:
        asyncio.run(scenario(registry))
