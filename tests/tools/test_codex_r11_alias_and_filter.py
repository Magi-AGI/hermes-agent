"""Adopted from the Codex round-10 closure review of 981dd0c7aa (codex-closure-r10/mcp_agent).
The three P1 probes are fixed in round 11; the two P2 probes are deferred known limits
(Lake rule 2026-09-30) and marked xfail(strict=True) so they fail loudly once fixed."""

import pytest

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


def config(*, include, resources=False, trust=None):
    return {
        "url": "https://mcp.example.invalid/mcp", "lazy": True,
        "tools": {"include": include, "resources": resources, "prompts": False},
        **({"trust": trust} if trust is not None else {}),
    }


def tool(name):
    return types.Tool.model_validate({
        "name": name, "description": name, "inputSchema": {"type": "object"},
    })


def test_owner_empty_removing_one_adopter_preserves_survivors_alias(tmp_path):
    """The alias is process-wide even when the owner filters out every tool."""
    name = "r10_alias_adopter_only"
    owner_cfg = config(include=[], resources=False)
    adopter_cfg = config(include=[], resources=True)
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.registry.registry", ToolRegistry()) as registry:
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = owner_cfg
            server._tools = []
            server.session = SimpleNamespace(list_resources=lambda: None, read_resource=lambda: None)
            server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(
                tools=object(), resources=object(), prompts=None))
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, owner_cfg)
            assert server._registered_tool_names == []
        with profile(tmp_path / "a") as a_scope:
            assert registration.register_connected_into_current_scope({name: adopter_cfg}) == 1
            assert registry.snapshot_registration(f"mcp__{name}__list_resources", scope=a_scope)
        with profile(tmp_path / "b") as b_scope:
            assert registration.register_connected_into_current_scope({name: adopter_cfg}) == 1
            assert registry.snapshot_registration(f"mcp__{name}__list_resources", scope=b_scope)
            assert registry.get_toolset_alias_target(name) == f"mcp-{name}"
        with profile(tmp_path / "a"):
            registration.register_connected_into_current_scope({})
        with profile(tmp_path / "b"):
            assert registry.snapshot_registration(f"mcp__{name}__list_resources", scope=b_scope)
            assert registry.get_toolset_alias_target(name) == f"mcp-{name}", (
                "removing a different adopter's last local entry dropped this survivor's alias")


@pytest.mark.xfail(strict=True, reason="Deferred (Lake rule 2026-09-30): P2 per-profile filters cannot share one route; see R11-DISPOSITIONS")
def test_different_tool_filters_can_adopt_same_route(tmp_path):
    """A per-profile filter is policy, not a different connection identity."""
    name = "r10_filter_route"
    owner_cfg, adopter_cfg = config(include=["alpha"]), config(include=["beta"])
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.registry.registry", ToolRegistry()) as registry:
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = owner_cfg
            server._tools = [tool("alpha"), tool("beta")]
            server.session = SimpleNamespace()
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, owner_cfg)
        with profile(tmp_path / "adopter") as adopter:
            assert registration.register_connected_into_current_scope({name: adopter_cfg}) == 1
            assert registry.snapshot_registration(f"mcp__{name}__beta", scope=adopter)


def test_owner_filter_change_reconciles_connected_owner_on_next_pass(tmp_path):
    """Changing only a tool visibility policy must not orphan the owner's live connection."""
    name = "r10_owner_filter_change"
    initial, changed = config(include=["alpha"]), config(include=["beta"])
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.registry.registry", ToolRegistry()) as registry:
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = initial
            server._tools = [tool("alpha"), tool("beta")]
            server.session = SimpleNamespace()
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, initial)
            assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=owner)
            registration.register_connected_into_current_scope({name: changed})
            assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=owner) is None
            assert registry.snapshot_registration(f"mcp__{name}__beta", scope=owner), (
                "owner's filter changed but the registration pass removed every live tool")


def test_owner_trust_change_to_untrusted_takes_effect_on_next_registration_pass(tmp_path):
    """A policy-only edit must turn on the write-capable tool's call-time approval gate."""
    from tools import mcp_tool_handlers as handlers

    name = "r10_owner_trust_change"
    initial = config(include=["alpha"], trust="full")
    changed = config(include=["alpha"], trust="untrusted")
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
            server._tools = [tool("alpha")]
            server.session = SimpleNamespace()
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, initial)
            assert core._server_trust_levels[key] == core._TRUST_FULL
            assert handlers._trust_gate_check(name, "alpha") is None

            # The real registration pass sees the same live connection and a changed owner policy.
            # Neither the URL nor credentials changed, so no reconnect is required.
            registration.register_connected_into_current_scope({name: changed})
            assert core._server_scope_configs[(key, owner)] == changed
            assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=owner)
            with patch("tools.approval_prompt.request_elicitation_consent", return_value="deny") as ask:
                denied = handlers._trust_gate_check(name, "alpha")
            assert denied is not None and ask.called, (
                "owner trust became untrusted but the write-capable tool skipped approval")


@pytest.mark.xfail(strict=True, reason="Deferred (Lake rule 2026-09-30): P2 owner policy change leaves the status snapshot stale; see R11-DISPOSITIONS")
def test_owner_policy_change_updates_registered_name_snapshot_and_status(tmp_path):
    """The registered-name/status snapshot must follow an owner policy change."""
    from tools.mcp_tool_discovery import get_mcp_status

    name = "r10_owner_count_change"
    initial = config(include=[], resources=False)
    changed = config(include=[], resources=True)
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.registry.registry", ToolRegistry()) as registry:
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = initial
            server._tools = []
            server.session = SimpleNamespace(list_resources=lambda: None, read_resource=lambda: None)
            server.initialize_result = SimpleNamespace(capabilities=SimpleNamespace(
                tools=object(), resources=object(), prompts=None))
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            server._registered_tool_names = registration._register_server_tools(name, server, initial)
            assert server._registered_tool_names == []
            registration.register_connected_into_current_scope({name: changed})
            live = set(registry.get_tool_names_for_toolset_in_scope(f"mcp-{name}", owner))
            assert live == {f"mcp__{name}__list_resources", f"mcp__{name}__read_resource"}
            assert set(server._registered_tool_names) == live, (
                "owner's live registry changed but its registered-name snapshot did not")
            assert get_mcp_status({name: changed})[0]["tools"] == len(live)
