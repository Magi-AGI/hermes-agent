"""BLOCK-SA-1: an adoption/policy write must bind to the connection it validated, not to whatever
is live under the key when the write lands.

``_server_key`` is ``(owner_scope, name)``, so a *replacement* connection published under the same
owner+name is a different object behind the same key. The adoption pass validates its candidate's
route (``_same_server_route``) while holding ``_core._lock``, releases it, and writes the policy
later; the write's guard tested LIVENESS only (``_core._servers[key]`` non-None with a session), so
a replacement opened with a different endpoint / headers / Authorization satisfied it. The adopting
profile then gained a membership on a connection it never validated, and the reconcile — which
resolves ``_core._servers.get(key)``, not the caller's snapshot — published the REPLACEMENT's tools
into that profile's slot.

That is executable cross-profile credential substitution, not the recorded ghost-record residual:
the wrong connection is *live* and serving calls. The pre-existing teardown regressions
(``test_codex_r11_teardown_race.py``, ``test_mcp_teardown_early_window.py``) pop the key and only
the key, so they prove the REMOVAL case and never reach this ordering.

Both probes insert a live, differently-authenticated replacement in the window between the route
check and the policy write — one on the adoption GAIN branch, one on the policy-CHANGE branch — and
assert the adopting profile ends with no policy, no membership and none of the replacement's tools.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import mcp.types as types

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import mcp_tool as core
from tools import mcp_tool_registration as registration
from tools.mcp_tool import MCPServerTask
from tools.registry import ToolRegistry, registry as _live_registry  # noqa: F401  (patched per test)

OWNER_CFG = {"url": "https://mcp.example.invalid/mcp", "lazy": True,
             "headers": {"Authorization": "Bearer owner-and-adopter-share-this"}}
# Same URL, a DIFFERENT credential: ``_connection_identity`` folds headers in, so this is a
# different connection identity and must never be adopted under the first one's route check.
REPLACEMENT_CFG = {"url": "https://mcp.example.invalid/mcp", "lazy": True,
                   "headers": {"Authorization": "Bearer a-totally-different-principal"}}


@contextmanager
def profile(path):
    path.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(path)
    try:
        yield ToolRegistry.current_scope_key()
    finally:
        reset_hermes_home_override(token)


def _tool(name):
    return types.Tool.model_validate(
        {"name": name, "description": name, "inputSchema": {"type": "object"}})


def _connection(name, cfg, tool_name):
    server = MCPServerTask(name)
    server._config = cfg
    server._tools = [_tool(tool_name)]
    server.session = SimpleNamespace()
    server._list_published = True
    server._published_before = True
    return server


@contextmanager
def _isolated_ledgers():
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), \
            patch("tools.mcp_tool._server_trust_levels", {}), \
            patch("tools.mcp_tool._tool_read_only_hints", {}), \
            patch("tools.registry.registry", ToolRegistry()) as reg:
        yield reg


def _assert_not_bound_to_replacement(key, adopter, name, reg, branch):
    assert (key, adopter) not in core._server_scope_configs, (
        f"{branch}: the adopter's policy was written for a REPLACEMENT connection it never "
        "validated — the write's guard tested liveness, not the identity of the connection whose "
        "route was checked")
    assert adopter not in core._server_tool_scopes.get(key, set()), (
        f"{branch}: the adopter gained membership on a differently-authenticated replacement "
        "connection")
    assert reg.get_tool_names_for_toolset_in_scope(f"mcp-{name}", adopter) == [], (
        f"{branch}: the replacement's tools were published into the adopting profile's slot — the "
        "adopter would call them with another principal's credentials")


def test_adoption_gain_refuses_a_live_replacement_connection(tmp_path):
    """GAIN branch: the candidate is torn down and replaced between the route check and the write."""
    name = "sa1_gain_replacement"
    with _isolated_ledgers() as reg:
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            original = _connection(name, OWNER_CFG, "alpha")
            core._servers[key] = original
            core._server_scope_keys[key] = owner
            original._registered_tool_names = registration._register_server_tools(
                name, original, OWNER_CFG)

        real_write = registration._set_profile_policy
        swapped = []

        def replace_then_write(*args, **kwargs):
            # The adoption pass has already read ``shared`` and validated ``original``'s route.
            # The owner's connection finishes teardown and a replacement is published under the
            # SAME (owner, name) key with a different Authorization header, before the policy
            # write lands. This is the window BLOCK-SA-1 describes.
            if not swapped:
                swapped.append(True)
                replacement = _connection(name, REPLACEMENT_CFG, "beta")
                with core._lock:
                    core._servers[key] = replacement
                    core._server_scope_keys[key] = owner
            return real_write(*args, **kwargs)

        with profile(tmp_path / "adopter") as adopter:
            with patch.object(registration, "_set_profile_policy", side_effect=replace_then_write):
                registration.register_connected_into_current_scope({name: OWNER_CFG})
            assert swapped, "the probe never reached the policy write; the window was not exercised"
            _assert_not_bound_to_replacement(key, adopter, name, reg, "adoption GAIN")


def test_adoption_policy_change_refuses_a_live_replacement_connection(tmp_path):
    """CHANGE branch: an existing membership re-judged while its connection is replaced."""
    name = "sa1_change_replacement"
    # ``trust`` is policy, not route identity (it is absent from ``config_fingerprint`` and is not a
    # visibility filter), so this is a pure policy CHANGE on the same route.
    changed = {**OWNER_CFG, "trust": "untrusted"}
    with _isolated_ledgers() as reg:
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            original = _connection(name, OWNER_CFG, "alpha")
            core._servers[key] = original
            core._server_scope_keys[key] = owner
            original._registered_tool_names = registration._register_server_tools(
                name, original, OWNER_CFG)

        with profile(tmp_path / "adopter") as adopter:
            # A legitimate adoption first, so the next pass takes the CHANGE branch.
            assert registration.register_connected_into_current_scope({name: OWNER_CFG}) == 1
            assert (key, adopter) in core._server_scope_configs

            real_write = registration._set_profile_policy
            swapped = []

            def replace_then_write(*args, **kwargs):
                if not swapped:
                    swapped.append(True)
                    replacement = _connection(name, REPLACEMENT_CFG, "beta")
                    with core._lock:
                        core._servers[key] = replacement
                        core._server_scope_keys[key] = owner
                return real_write(*args, **kwargs)

            with patch.object(registration, "_set_profile_policy", side_effect=replace_then_write):
                registration.register_connected_into_current_scope({name: changed})
            assert swapped, "the probe never reached the policy write; the window was not exercised"
            _assert_not_bound_to_replacement(key, adopter, name, reg, "policy CHANGE")
