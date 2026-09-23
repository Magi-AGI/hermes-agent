"""G4 (bucket C): a respawned compute host continues the parent's history_version.

The parent adopts ``max(parent, frame)`` from host frames (``_compute_host_adopt_frame_meta``), so a
child whose session was rebuilt at version 0 (``_init_session``) never rolls the parent back, but
its bumps stay BELOW the parent's value and are ignored until they catch up: a compression on a
freshly respawned host did not move the parent's version, so anything keyed on version changes
missed it. The child now seeds each rebuilt session from the turn frame's version.
"""

from __future__ import annotations

import threading
import time

from tui_gateway import server
from tui_gateway import compute_host
from tests.tui_gateway.test_compute_host_borrowed_lease import (  # noqa: F401  (fixture)
    _make_frame, _run_turn, isolated_env)


def _parent(version: int) -> dict:
    return {"session_key": "s1-key", "history": [], "history_version": version,
            "history_lock": threading.Lock(), "created_at": time.time()}


def test_compression_on_a_respawned_host_moves_the_parents_version(isolated_env):
    parent = _parent(7)
    # A freshly spawned host has no record of the session: the turn frame rebuilds it.
    server._sessions.pop("s1", None)
    _frames, end = _run_turn(_make_frame("s1", history_version=7))
    with parent["history_lock"]:
        server._compute_host_adopt_frame_meta(parent, end)
    assert parent["history_version"] >= 7

    child = server._sessions["s1"]
    before = parent["history_version"]
    with child["history_lock"]:  # what session_compression does when it rewrites the history
        child["history_version"] = int(child.get("history_version", 0)) + 1
        meta = compute_host._history_meta(child)
    with parent["history_lock"]:
        server._compute_host_adopt_frame_meta(parent, {"sid": "s1", **meta})
    assert parent["history_version"] > before, "the respawned host's compression was masked"


def test_a_reused_child_session_never_moves_below_the_frame(isolated_env):
    """A child session that already exists takes max(child, frame): the parent can be ahead after
    its own history edits between turns."""
    server._sessions.pop("s1", None)
    _run_turn(_make_frame("s1", history_version=3, request_id="t1"))
    _run_turn(_make_frame("s1", history_version=20, request_id="t2"))
    assert int(server._sessions["s1"]["history_version"]) >= 20
