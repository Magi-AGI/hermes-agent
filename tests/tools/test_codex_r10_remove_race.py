"""Adopted from the Codex round-9 closure review of fd5c4bdb98 (codex-closure-r9): adopter removal vs in-flight publication."""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types as types

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
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


def test_removed_adopter_is_not_reintroduced_by_inflight_publication(tmp_path):
    async def scenario(registry):
        name = "r9_inflight_readoption"
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
            server.session = SimpleNamespace()
            server._registered_tool_names = registration._register_server_tools(name, server, owner_config)
            core._servers[owner_key] = server
            core._server_scope_keys[owner_key] = owner_scope
        with profile(tmp_path / "adopter"):
            adopter_scope = ToolRegistry.current_scope_key()
            assert registration.register_connected_into_current_scope({name: adopter_config}) == 1
            assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=adopter_scope)

        async def same_list(**_kwargs):
            return types.ListToolsResult.model_validate({
                "tools": [tool("alpha").model_dump(by_alias=True)],
            })

        server.session = SimpleNamespace(list_tools=same_list)
        original_filter = registration._make_tool_filter
        removed = []

        def filter_with_remove(server_name, config):
            if config == adopter_config and not removed:
                # Models a profile config reconcile between the publisher's scope
                # snapshot and its registry write.
                registration._remove_server_scope(owner_key, adopter_scope)
                removed.append(True)
            return original_filter(server_name, config)

        with profile(tmp_path / "owner"), patch.object(
            registration, "_make_tool_filter", side_effect=filter_with_remove
        ):
            await server._refresh_tools()
        assert removed
        with profile(tmp_path / "adopter"):
            observed = (
                registry.snapshot_registration(f"mcp__{name}__alpha", scope=adopter_scope),
                adopter_scope in core._server_tool_scopes.get(owner_key, ()),
                (owner_key, adopter_scope) in core._server_scope_configs,
            )
            assert observed == (None, False, False), observed

    with patch("agent.secret_scope.serves_routed_profile", return_value=True), patch(
        "tools.mcp_tool._servers", {}
    ), patch("tools.mcp_tool._server_tool_scopes", {}), patch(
        "tools.mcp_tool._server_scope_keys", {}
    ), patch("tools.mcp_tool._server_scope_configs", {}), patch(
        "tools.registry.registry", ToolRegistry()) as registry:
        asyncio.run(scenario(registry))
