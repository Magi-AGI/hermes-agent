"""Adopted from the Codex P1 recheck 2 of 5d0828572b (codex-p1-recheck2): a concurrent owner
policy tightening that lands between a publication's policy read and its write must SURVIVE.
Codex's probe asserted the rollback (it passed on the buggy tip); these assert the fix."""

from unittest.mock import patch

from tests.tools.test_codex_r11_policy_refresh import cfg, tool, world
from tools import mcp_tool as core
from tools import mcp_tool_handlers as handlers
from tools import mcp_tool_registration as registration


def _publish_with_edit_between_read_and_write(server, name, tightened, check):
    original = registration._record_tool_trust_metadata
    edits = []

    def edit_between_read_and_write(*args, **kwargs):
        if not edits:
            registration.register_connected_into_current_scope({name: tightened})
            check()
            edits.append("applied")
        return original(*args, **kwargs)

    with patch.object(registration, "_record_tool_trust_metadata", edit_between_read_and_write):
        server._publish_tool_list([tool("alpha"), tool("beta")], {}, origin="list_changed")
    assert edits == ["applied"]


def test_owner_trust_tightening_during_refresh_survives(tmp_path):
    name = "codex_race_trust"
    initial = cfg(include=["alpha"], trust="full")
    tightened = cfg(include=["alpha"], trust="untrusted")
    with world(tmp_path / "trust", name, initial) as (server, key, owner, _registry):
        assert server._published_before

        def check():
            assert core._server_scope_configs[(key, owner)] == tightened
            assert core._server_trust_levels[key] == core._TRUST_UNTRUSTED

        _publish_with_edit_between_read_and_write(server, name, tightened, check)
        assert core._server_scope_configs[(key, owner)] == tightened
        assert core._server_trust_levels[key] == core._TRUST_UNTRUSTED
        with patch("tools.approval_prompt.request_elicitation_consent", return_value="deny") as ask:
            gate = handlers._trust_gate_check(name, "alpha")
        assert gate is not None and ask.called, "refresh restored full trust: approval was skipped"


def test_owner_filter_tightening_during_refresh_survives(tmp_path):
    name = "codex_race_filter"
    initial = cfg(include=["alpha"])
    tightened = cfg(include=["beta"])
    with world(tmp_path / "filter", name, initial) as (server, key, owner, registry):
        assert server._published_before

        def check():
            assert core._server_scope_configs[(key, owner)] == tightened
            assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=owner) is None

        _publish_with_edit_between_read_and_write(server, name, tightened, check)
        assert core._server_scope_configs[(key, owner)] == tightened
        assert registry.snapshot_registration(f"mcp__{name}__alpha", scope=owner) is None, "excluded tool returned"
        assert registry.snapshot_registration(f"mcp__{name}__beta", scope=owner) is not None
