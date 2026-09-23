"""Ownership scenarios salvaged from the fork's delegated-ownership integration suite (bucket C).

The fork's 19-commit ownership model is not carried onto v2026.9.21 (upstream's borrowed-token
model replaces it). These are the fork's scenarios whose GUARANTEE still matters, re-expressed
against upstream's model (source: 6afa0cecac tests/tui_gateway/test_compute_host_delegation_integration.py):

- ``..._early_return_before_admission_never_ends_parent_db_row`` (fork line 716): a child that built
  the session and returned before admission must not end the parent's row at shutdown.
- ``..._parent_marked_dead_but_bound_executor_alive_survives_pruning`` (fork line 262): a parent that
  looks dead must not strip protection from a session a live executor still works on. Upstream's
  advisory registry DOES prune the dead parent's entry; the protection is the executor's durable
  turn lease, which this pins.

Two more fork scenarios are covered where their fixes landed: child restart/shutdown vs the parent
row (test_compute_host_shutdown_preserves_parent_row.py, G1) and a resident child session keeping
its version across turns (test_compute_host_version_seed.py, G4).
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import threading

from hermes_cli import active_sessions
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tests.tui_gateway.test_compute_host_borrowed_lease import (  # noqa: F401  (fixture)
    _make_frame, _seed_parent_lease, _stub_agent, _wait, isolated_env)


def test_child_early_return_before_admission_never_ends_the_parents_row(isolated_env, monkeypatch):
    parent = _seed_parent_lease("s1-key", live_session_id="s1")
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    session = {"agent": _stub_agent(["x"]), "session_key": "s1-key", "history": [],
               "history_lock": threading.Lock(), "history_version": 0, "running": False,
               "attached_images": [], "image_counter": 0, "cols": 80, "slash_worker": None,
               "inflight_turn": None, "_queued_prompt_generation": 99}
    server._sessions["s1"] = session
    try:
        host.handle_frame(_make_frame(
            "s1", queued_prompt_generation=1,  # != 99: returns before the turn is admitted
            active_session_lease={"lease_id": parent.lease_id, "session_id": "s1-key"}))
        end = _wait(out, lambda f: f["type"] == "turn.end")
        assert end["interrupted"] is True

        ended = []

        class _Db:
            def get_session(self, _sid):
                return {"source": "desktop"}

            def end_session(self, sid, reason):
                ended.append((sid, reason))

        @contextlib.contextmanager
        def _db(_session):
            yield _Db()

        monkeypatch.setattr(server, "_session_db", _db)
        host.flush_all_sessions(reason="shutdown")
        assert ended == [], "the child must never end the parent's live row"
    finally:
        server._sessions.pop("s1", None)
        host.close()
        parent.release()


def test_dead_parent_registry_entry_is_pruned_but_the_live_executor_keeps_the_session(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    parent, error = active_sessions.try_acquire_active_session(
        session_id="conv", surface="desktop", config={}, metadata={"live_session_id": "parent-sid"},
        track_liveness=True)
    assert parent is not None and error is None
    executor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("conv", source="desktop")
        assert db.try_acquire_session_turn_lease("conv", f"pid={executor.pid}:turn=isolated:platform=desktop")

        # The parent dies: forge a dead pid onto its registry entry (single-process test).
        state_path = parent.state_path
        entries = active_sessions._read_entries(state_path, strict=True)
        entries[0]["pid"] = 999999
        active_sessions._write_entries(state_path, entries)

        # Upstream's advisory registry lets another runtime claim the session...
        other, refusal = active_sessions.try_acquire_active_session(
            session_id="conv", surface="desktop", config={}, metadata={"live_session_id": "other-window"},
            track_liveness=True)
        assert refusal is None and other is not None
        # ...but it cannot run a turn or write while the executor lives: the durable lease holds.
        assert db.try_acquire_session_turn_lease("conv", f"pid={os.getpid()}:turn=takeover:platform=desktop") is False
        other.release()
    finally:
        executor.kill()
        executor.wait(10)
        db.close()
        with contextlib.suppress(Exception):
            parent.release()
