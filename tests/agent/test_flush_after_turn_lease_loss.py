"""A flush refused for turn-lease loss is never retried without its fencing token (Codex, sync bucket C
blocker 2).

A turn whose lease was taken over has its flush rejected (SessionTurnLeaseLostError), leaving its
pending messages unpersisted. Turn cleanup then clears ``_active_session_turn_lease_holder``, and a
later shutdown flush (``_persist_session`` from session finalize) retried those messages with
``None``, which skips the turn-lease check entirely, writing the old executor's rows into a
conversation the successor owns.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent

SID = "sess-lease-loss"


@pytest.fixture()
def agent_db():
    tmp = tempfile.mkdtemp(prefix="flush_lease_loss_")
    db = SessionDB(Path(tmp) / "state.db")
    db.create_session(session_id=SID, source="desktop", model="test-model")
    agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, session_db=db, session_id=SID)
    agent._session_db_created = True
    try:
        yield agent, db
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _contents(db):
    return [m["content"] for m in db.get_messages(SID)]


def _take_over(agent, db):
    assert db.try_acquire_session_turn_lease(SID, "old-executor", ttl_seconds=0.2)
    agent._active_session_turn_lease_holder = "old-executor"
    agent._active_session_turn_lease_ttl_seconds = 0.2
    time.sleep(0.4)
    assert db.try_acquire_session_turn_lease(SID, "successor", ttl_seconds=300)


def _fenced_flush(agent, messages):
    # The flush reports failure (False) rather than raising; nothing lands.
    assert agent._flush_messages_to_session_db(messages) is False


def test_a_cleared_holder_does_not_authorize_a_late_retry(agent_db):
    agent, db = agent_db
    _take_over(agent, db)
    messages = [{"role": "user", "content": "old executor's prompt"},
                {"role": "assistant", "content": "old executor's reply"}]
    _fenced_flush(agent, messages)

    # Turn cleanup clears the active-turn field; the shutdown flush retries.
    agent._active_session_turn_lease_holder = None
    assert agent._flush_messages_to_session_db(messages) is False
    # A row created after the loss (e.g. an interrupt notice) is fenced by the lost holder too.
    assert agent._flush_messages_to_session_db(messages + [{"role": "user", "content": "late"}]) is False
    assert _contents(db) == [], "the old executor's rows must not land while the successor owns it"


def test_a_holderless_retry_stays_fenced_after_the_successor_releases(agent_db):
    """Nobody holds the conversation any more, but this agent never re-won it: a holder-less
    shutdown flush is still fenced (by the lost holder), so it cannot slip in unowned."""
    agent, db = agent_db
    _take_over(agent, db)
    messages = [{"role": "user", "content": "stale"}]
    _fenced_flush(agent, messages)
    agent._active_session_turn_lease_holder = None
    db.release_session_turn_lease(SID, "successor")
    db.append_message(SID, role="user", content="successor's row")
    assert agent._flush_messages_to_session_db(messages) is False
    assert _contents(db) == ["successor's row"]


def test_an_ordinary_turn_end_still_flushes_leftovers(agent_db):
    """Control: no lease loss recorded, so a holder-less shutdown flush persists normally."""
    agent, db = agent_db
    agent._active_session_turn_lease_holder = None
    agent._flush_messages_to_session_db([{"role": "user", "content": "leftover"}])
    assert _contents(db) == ["leftover"]
