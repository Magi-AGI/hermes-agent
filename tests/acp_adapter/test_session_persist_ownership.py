"""ACP persistence never deletes rows another executor is writing (Codex, sync bucket C blocker 3).

Restore an ACP session in a second client, then switch its model while the first client runs a
turn: the replacement agent has ``_session_db_created=False``, so ``_persist`` replaced the active
messages from its stale history with no turn-lease check, deleting the first client's new rows.
Metadata saves (model/mode/config/cwd) no longer touch the transcript; a transcript sync from a
non-owning agent is refused while another turn holds the conversation, and archives rather than
deletes whatever it replaces.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from acp_adapter.session import SessionManager
from hermes_state import SessionDB


def _stale_agent():
    # A replacement agent after a model switch / restore: not the DB's persistence owner.
    return SimpleNamespace(model="m", provider=None, base_url=None, api_mode=None, _session_db_created=False)


@pytest.fixture
def setup(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    manager = SessionManager(agent_factory=_stale_agent, db=db)
    state = manager.create_session(cwd="/work")
    state.history.extend([{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}])
    manager.save_session(state.session_id)
    sid = state.session_id
    # Client 1 is mid-turn: it holds the durable lease and has written a new row.
    assert db.try_acquire_session_turn_lease(sid, "client-1-turn", ttl_seconds=300)
    db.append_message(sid, role="user", content="client 1's live prompt", turn_lease_holder="client-1-turn")
    try:
        yield manager, db, state
    finally:
        db.close()


def _live(db, sid):
    return [m["content"] for m in db.get_messages(sid)]


def test_a_metadata_save_never_rewrites_the_transcript(setup):
    manager, db, state = setup
    state.agent, state.model = _stale_agent(), "switched-model"  # what the model switch does
    manager.save_session(state.session_id, transcript=False)
    assert _live(db, state.session_id) == ["q1", "a1", "client 1's live prompt"]


def test_a_stale_transcript_sync_is_refused_while_another_turn_is_live(setup):
    manager, db, state = setup
    manager.save_session(state.session_id)  # stale history: q1, a1 only
    assert _live(db, state.session_id) == ["q1", "a1", "client 1's live prompt"]


def test_a_replaced_row_is_archived_not_deleted(setup):
    manager, db, state = setup
    db.release_session_turn_lease(state.session_id, "client-1-turn")
    state.history[:] = [{"role": "user", "content": "q1"}]  # e.g. /clear-style shrink
    manager.save_session(state.session_id)
    assert _live(db, state.session_id) == ["q1"]
    everything = [m["content"] for m in db.get_messages(state.session_id, include_inactive=True)]
    assert "client 1's live prompt" in everything and "a1" in everything, "dropped rows stay recoverable"
