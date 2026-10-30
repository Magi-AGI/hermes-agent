"""Adopted from the Codex P1 recheck of 4a4a473fff (codex-p1-recheck): an MCP list refresh must not undo a live owner policy edit."""

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


def cfg(*, include, trust="full"):
    return {"url": "https://mcp.example.invalid/mcp", "lazy": True,
            "trust": trust, "tools": {"include": list(include), "resources": False, "prompts": False}}


def tool(name):
    return types.Tool.model_validate({"name": name, "description": name,
                                      "inputSchema": {"type": "object"}})


@contextmanager
def world(tmp_path, name, initial):
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.mcp_tool._server_trust_levels", {}), \
            patch("tools.mcp_tool._tool_read_only_hints", {}), \
            patch("tools.registry.registry", ToolRegistry()) as registry:
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = initial
            server._tools = [tool("alpha"), tool("beta")]
            server.session = SimpleNamespace()
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, initial)
            yield server, key, owner, registry


def test_list_changed_does_not_restore_old_owner_trust_after_policy_edit(tmp_path):
    from tools import mcp_tool_handlers as handlers

    name = "r11_trust_refresh"
    initial = cfg(include=["alpha"], trust="full")
    changed = cfg(include=["alpha"], trust="untrusted")
    with world(tmp_path, name, initial) as (server, key, owner, registry):
        registration.register_connected_into_current_scope({name: changed})
        assert core._server_scope_configs[(key, owner)] == changed
        assert core._server_trust_levels[key] == core._TRUST_UNTRUSTED
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="deny"):
            assert handlers._trust_gate_check(name, "alpha") is not None

        # A real tools/list_changed publication uses `self._config` from the original connect.
        server._publish_tool_list([tool("alpha"), tool("beta")], {}, origin="list_changed")
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="deny") as ask:
            denied = handlers._trust_gate_check(name, "alpha")
        assert denied is not None and ask.called, (
            "list_changed restored the old full-trust config and bypassed the approval gate")
        assert core._server_scope_configs[(key, owner)] == changed


def test_list_changed_does_not_restore_old_owner_filter_after_policy_edit(tmp_path):
    name = "r11_filter_refresh"
    initial = cfg(include=["alpha"])
    changed = cfg(include=["beta"])
    with world(tmp_path, name, initial) as (server, key, owner, registry):
        registration.register_connected_into_current_scope({name: changed})
        assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=owner) is None
        assert registry.snapshot_registration(f"mcp__{name}__beta", scope=owner)

        server._publish_tool_list([tool("alpha"), tool("beta")], {}, origin="list_changed")
        assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=owner) is None
        assert registry.snapshot_registration(f"mcp__{name}__beta", scope=owner), (
            "list_changed restored the old visibility filter")
        assert core._server_scope_configs[(key, owner)] == changed
