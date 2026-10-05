"""MCP-P2-1 round 2 (B4) and relay finding R2 — the owner publication path's lock boundary.

Three contracts about work that lands at or after the per-connection lock that protects publication.
Read the dispositions: they differ, and only two of these were red on ``d9ee36e999``.

B4 — ``_registered_tool_names`` assigned by the CALLER, outside the lock. **Codex's stated mechanism
does not hold, and this file is the evidence.** The claim was that an interleaved owner policy edit
installs the correct snapshot and "publication then overwrites it with its earlier names". It cannot:
the value the callers assigned is the RETURN of ``_register_server_tools``, which is
``_reconcile_connection``'s owner names computed by the final reconcile pass — and the round-10
policy epoch makes a pass whose input moved restart, so the return value is already the POST-edit
set. ``test_a_policy_edit_during_publication_is_not_overwritten_by_the_publications_names`` drives
exactly that race through the real ``_publish_tool_list`` caller and **passes on ``d9ee36e999``**.
It is kept as the contract pin for the convergence property, not as a regression test.

What IS real is narrower than the report said: the assignment happens after
``_connection_lock(key)`` is released, so a policy edit landing in the gap between the return and
the caller's store has its ``_record_owner_name_snapshot`` write clobbered. That window is a few
bytecodes wide and no deterministic probe can sit inside it, so it is closed STRUCTURALLY instead of
by test: ``_register_server_tools`` is now the single writer of the attribute and the callers read it
back.

Severity — the one explicit decision, because the two reviewers split and so did this branch's own
records. Codex said P2; Gemini said P3 on identical code facts; the commit that introduced this
file argues in its message for P2 ("a lost write with no deadline is not a cosmetic lag"), while
this docstring is the adjudicated record and says **P3**. Reconciled deliberately rather than left
as drift, on three grounds the P2 argument does not address:

1. the stated P2 mechanism is DISPROVED above (the epoch makes the return value post-edit), so the
   wide window the P2 rating was assigned to does not exist;
2. what remains is a few bytecodes between the return and the caller's store -- not a window a
   policy edit realistically lands in;
3. the damage is confined to ``_registered_tool_names``, which only feeds status and tool-count
   display (``get_mcp_status``, ``_connected_summary``). It does NOT gate tool availability or
   trust: the registry holds the real entries, so no tool is lost, exposed, or mis-authorised.

The "no deadline" point stands and is why this is still FIXED rather than accepted -- unbounded
display staleness is a real defect. It is just not a deploy blocker. Anyone re-reading the commit
message should treat THIS as the current disposition.

R2 (relay finding) — the owner policy write had no ``require_live``, and that IS a real P2 of the
same class as MCP-P2-2: a teardown landing mid-publication popped the key and swept the policies,
after which the publication re-recorded an owner policy for a destroyed connection. Red on
``d9ee36e999``.

The third test pins the legitimate not-in-``_servers`` publication (a retained initial-failure
connection) so the R2 guard cannot regress into returning such a server with zero tools. Also red
on ``d9ee36e999``, because the single-writer change is what settles its snapshot.
"""

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


def _tools(*names):
    return [types.Tool.model_validate({"name": n, "description": n, "inputSchema": {"type": "object"}})
            for n in names]


@contextmanager
def clean_mcp_state():
    with patch("agent.secret_scope.serves_routed_profile", return_value=True), \
            patch("tools.mcp_tool._servers", {}), patch("tools.mcp_tool._server_tool_scopes", {}), \
            patch("tools.mcp_tool._server_scope_keys", {}), patch("tools.mcp_tool._server_scope_configs", {}), \
            patch("tools.mcp_tool._server_scope_epochs", {}), patch("tools.mcp_tool._orphaned_adopters", {}), \
            patch("tools.registry.registry", ToolRegistry()):
        yield


def test_a_policy_edit_during_publication_is_not_overwritten_by_the_publications_names(tmp_path):
    """The snapshot must equal what the owner's slot HOLDS, not what publication started with.

    Driven through the real caller, ``MCPServerTask._publish_tool_list`` (the ``list_changed`` /
    reconnect path), because the bug IS the caller's assignment after the lock: the owner narrows
    its filter to ``alpha`` while ``_register_server_tools`` is reconciling, the reconcile installs
    the correct one-name snapshot, and the caller then re-assigns the pre-edit two-name list it had
    captured as the return value.
    """
    name = "r12_publication_snapshot"
    cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
    narrowed = {**cfg, "tools": {"include": ["alpha"]}}
    with clean_mcp_state():
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = cfg
            server._tools = _tools("alpha", "beta")
            server.session = SimpleNamespace()
            core._servers[key] = server
            core._server_scope_keys[key] = owner
            # First publication: both tools, so the snapshot the race can clobber is non-trivial.
            registration._register_server_tools(name, server, cfg)
            assert sorted(server._registered_tool_names) == [
                f"mcp__{name}__alpha", f"mcp__{name}__beta"]

            original = registration._reconcile_connection
            edits: list = []

            def edit_then_reconcile(*args, **kwargs):
                # The owner narrows its policy while publication is mid-flight. Re-entrant, so it
                # runs under the same RLock the publication holds -- the epoch check is what makes
                # the publication converge on the NEW input.
                if not edits:
                    edits.append(True)
                    registration._set_profile_policy(key, owner, narrowed, server=server)
                return original(*args, **kwargs)

            with patch.object(registration, "_reconcile_connection", side_effect=edit_then_reconcile):
                server._publish_tool_list(server._tools, {}, origin="list_changed")

            from tools.registry import registry
            landed = sorted(registry.get_tool_names_for_toolset_in_scope(f"mcp-{name}", owner))
            assert landed == [f"mcp__{name}__alpha"], (
                "the narrowed policy did not reach the registry; the probe is not exercising the race")
            assert sorted(server._registered_tool_names) == landed, (
                "the publication's pre-edit names overwrote the snapshot the policy edit installed: "
                f"snapshot={sorted(server._registered_tool_names)} slot={landed}")


def test_the_owner_publication_does_not_record_a_policy_for_a_connection_teardown_destroyed(tmp_path):
    """Relay R2: the owner path re-validates liveness like the adoption paths do.

    The teardown lands after the publication read ``_servers`` and before its policy write — the
    same window ``require_live`` closes for adopters. A destroyed connection must end as a policy
    LOSS, not a resurrected owner record.
    """
    name = "r12_owner_teardown_window"
    cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
    with clean_mcp_state():
        with profile(tmp_path / "owner") as owner:
            key = (owner, name)
            server = MCPServerTask(name)
            server._config = cfg
            server._tools = _tools("alpha")
            server.session = SimpleNamespace()
            core._servers[key] = server
            core._server_scope_keys[key] = owner

            original_policy = registration._set_profile_policy

            def teardown_then_write(*args, **kwargs):
                with core._lock:
                    core._servers.pop(key, None)
                    core._server_scope_keys.pop(key, None)
                    registration._forget_connection_profiles_locked(key)
                return original_policy(*args, **kwargs)

            with patch.object(registration, "_set_profile_policy", side_effect=teardown_then_write):
                registration._register_server_tools(name, server, cfg)

            assert (key, owner) not in core._server_scope_configs, (
                "the owner publication recorded a policy for a connection shutdown had destroyed")
            assert owner not in core._server_tool_scopes.get(key, set()), (
                "a destroyed connection was resurrected as a ghost owner membership")


def test_a_retained_initial_failure_connection_can_still_publish(tmp_path):
    """The R2 guard must not break the legitimate not-in-``_servers`` publication.

    ``mcp_tool_transport._register_discovered_tools_if_needed`` publishes for a server that is
    retained after a recoverable initial failure and whose ``_ready`` is set — it was never the
    ``_servers`` entry, so "not live" there is normal, not a teardown. Pinning this keeps the fix
    from becoming a regression that returns such a server with zero tools.
    """
    name = "r12_retained_initial_failure"
    cfg = {"url": "https://mcp.example.invalid/mcp", "lazy": True}
    with clean_mcp_state():
        with profile(tmp_path / "owner") as owner:
            server = MCPServerTask(name)
            server._config = cfg
            server._tools = _tools("alpha")
            server.session = SimpleNamespace()
            # Deliberately NOT in _core._servers: the retained-initial-failure shape.
            names = registration._register_server_tools(name, server, cfg)
            assert names == [f"mcp__{name}__alpha"], (
                "a retained initial-failure connection published zero tools")
            assert list(server._registered_tool_names) == names, (
                "the snapshot was not settled for a server that is not the _servers entry")
            assert (registration._server_key_for_task(server), owner) in core._server_scope_configs
