"""G2 (bucket C): the continuation of a mid-turn compression is owned BEFORE any continuation write.

Codex's bar for G2: after a compression rotates the session id A -> B, ownership of B must be
established before any write to B, and losing it must stop the writer. Upstream's durable turn
lease already provides this at the write level: the lease is keyed by the compression LINEAGE ROOT
(``_session_turn_lease_key_on_conn``), so A and B share one row from the moment
``publish_compression_child`` links them, and every turn write re-checks its holder in the same
transaction (``SessionTurnLeaseLostError``), which ``DurableTurnLease.refresh_tick`` turns into an
interrupt. These tests pin that guarantee against a real SessionDB. What they do NOT cover is the
advisory active-session registry, which still re-anchors only at turn.end (see the G2 section of
BUCKET-C-SUPERSESSION-v2026.9.21.md).
"""

from __future__ import annotations

import os

import pytest

from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError


@pytest.fixture()
def rotated(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("A", source="desktop")
    db.append_message("A", "user", content="before compression")
    turn_holder = f"pid={os.getpid()}:turn=child-turn:platform=desktop"
    assert db.try_acquire_session_turn_lease("A", turn_holder)
    compression_holder = f"pid={os.getpid()}:compress"
    assert db.try_acquire_compression_lock("A", compression_holder)
    db.publish_compression_child(
        parent_session_id="A", child_session_id="B", source="desktop",
        messages=[{"role": "user", "content": "compacted handoff"}], compression_lock_holder=compression_holder)
    try:
        yield db, turn_holder
    finally:
        db.close()


def test_the_continuation_is_fenced_by_the_same_turn_lease(rotated):
    db, holder = rotated
    assert db._session_turn_lease_key("B") == db._session_turn_lease_key("A") == "A"
    assert db.try_acquire_session_turn_lease("B", f"pid={os.getpid()}:turn=other-window:platform=desktop") is False


def test_only_the_lease_holder_can_write_the_continuation(rotated):
    db, holder = rotated
    db.append_message("B", "assistant", content="continuation", turn_lease_holder=holder)
    with pytest.raises(SessionTurnLeaseLostError):
        db.append_message("B", "assistant", content="intruder", turn_lease_holder=f"pid={os.getpid()}:turn=x:platform=desktop")


def test_takeover_waits_for_a_live_previous_executor_and_follows_its_death(tmp_path):
    """G3: the parent died mid-isolated-turn, so its registry entry is prunable, but the DURABLE turn
    lease is held by the child executor (holder pid = child). A successor cannot take the session while
    that pid lives; once the executor process is gone its lease is reclaimable (dead-pid rule)."""
    import subprocess
    import sys

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("A", source="desktop")
        executor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert db.try_acquire_session_turn_lease("A", f"pid={executor.pid}:turn=isolated:platform=desktop")
            successor = f"pid={os.getpid()}:turn=takeover:platform=desktop"
            assert db.try_acquire_session_turn_lease("A", successor) is False, "must wait while the executor lives"
        finally:
            executor.kill()
            executor.wait(10)
        assert db.try_acquire_session_turn_lease("A", successor) is True, "reclaimable once the executor is gone"
    finally:
        db.close()


def test_a_writer_that_lost_the_lease_is_stopped_before_writing_the_continuation(rotated):
    db, holder = rotated
    db.release_session_turn_lease("B", holder)  # e.g. reclaimed after the holder died
    assert db.try_acquire_session_turn_lease("B", f"pid={os.getpid()}:turn=successor:platform=desktop")
    with pytest.raises(SessionTurnLeaseLostError):
        db.append_message("B", "assistant", content="late continuation", turn_lease_holder=holder)
