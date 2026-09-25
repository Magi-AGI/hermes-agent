"""Yuanbao recall redaction is targeted and fenced (gate item A5).

It used to load the transcript, blank the entry in memory and rewrite the WHOLE live transcript: a
message landing between the load and the rewrite was deleted, compaction-archived copies of the
recalled text survived, and nothing stopped a turn still holding the conversation from writing the
recalled message back afterwards. Now one transaction overwrites exactly the recalled row wherever it
is stored (live, archived, queued for a retried write) and waits for a live turn to let go first.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.yuanbao import RecallGuardMiddleware
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB

SECRET = "my card number is 4111 1111 1111 1111"


@pytest.fixture()
def store(tmp_path):
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    if store._db is not None:
        store._db.close()
    store._db = SessionDB(db_path=tmp_path / "state.db")
    yield store
    store._db.close()


def _adapter(store, tasks):
    def build_source(chat_id, chat_type="dm", user_id=None, thread_id=None, **_kw):
        return SessionSource(platform=Platform.YUANBAO, chat_id=chat_id, chat_type=chat_type,
                             user_id=user_id, thread_id=thread_id)
    return SimpleNamespace(name="yuanbao", _session_store=store, build_source=build_source,
                           _track_task=tasks.append)


def _session(store, adapter):
    return RecallGuardMiddleware._resolve_sid(store, adapter, "", "u1")


def _rows(db, sid):
    with db._read_ctx() as conn:
        return [(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT role, content, active FROM messages WHERE session_id = ? ORDER BY id", (sid,))]


def test_only_the_recalled_message_is_overwritten_everywhere_it_is_stored(store):
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, platform_message_id="p1")
    db._execute_write(lambda conn: conn.execute("UPDATE messages SET active = 0 WHERE session_id = ?", (sid,)))
    db.append_message(session_id=sid, role="user", content=SECRET, platform_message_id="p1")  # live copy
    db.append_message(session_id=sid, role="assistant", content="noted")
    db.append_message(session_id=sid, role="user", content="unrelated, keep me")
    with store._transcript_retry_lock:
        store._dirty_transcripts[sid] = [{"role": "user", "content": SECRET, "message_id": "p1"}]

    async def recall():
        RecallGuardMiddleware._patch_transcript(adapter, "p1", "", "u1", SECRET)
        await asyncio.gather(*tasks)

    asyncio.run(recall())
    rows = _rows(db, sid)
    assert not [r for r in rows if SECRET in (r[1] or "")]
    assert ("assistant", "noted", 1) in rows and ("user", "unrelated, keep me", 1) in rows
    assert len(rows) == 4  # nothing deleted, nothing appended (no branch-B note)
    assert SECRET not in str(store._dirty_transcripts.get(sid))


def test_a_live_turn_holding_the_conversation_defers_the_redaction(store, monkeypatch):
    monkeypatch.setattr(RecallGuardMiddleware, "_REDACT_RETRY_INTERVAL_S", 0.05)
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, platform_message_id="p1")
    assert db.acquire_session_turn_lease(sid, "other-executor", wait_seconds=0)

    async def recall():
        RecallGuardMiddleware._patch_transcript(adapter, "p1", "", "u1", SECRET)
        await asyncio.sleep(0.6)
        held = _rows(db, sid)
        db.release_session_turn_lease(sid, "other-executor")
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
        return held

    held = asyncio.run(recall())
    assert any(SECRET == r[1] for r in held), "redacted under a live turn's lease"
    assert not [r for r in _rows(db, sid) if SECRET in (r[1] or "")]
