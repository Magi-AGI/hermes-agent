"""Compression commits honour the durable turn lease (Codex, sync bucket C blocker 1).

Both compression commits (rotation's ``publish_compression_child`` and in-place
``archive_and_compact``) checked only the compression lock. After an old executor's turn lease
expired and a successor took it, the old executor could still publish a continuation or commit an
in-place compaction, because its compression lock stays valid independently. Commits now verify
the turn lease inside the publication transaction: a turn's commit must still hold its lease, and
a standalone (turn-less) commit is refused while another turn is live.
"""

from __future__ import annotations

import time

import pytest

from hermes_state import SessionDB
from hermes_state_errors import SessionTurnLeaseLostError

SID = "conv-a"


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    store.create_session(SID, source="desktop", model="m")
    store.append_message(SID, role="user", content="hello")
    store.append_message(SID, role="assistant", content="hi")
    try:
        yield store
    finally:
        store.close()


def _taken_over(db) -> None:
    """The old executor's lease expires and a successor takes the conversation."""
    assert db.try_acquire_session_turn_lease(SID, "old-executor", ttl_seconds=0.2)
    time.sleep(0.4)
    assert db.try_acquire_session_turn_lease(SID, "successor", ttl_seconds=300)


def _handoff():
    return [{"role": "user", "content": "[summary]"}, {"role": "assistant", "content": "ok"}]


def test_a_taken_over_executor_cannot_publish_a_rotation(db):
    assert db.try_acquire_compression_lock(SID, "old-compressor")
    _taken_over(db)
    with pytest.raises(SessionTurnLeaseLostError):
        db.publish_compression_child(
            parent_session_id=SID, child_session_id="conv-a-child", source="desktop", messages=_handoff(),
            compression_lock_holder="old-compressor", turn_lease_holder="old-executor")
    assert db.get_session("conv-a-child") is None, "no child published"
    assert db.get_session(SID)["ended_at"] is None, "parent left live for the successor"


def test_a_taken_over_executor_cannot_commit_an_in_place_compaction(db):
    assert db.try_acquire_compression_lock(SID, "old-compressor")
    _taken_over(db)
    before = [m["content"] for m in db.get_messages(SID)]
    with pytest.raises(SessionTurnLeaseLostError):
        db.archive_and_compact(SID, _handoff(), lock_holder="old-compressor", turn_lease_holder="old-executor")
    assert [m["content"] for m in db.get_messages(SID)] == before, "transcript untouched"


def test_a_standalone_compaction_is_refused_while_another_turn_is_live(db):
    assert db.try_acquire_session_turn_lease(SID, "live-turn", ttl_seconds=300)
    before = [m["content"] for m in db.get_messages(SID)]
    with pytest.raises(SessionTurnLeaseLostError):
        db.archive_and_compact(SID, _handoff())
    assert [m["content"] for m in db.get_messages(SID)] == before


def test_the_lease_holders_own_commits_still_succeed(db):
    assert db.try_acquire_session_turn_lease(SID, "owner", ttl_seconds=300)
    assert db.try_acquire_compression_lock(SID, "owner-compressor")
    db.archive_and_compact(SID, _handoff(), lock_holder="owner-compressor", turn_lease_holder="owner")
    db.publish_compression_child(
        parent_session_id=SID, child_session_id="conv-a-child", source="desktop", messages=_handoff(),
        compression_lock_holder="owner-compressor", turn_lease_holder="owner")
    assert db.get_session("conv-a-child") is not None


def test_a_turnless_compaction_without_any_live_turn_still_succeeds(db):
    """First turn of a new session / no active turn: nothing to fence against (R3)."""
    assert db.archive_and_compact(SID, _handoff()) == 2


def test_an_agents_compressor_commits_under_the_agents_turn_lease(tmp_path):
    """In-turn compression commits (proactive prune, micro-compaction) run inside the compressor,
    which has no agent reference: agent_init hands it the holder, read live at commit time."""
    from run_agent import AIAgent
    store = SessionDB(tmp_path / "agent-state.db")
    try:
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, session_db=store, session_id="s-agent")
        compressor = agent.context_compressor
        assert compressor._current_turn_lease_holder() is None
        agent._active_session_turn_lease_holder = "turn-holder-1"
        assert compressor._current_turn_lease_holder() == "turn-holder-1"
        agent._active_session_turn_lease_holder = None
        assert compressor._current_turn_lease_holder() is None
    finally:
        store.close()
