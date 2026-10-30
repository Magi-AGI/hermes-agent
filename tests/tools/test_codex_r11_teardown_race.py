"""Adopted from the Codex round-10 closure review (codex-closure-r10/mcp_agent/test_r10_teardown_race.py),
unchanged apart from this docstring and an added policy-CHANGE case. MCP-P2-2: a shared MCP
connection destroyed after the adoption pass read its live snapshot must not gain a policy or a
membership record for the dead key; such a ghost is invisible to teardown and to the orphan
re-registration, so the profile stays tool-less until its next discovery."""

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
        yield ToolRegistry.current_scope_key()
    finally:
        reset_hermes_home_override(token)


def test_shutdown_between_shared_snapshot_and_adopter_gain_leaves_no_ghost_membership(tmp_path):
    name = "r10_shutdown_adoption"
    cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.registry.registry", ToolRegistry()):
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = cfg
            server._tools = [types.Tool.model_validate({
                "name": "alpha", "description": "alpha", "inputSchema": {"type": "object"}})]
            server.session = SimpleNamespace()
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, cfg)

        original_gain = registration._set_profile_policy

        def shutdown_then_gain(*args, **kwargs):
            # The adopter already read `shared` with a live session. Shutdown completes before
            # its policy write. This is the exact gap between lines 718-725 and 729.
            with core._lock:
                core._servers.pop(key, None)
                core._server_scope_keys.pop(key, None)
                registration._forget_connection_profiles_locked(key)
            return original_gain(*args, **kwargs)

        with profile(tmp_path / "adopter") as adopter:
            with patch.object(registration, "_set_profile_policy", side_effect=shutdown_then_gain):
                registration.register_connected_into_current_scope({name: cfg})
            assert (key, adopter) not in core._server_scope_configs, (
                "an adopter wrote policy for a connection that shutdown had already destroyed")
            assert adopter not in core._server_tool_scopes.get(key, set()), (
                "a destroyed connection was resurrected as a ghost adopter membership")


def test_shutdown_between_shared_snapshot_and_adopter_policy_change_is_a_loss(tmp_path):
    """The same window on the policy-CHANGE branch: a dead connection ends as a loss, not a change."""
    name = "r10_shutdown_adoption_change"
    cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
    changed = {**cfg, "tools": {"include": ["alpha"]}}
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.registry.registry", ToolRegistry()):
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = cfg
            server._tools = [types.Tool.model_validate({
                "name": "alpha", "description": "alpha", "inputSchema": {"type": "object"}})]
            server.session = SimpleNamespace()
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, cfg)

        # The adopter already holds a policy, so the next pass takes the CHANGE branch.
        with profile(tmp_path / "adopter") as adopter:
            assert registration.register_connected_into_current_scope({name: cfg}) == 1
            assert (key, adopter) in core._server_scope_configs

            original = registration._set_profile_policy

            def shutdown_then_change(*args, **kwargs):
                with core._lock:
                    core._servers.pop(key, None)
                    core._server_scope_keys.pop(key, None)
                    registration._forget_connection_profiles_locked(key)
                return original(*args, **kwargs)

            with patch.object(registration, "_set_profile_policy", side_effect=shutdown_then_change):
                registration.register_connected_into_current_scope({name: changed})
            assert (key, adopter) not in core._server_scope_configs, (
                "a policy change landed on a connection shutdown had already destroyed")
            assert adopter not in core._server_tool_scopes.get(key, set())
