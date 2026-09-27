"""Yuanbao recall redaction is targeted and fenced (gate item A5).

It used to load the transcript, blank the entry in memory and rewrite the WHOLE live transcript: a
message landing between the load and the rewrite was deleted, compaction-archived copies of the
recalled text survived, and nothing stopped a turn still holding the conversation from writing the
recalled message back afterwards. Now a recall is a durable request settled in one fenced
transaction over every stored and retry copy; it waits for a live turn to let go first, and whichever
path gets there (the recall itself, a spool drain, boot recovery, the housekeeping tick) finishes it.
"""

import asyncio
import json
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
    assert ("assistant", "noted", 1) in rows and ("user", "unrelated, keep me", 1) in rows
    assert len(rows) == 4  # nothing deleted, nothing appended (no branch-B note)
    store.append_to_transcript(sid, {"role": "assistant", "content": "later"})  # flushes the queued copy
    assert not [r for r in _rows(db, sid) if SECRET in (r[1] or "")]


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


def _recall(adapter, tasks, recalled_id, content):
    async def run():
        RecallGuardMiddleware._patch_transcript(adapter, recalled_id, "", "u1", content)
        await asyncio.wait_for(asyncio.gather(*tasks), 30)
    asyncio.run(run())


def test_equal_text_is_not_identity(store):
    """Content is only a fallback for id-less rows, and only for ONE logical message: a same-text
    message with its own id, or several id-less occurrences, are never rewritten (round 1, Codex)."""
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content="ok", platform_message_id="other")
    db.append_message(session_id=sid, role="user", content="ok")
    with store._transcript_retry_lock:
        store._dirty_transcripts[sid] = [{"role": "user", "content": "ok", "message_id": "other2"}]
    _recall(adapter, tasks, "missing", "ok")
    rows = _rows(db, sid)
    assert ("user", "ok", 1) in rows and rows.count(("user", RecallGuardMiddleware._REDACTED, 1)) == 1
    assert store._dirty_transcripts[sid][0]["content"] == "ok"

    tasks.clear()
    db.append_message(session_id=sid, role="user", content="again")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET timestamp = timestamp - 60 WHERE session_id = ? AND content = 'again'", (sid,)))
    db.append_message(session_id=sid, role="user", content="again")
    _recall(adapter, tasks, "missing2", "again")
    rows = _rows(db, sid)
    assert rows.count(("user", "again", 1)) == 2  # ambiguous: nothing rewritten
    assert any(r[0] == "system" and "missing2" in r[1] for r in rows)  # the model is told instead


def test_a_compression_rotation_mid_recall_is_followed_to_the_new_tip(store, tmp_path):
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, platform_message_id="p1")
    sibling = SessionDB(db_path=tmp_path / "state.db")
    original = db.resolve_recall
    rotated = []

    def rotate_first(ids, **kw):
        if not rotated:
            rotated.append(True)
            assert sibling.try_acquire_compression_lock(sid, "compressor")
            sibling.publish_compression_child(
                parent_session_id=sid, child_session_id="child", source="yuanbao",
                messages=[{"role": "user", "content": SECRET, "platform_message_id": "p1"}],
                compression_lock_holder="compressor")
            sibling.release_compression_lock(sid, "compressor")
        return original(ids, **kw)

    db.resolve_recall = rotate_first
    try:
        _recall(adapter, tasks, "p1", SECRET)
    finally:
        sibling.close()
    assert rotated
    for session in (sid, "child"):
        assert not [r for r in _rows(db, session) if SECRET in (r[1] or "")], session


def test_a_spooled_copy_is_not_replayed_after_the_recall(store):
    from gateway.shutdown_flush import spool_dropped_transcript_message

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, platform_message_id="p1")
    assert spool_dropped_transcript_message(sid, {"role": "user", "content": SECRET, "message_id": "p1"})
    store._spooled_drop_sessions = {sid}
    _recall(adapter, tasks, "p1", SECRET)
    store.append_to_transcript(sid, {"role": "assistant", "content": "next"})  # drains the spool first
    assert not [r for r in _rows(db, sid) if SECRET in (r[1] or "")]


def test_identity_is_resolved_across_the_retry_queue_and_spool_before_anything_changes(store):
    """Round 2 (Codex): the retry copies bypassed the one-message rule. Three id-less 'repeat'
    messages at different times (two queued, one spooled) are ambiguous: none is rewritten. An exact
    id that only a queued copy carries wins over an unrelated equal-text row in SQLite."""
    from gateway.shutdown_flush import spool_dropped_transcript_message, spooled_transcript_messages

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    with store._transcript_retry_lock:
        store._dirty_transcripts[sid] = [
            {"role": "user", "content": "repeat", "timestamp": 1_700_000_000.0},
            {"role": "user", "content": "repeat", "timestamp": 1_700_000_100.0}]
    assert spool_dropped_transcript_message(sid, {"role": "user", "content": "repeat", "timestamp": 1_700_000_200.0})
    _recall(adapter, tasks, "missing", "repeat")
    everywhere = ([r[1] for r in _rows(db, sid) if r[0] == "user"]
                  + [m["content"] for m in store._dirty_transcripts.get(sid, [])]
                  + [m["content"] for _p, _pl, m in spooled_transcript_messages({sid})])
    assert everywhere.count("repeat") == 3 and RecallGuardMiddleware._REDACTED not in everywhere
    assert any(r[0] == "system" and "missing" in r[1] for r in _rows(db, sid))
    store._spooled_drop_sessions = {sid}
    store.append_to_transcript(sid, {"role": "assistant", "content": "next"})  # the hold is released
    assert not spooled_transcript_messages({sid}) and not store._dirty_transcripts.get(sid)
    assert [r[1] for r in _rows(db, sid) if r[0] == "user"].count("repeat") == 3

    tasks.clear()
    db.append_message(session_id=sid, role="user", content="same words")  # an unrelated, older message
    with store._transcript_retry_lock:
        store._dirty_transcripts[sid] = [{"role": "user", "content": "same words", "message_id": "p9"}]
    _recall(adapter, tasks, "p9", "same words")
    store.append_to_transcript(sid, {"role": "assistant", "content": "after"})  # flushes the queued copy
    user_rows = [r[1] for r in _rows(db, sid) if r[0] == "user"]
    assert user_rows.count("same words") == 1 and RecallGuardMiddleware._REDACTED in user_rows


@pytest.mark.windows_only
def test_an_unreadable_spool_file_keeps_the_recall_pending_and_replay_cannot_restore_it(store, monkeypatch):
    """Round 2 (Codex): a spool file held with a non-sharing handle was skipped, the recall reported
    success, and the next replay wrote the original text back."""
    import ctypes
    from ctypes import wintypes

    from gateway.shutdown_flush import spool_dropped_transcript_message

    monkeypatch.setattr(RecallGuardMiddleware, "_REDACT_RETRY_INTERVAL_S", 0.05)
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, platform_message_id="p1")
    path = spool_dropped_transcript_message(sid, {"role": "user", "content": SECRET, "message_id": "p1"})
    store._spooled_drop_sessions = {sid}
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, no sharing
    assert handle not in (None, wintypes.HANDLE(-1).value)

    async def recall():
        RecallGuardMiddleware._patch_transcript(adapter, "p1", "", "u1", SECRET)
        await asyncio.sleep(0.5)
        pending = not all(t.done() for t in tasks)
        kernel32.CloseHandle(handle)
        store.append_to_transcript(sid, {"role": "assistant", "content": "next"})  # replays the spool
        await asyncio.wait_for(asyncio.gather(*tasks), 30)
        return pending

    try:
        still_pending = asyncio.run(recall())
    finally:
        kernel32.CloseHandle(handle)
    assert still_pending, "reported success over a spool file it could not read"
    assert not [r for r in _rows(db, sid) if SECRET in (r[1] or "")]


def _hold_windows_file(path, share_mode):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(path), 0x80000000, share_mode, None, 3, 0x80, None)  # GENERIC_READ
    assert handle not in (None, wintypes.HANDLE(-1).value)
    return lambda: kernel32.CloseHandle(handle)


def test_identity_is_decided_inside_the_fenced_transaction(store, tmp_path, monkeypatch):
    """Round 3 (Codex): candidates were read before the transaction. A live turn that wrote the real
    target id and released its lease in that gap left the target untouched and blanked an older,
    unrelated message with the same text."""
    import time as _time

    import gateway.shutdown_flush as shutdown_flush

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    stamp = _time.time() - 100
    db.append_message(session_id=sid, role="user", content="repeat", timestamp=stamp)
    sibling = SessionDB(db_path=tmp_path / "state.db")
    assert sibling.acquire_session_turn_lease(sid, "live-turn", wait_seconds=0)
    real_scan = shutdown_flush.spooled_transcript_messages

    def turn_finishes_in_the_gap(sessions):
        if not getattr(turn_finishes_in_the_gap, "done", False):
            turn_finishes_in_the_gap.done = True
            sibling.append_message(session_id=sid, role="user", content="repeat", platform_message_id="target",
                                   timestamp=stamp + 10, turn_lease_holder="live-turn")
            sibling.release_session_turn_lease(sid, "live-turn")
        return real_scan(sessions)

    monkeypatch.setattr(shutdown_flush, "spooled_transcript_messages", turn_finishes_in_the_gap)
    try:
        store.recall_message(sid, request_id="target", platform_message_id="target", content="repeat",
                             replacement=RecallGuardMiddleware._REDACTED)
    finally:
        sibling.close()
    rows = _rows(db, sid)
    assert rows[0] == ("user", "repeat", 1)  # the older, unrelated message
    assert rows[1] == ("user", RecallGuardMiddleware._REDACTED, 1)


def test_the_fence_applies_when_nothing_is_stored_yet_or_only_an_undated_retry_copy_exists(store):
    """Round 3 (Codex): with no candidate, or only an undated queued copy, the transaction returned
    before the lease check: recall "succeeded" under a live turn that then flushed the text back."""
    from hermes_state_errors import SessionTurnLeaseLostError

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    assert db.acquire_session_turn_lease(sid, "live-turn", wait_seconds=0)
    with pytest.raises(SessionTurnLeaseLostError):
        store.recall_message(sid, request_id="r1", platform_message_id="target", content=SECRET, replacement="x")
    with store._transcript_retry_lock:
        store._dirty_transcripts[sid] = [{"role": "user", "content": SECRET}]
    with pytest.raises(SessionTurnLeaseLostError):
        store.recall_message(sid, request_id="r2", content=SECRET, replacement="x")
    assert store._dirty_transcripts[sid][0]["content"] == SECRET  # untouched while fenced


@pytest.mark.windows_only
def test_an_undated_spooled_copy_is_redacted_on_replay_without_rewriting_its_file(store):
    """Round 3 (Codex): a readable spool file whose update failed replayed its original text. An undated
    copy's identity now comes from its file, the same for every reader, so the recall needs no write:
    a copy that cannot be replaced still replays as the redaction."""
    from gateway.shutdown_flush import TRANSCRIPT_CAP_DROP_REASON, _get_flush_dir, _write_payload

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    path = _write_payload(_get_flush_dir(), {"session_key": sid, "reason": TRANSCRIPT_CAP_DROP_REASON, "ts": 1,
                                             "data": {"session_id": sid, "message": {"role": "user", "content": SECRET}}})
    store._spooled_drop_sessions = {sid}
    release = _hold_windows_file(path, 0x1)  # FILE_SHARE_READ only: readable, not replaceable
    try:
        assert store.recall_message(sid, request_id="r1", content=SECRET,
                                    replacement=RecallGuardMiddleware._REDACTED)[1] == "resolved"
    finally:
        release()
    store.append_to_transcript(sid, {"role": "assistant", "content": "next"})  # replays the spool
    assert not [r for r in _rows(db, sid) if SECRET in (r[1] or "")]
    assert ("user", RecallGuardMiddleware._REDACTED, 1) in _rows(db, sid)


@pytest.mark.windows_only
def test_a_pending_recall_survives_a_restart_and_boot_recovery_honours_it(store, tmp_path):
    """Round 3 (Codex): the pending recall lived only in memory; a new process's boot recovery replayed
    the unread spool file and inserted the original text."""
    from gateway.shutdown_flush import TranscriptSpoolIncompleteError, spool_dropped_transcript_message

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    path = spool_dropped_transcript_message(sid, {"role": "user", "content": SECRET, "message_id": "p1"})
    release = _hold_windows_file(path, 0)  # no sharing: unreadable
    try:
        with pytest.raises(TranscriptSpoolIncompleteError):
            store.recall_message(sid, request_id="p1", platform_message_id="p1", content=SECRET,
                                 replacement=RecallGuardMiddleware._REDACTED)
    finally:
        release()
    store._db.close()
    assert "recovered 1" in _boot(tmp_path)
    assert not [r for r in _stored_rows(tmp_path, sid) if SECRET in (r[1] or "")]


def _boot(tmp_path) -> str:
    """Boot recovery in a fresh process (nothing in memory survives): its output."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from hermes_constants import get_hermes_home
    boot = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from pathlib import Path\n"
        "from hermes_state import SessionDB\n"
        "from gateway.shutdown_flush import recover_pending_to_db\n"
        "db = SessionDB(db_path=Path(sys.argv[2]))\n"
        "print('recovered', recover_pending_to_db(db))\n"
        "db.close()\n")
    proc = subprocess.run([sys.executable, "-c", boot, str(Path(__file__).resolve().parents[3]),
                           str(tmp_path / "state.db")], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "HERMES_HOME": str(get_hermes_home())})
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def _stored_rows(tmp_path, sid):
    reopened = SessionDB(db_path=tmp_path / "state.db")
    try:
        return _rows(reopened, sid)
    finally:
        reopened.close()


def _db_recall(db, sid, request_id, *, platform_message_id=None, content=None, replacement="[redacted]"):
    """A recall straight against the database: open the request, then settle it."""
    from gateway.transcript_recall import settle_recall
    lineage = db.recall_lineage(sid)
    db.open_recall_request(lineage, request_id=request_id, platform_message_id=platform_message_id,
                           content=content, replacement=replacement)
    return settle_recall(db, db.recall_request(lineage, request_id))


def _fts_hits(db, word):
    hits = {}
    with db._read_ctx() as conn:
        for table in ("messages_fts", "messages_fts_trigram"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
                hits[table] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (word,)).fetchone()[0]
    return hits


def _fts_integrity(db):
    with db._lock:
        for table in ("messages_fts", "messages_fts_trigram"):
            if db._conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
                db._conn.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")


def test_an_upgraded_store_keeps_its_search_index_consistent_through_a_recall(tmp_path):
    """Round 4 (Codex, P1): on a store upgraded under existing FTS triggers the recall rewrite ran
    before the index saw the original, leaving the withdrawn words searchable and the index
    failing its integrity check. Withdrawn rows are now written as their replacement."""
    path = tmp_path / "upgraded.db"
    old = SessionDB(db_path=path)
    old.create_session("s", "cli")
    old.append_message(session_id="s", role="user", content="secretword here", platform_message_id="p1")
    old._execute_write(lambda conn: conn.executescript(  # the store as it was before recalls existed
        "DROP TABLE recalled_messages; DROP TABLE recall_requests;"))
    old.close()
    db = SessionDB(db_path=path)
    try:
        _db_recall(db, "s", "p1", platform_message_id="p1", content="secretword here")
        db.append_message(session_id="s", role="user", content="secretword here", platform_message_id="p1")  # a late copy
        row_id = db.append_message(session_id="s", role="user", content="other", platform_message_id="p1")
        db.set_user_message_content("s", row_id, "secretword again")  # a prologue rewrite of a withdrawn row
        _fts_integrity(db)
        assert all(n == 0 for n in _fts_hits(db, "secretword").values()), _fts_hits(db, "secretword")
        assert {r[1] for r in _rows(db, "s")} == {"[redacted]"}
    finally:
        db.close()


def test_a_new_recall_of_equal_text_is_not_mistaken_for_an_earlier_one(tmp_path):
    """Round 4 (Codex): the first text recall's identity (timestamp 100) was reused for every later
    recall of the same text, so the message at 200 stayed visible."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", "cli")
        db.append_message(session_id="s", role="user", content="repeat", timestamp=1_700_000_100.0)
        assert _db_recall(db, "s", "r1", content="repeat", replacement="[x]") == (1, "resolved")
        db.append_message(session_id="s", role="user", content="repeat", timestamp=1_700_000_200.0)
        assert _db_recall(db, "s", "r2", content="repeat", replacement="[x]") == (1, "resolved")
        assert [r[1] for r in _rows(db, "s")] == ["[x]", "[x]"]
    finally:
        db.close()


def test_recorded_recalls_survive_recovery_and_a_profile_move(tmp_path):
    """Round 4 (Codex): recovery and session moves dropped the recall records, so a replayed copy of
    a withdrawn message came back in the recovered or destination store."""
    from hermes_cli.session_recovery import recover_session_database

    source = SessionDB(db_path=tmp_path / "source.db")
    source.create_session("s", "cli")
    source.append_message(session_id="s", role="user", content=SECRET, platform_message_id="p1")
    _db_recall(source, "s", "p1", platform_message_id="p1")
    payload = source.export_session_for_move("s")
    source.close()

    report = recover_session_database(tmp_path / "source.db", tmp_path / "recovered.db")
    assert report.get("complete", True)
    target = SessionDB(db_path=tmp_path / "moved.db")
    target.import_moved_session(payload, profile_name="elsewhere")
    for path, db in (("recovered", SessionDB(db_path=tmp_path / "recovered.db")), ("moved", target)):
        try:
            db.append_message(session_id="s", role="user", content=SECRET, platform_message_id="p1")
            assert not [r for r in _rows(db, "s") if SECRET in (r[1] or "")], path
        finally:
            db.close()


@pytest.mark.windows_only
def test_an_unresolved_text_recall_survives_a_restart_and_boot_recovery_resolves_it(store, tmp_path):
    """Round 4 (Codex): an id recall whose unreadable spooled copy had NO id recorded only the id; a
    fresh process's boot recovery then inserted the original text. The request is durable: replay holds
    the copy and boot recovery settles it first."""
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    path = _spool(sid, SECRET, timestamp=1_700_000_100.0)
    _interrupted_recall(store, adapter, tasks, "p1", SECRET, [path])
    assert "recovered 1" in _boot(tmp_path)
    rows = _stored_rows(tmp_path, sid)
    assert rows and not [r for r in rows if SECRET in (r[1] or "")]


def _spool(sid, content, **fields):
    from gateway.shutdown_flush import spool_dropped_transcript_message
    return spool_dropped_transcript_message(sid, {"role": "user", "content": content, **fields})


def _interrupted_recall(store, adapter, tasks, recalled_id, content, locked, *, keep_locked=False):
    """A recall that met unreadable spool files (no-sharing handles) and whose process then stopped;
    the store is closed as a restart would. Returns the release of the handles when *keep_locked*."""
    releases = [_hold_windows_file(path, 0) for path in locked]

    async def run():
        RecallGuardMiddleware._patch_transcript(adapter, recalled_id, "", "u1", content)
        await asyncio.sleep(0.5)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def release_all():
        for release in releases:
            release()

    try:
        asyncio.run(run())
    finally:
        store._db.close()
        if not keep_locked:
            release_all()
    return release_all


@pytest.mark.windows_only
def test_boot_recovery_resolves_the_recalled_platform_id_before_equal_text(store, tmp_path):
    """Round 5 (Codex, id_wins): boot recovery resolved the recall by text alone and overwrote an
    unrelated id-less message with the same words, although the spool held the exact recalled id."""
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    store._db.append_message(session_id=sid, role="user", content=SECRET, timestamp=1_700_000_100.0)
    path = _spool(sid, SECRET, message_id="p1", timestamp=1_700_000_200.0)
    _interrupted_recall(store, adapter, tasks, "p1", SECRET, [path])
    _boot(tmp_path)
    assert [r[1] for r in _stored_rows(tmp_path, sid)] == [SECRET, RecallGuardMiddleware._REDACTED]


@pytest.mark.windows_only
def test_boot_recovery_waits_for_every_copy_and_settles_ambiguity_with_the_note(store, tmp_path):
    """Round 5 (Codex, unreadable + ambiguous): boot resolved from the readable copies only, so one of
    two equal-text messages was taken for the target; and when it did see both, it dropped the recall
    without telling the model. Now an unreadable copy keeps the request open and every candidate held;
    once all are readable the text names two messages: both replay unchanged and the note is written."""
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    _spool(sid, SECRET, timestamp=1_700_000_100.0)
    second = _spool(sid, SECRET, timestamp=1_700_000_200.0)
    release = _interrupted_recall(store, adapter, tasks, "p1", SECRET, [second], keep_locked=True)
    try:
        _boot(tmp_path)
        assert _stored_rows(tmp_path, sid) == []  # nothing decided, nothing replayed
    finally:
        release()
    _boot(tmp_path)
    rows = _stored_rows(tmp_path, sid)
    assert [r[1] for r in rows if r[0] == "user"] == [SECRET, SECRET]
    assert [r for r in rows if r[0] == "system" and "p1" in r[1]]


@pytest.mark.windows_only
def test_boot_recovery_redacts_a_copy_spooled_without_a_timestamp(store, tmp_path):
    """Round 5 (Codex, undated): the sole, undated spooled copy was resolved without an identity replay
    could reproduce, so boot recovery stored the original text."""
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    path = _spool(sid, SECRET)
    _interrupted_recall(store, adapter, tasks, "p1", SECRET, [path])
    _boot(tmp_path)
    rows = _stored_rows(tmp_path, sid)
    assert rows and not [r for r in rows if SECRET in (r[1] or "")]


@pytest.mark.windows_only
def test_a_recall_left_open_is_settled_without_its_own_spool_file_or_a_restart(store, tmp_path, monkeypatch):
    """Round 5 (Codex, pending_without_own_spool + starvation): open recall work was only found through
    the conversation's own spool files and only by one boot pass, so a target stored in the database
    stayed visible and a held spool file blocked the rest of that session's spool until a restart."""
    from gateway.shutdown_flush import spool_dropped_transcript_message, spooled_transcript_messages
    from hermes_state import _default_db_path

    store._db.close()
    store._db = SessionDB(db_path=_default_db_path())  # where the housekeeping tick looks
    monkeypatch.setattr(RecallGuardMiddleware, "_REDACT_RETRY_INTERVAL_S", 0.05)
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    store._db.append_message(session_id=sid, role="user", content=SECRET, timestamp=1_700_000_100.0)
    other = _spool("another-session", "unrelated")
    _interrupted_recall(store, adapter, tasks, "p1", SECRET, [other])
    assert _boot(_default_db_path().parent) and SECRET not in {r[1] for r in _stored_rows(_default_db_path().parent, sid)}

    from gateway.run import _housekeeping_open_recalls
    tasks.clear()
    store._db = SessionDB(db_path=_default_db_path())
    held = _spool(sid, "second " + SECRET, timestamp=1_700_000_300.0)
    assert store._db.acquire_session_turn_lease(sid, "live-turn", wait_seconds=0)
    store._db.open_recall_request(store._db.recall_lineage(sid), request_id="p2", platform_message_id="p2",
                                  content="second " + SECRET, replacement=RecallGuardMiddleware._REDACTED)
    assert spool_dropped_transcript_message(sid, {"role": "assistant", "content": "unrelated reply"})
    _housekeeping_open_recalls(SimpleNamespace(session_store=store))  # fenced: stays open, copies held
    assert held.exists()
    store._db.release_session_turn_lease(sid, "live-turn")
    _housekeeping_open_recalls(SimpleNamespace(session_store=store))
    rows = _rows(store._db, sid)
    assert not spooled_transcript_messages({sid}) and ("assistant", "unrelated reply", 1) in rows
    assert not [r for r in rows if SECRET in (r[1] or "")]


def test_a_store_opened_by_the_trigger_based_build_is_upgraded(tmp_path):
    """Round 5 (Codex, upgrade): a store created while recalls were enforced by triggers kept its
    six-column table and the triggers, and every ordinary user append then failed. The tables are now
    reconciled like the rest of the schema and the obsolete triggers dropped."""
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "cli")
    db._execute_write(lambda conn: conn.executescript(_TRIGGER_BUILD_SCHEMA))
    db.close()
    db = SessionDB(db_path=path)
    try:
        db.append_message(session_id="s", role="user", content=SECRET, platform_message_id="p1")
        assert _db_recall(db, "s", "p1", platform_message_id="p1") == (1, "resolved")
        db.append_message(session_id="s", role="user", content=SECRET, platform_message_id="p1")
        assert [r[1] for r in _rows(db, "s")] == ["[redacted]", "[redacted]"]
        _fts_integrity(db)
    finally:
        db.close()


# The recall schema of the review build that enforced recalls with triggers (a historical fixture).
_TRIGGER_BUILD_SCHEMA = """
DROP TABLE recalled_messages; DROP TABLE IF EXISTS recall_requests;
CREATE TABLE recalled_messages (
    session_id TEXT NOT NULL, platform_message_id TEXT, content TEXT, timestamp REAL,
    replacement TEXT NOT NULL, recalled_at REAL NOT NULL);
CREATE INDEX idx_recalled_messages_session ON recalled_messages(session_id);
CREATE TRIGGER messages_recall_insert AFTER INSERT ON messages
WHEN new.role = 'user' AND EXISTS (SELECT 1 FROM recalled_messages WHERE session_id = new.session_id)
BEGIN
    UPDATE messages SET api_content = NULL, content = (
        SELECT r.replacement FROM recalled_messages r WHERE r.session_id = new.session_id
          AND ((r.platform_message_id IS NOT NULL AND r.platform_message_id = new.platform_message_id)
            OR (r.platform_message_id IS NULL AND COALESCE(new.platform_message_id, '') = ''
                AND r.content = new.content AND r.timestamp = new.timestamp))
        LIMIT 1)
    WHERE id = new.id AND EXISTS (
        SELECT 1 FROM recalled_messages r WHERE r.session_id = new.session_id
          AND ((r.platform_message_id IS NOT NULL AND r.platform_message_id = new.platform_message_id)
            OR (r.platform_message_id IS NULL AND COALESCE(new.platform_message_id, '') = ''
                AND r.content = new.content AND r.timestamp = new.timestamp)));
END;
CREATE TRIGGER messages_recall_update AFTER UPDATE OF content, platform_message_id ON messages
WHEN new.role = 'user' AND EXISTS (SELECT 1 FROM recalled_messages WHERE session_id = new.session_id)
BEGIN
    UPDATE messages SET api_content = NULL, content = (
        SELECT r.replacement FROM recalled_messages r WHERE r.session_id = new.session_id
          AND ((r.platform_message_id IS NOT NULL AND r.platform_message_id = new.platform_message_id)
            OR (r.platform_message_id IS NULL AND COALESCE(new.platform_message_id, '') = ''
                AND r.content = new.content AND r.timestamp = new.timestamp))
        LIMIT 1)
    WHERE id = new.id AND EXISTS (
        SELECT 1 FROM recalled_messages r WHERE r.session_id = new.session_id AND r.replacement IS NOT new.content
          AND ((r.platform_message_id IS NOT NULL AND r.platform_message_id = new.platform_message_id)
            OR (r.platform_message_id IS NULL AND COALESCE(new.platform_message_id, '') = ''
                AND r.content = new.content AND r.timestamp = new.timestamp)));
END;
"""


def test_a_stale_concurrent_resolver_cannot_change_a_copy_identity_after_settlement(store, monkeypatch):
    """Round 6 (Codex, stamp_race): two resolvers read one undated spool copy; the first settled it and
    recorded its guard, then the second, from its stale read, rewrote the file with another timestamp
    and replay restored the text. No resolver writes the copy now: its identity is the file's."""
    import gateway.shutdown_flush as shutdown_flush
    from gateway.transcript_recall import settle_recall

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    _spool(sid, SECRET)
    lineage = db.recall_lineage(sid)
    db.open_recall_request(lineage, request_id="r1", platform_message_id=None, content=SECRET,
                           replacement=RecallGuardMiddleware._REDACTED)
    request = db.recall_request(lineage, "r1")
    real_scan = shutdown_flush.spooled_transcript_messages
    outcomes = []

    def first_resolver_wins_in_the_gap(sessions):
        stale = real_scan(sessions)
        if not outcomes:
            monkeypatch.setattr(shutdown_flush, "spooled_transcript_messages", real_scan)
            outcomes.append(settle_recall(db, dict(request)))
        return stale

    monkeypatch.setattr(shutdown_flush, "spooled_transcript_messages", first_resolver_wins_in_the_gap)
    outcomes.append(settle_recall(db, dict(request)))
    assert [o[1] for o in outcomes] == ["resolved", "resolved"]
    shutdown_flush.recover_pending_to_db(db)
    rows = _rows(db, sid)
    assert rows and not [r for r in rows if SECRET in (r[1] or "")]


def test_an_undated_copy_is_never_taken_for_another_message_with_an_equal_timestamp(store, monkeypatch):
    """Round 6 (Codex, stamp_collision): the timestamp generated for an undated copy could equal a
    stored message's, and the two distinct messages were redacted as one. An undated copy is always
    a message of its own: with another candidate the recall is ambiguous."""
    import itertools

    import gateway.shutdown_flush as shutdown_flush
    import gateway.transcript_recall as transcript_recall

    stamp = 1_700_000_100.0
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, timestamp=stamp)
    _spool(sid, SECRET)
    monkeypatch.setattr(transcript_recall, "time", SimpleNamespace(time=lambda: stamp), raising=False)
    monkeypatch.setattr(transcript_recall, "_STAMP_SEQ", itertools.count(), raising=False)
    monkeypatch.setattr(shutdown_flush, "spool_message_timestamp", lambda _path, _payload: stamp, raising=False)
    assert store.recall_message(sid, request_id="r1", content=SECRET, replacement=RecallGuardMiddleware._REDACTED,
                                note="[recall] withdrawn")[1] == "ambiguous"
    assert (("user", SECRET, 1)) in _rows(db, sid)


def test_startup_recovery_settles_with_the_live_retry_queue_in_view(store):
    """Round 6 (Codex, boot_queue): the gateway recovers after its adapters start, but startup
    settlement ignored the store's retry queue: with the exact recalled id queued there, it took an
    older, unrelated message with the same text for the target."""
    from gateway.run import _recover_pending_transcripts
    from hermes_state import _default_db_path

    store._db.close()
    store._db = SessionDB(db_path=_default_db_path())  # the store startup recovery opens
    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, timestamp=1_700_000_100.0)
    db.open_recall_request(db.recall_lineage(sid), request_id="p1", platform_message_id="p1", content=SECRET,
                           replacement=RecallGuardMiddleware._REDACTED)
    with store._transcript_retry_lock:
        store._dirty_transcripts[sid] = [
            {"role": "user", "content": SECRET, "message_id": "p1", "timestamp": 1_700_000_200.0}]
    _recover_pending_transcripts(SimpleNamespace(session_store=store))
    store.append_to_transcript(sid, {"role": "assistant", "content": "next"})  # flushes the queue
    assert [r[1] for r in _rows(db, sid) if r[0] == "user"] == [SECRET, RecallGuardMiddleware._REDACTED]


def test_pruning_a_compressed_ancestor_keeps_an_open_recall_on_the_surviving_conversation(store, tmp_path):
    """Round 6 (Codex, prune_open_request): the request was anchored only on the conversation's root;
    pruning that ended ancestor detached it, and it settled as "none" while the surviving continuation
    kept the recalled text. Every session of the conversation now anchors it."""
    from gateway.transcript_recall import settle_open_recalls

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    assert db.try_acquire_compression_lock(sid, "compressor")
    db.publish_compression_child(parent_session_id=sid, child_session_id="child", source="yuanbao",
                                 messages=[{"role": "user", "content": SECRET, "platform_message_id": "p1"}],
                                 compression_lock_holder="compressor")
    db.release_compression_lock(sid, "compressor")
    db.open_recall_request(db.recall_lineage("child"), request_id="p1", platform_message_id="p1", content=SECRET,
                           replacement=RecallGuardMiddleware._REDACTED)
    db.prune_sessions(older_than_days=None, source="yuanbao", exclude_active_write_guards=True)
    assert db.get_session(sid) is None and db.get_session("child") is not None
    settle_open_recalls(db)
    db.append_message(session_id="child", role="user", content=SECRET, platform_message_id="p1")
    rows = _rows(db, "child")
    assert rows and not [r for r in rows if SECRET in (r[1] or "")]


def test_a_recall_left_pending_by_the_previous_review_build_is_resumed(tmp_path):
    """Round 6 (Codex, upgrade_r5_pending): a store written by the build that kept unresolved text
    recalls as ``pending`` rows was opened without carrying them over, and boot recovery replayed the
    recalled text. They are migrated into open requests."""
    from gateway.shutdown_flush import recover_pending_to_db

    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.create_session("s", "yuanbao")
    db._execute_write(lambda conn: conn.executescript(_PENDING_BUILD_SCHEMA))
    db.close()
    _spool("s", SECRET, timestamp=1_700_000_100.0)
    db = SessionDB(db_path=path)
    try:
        recover_pending_to_db(db)
        rows = _rows(db, "s")
        assert rows and not [r for r in rows if SECRET in (r[1] or "")]
    finally:
        db.close()


# The recall schema of the review build that kept unresolved text recalls as pending rows, with an id
# record and a pending text record for one recall (a historical fixture).
_PENDING_BUILD_SCHEMA = f"""
DROP TABLE recalled_messages; DROP TABLE IF EXISTS recall_requests;
CREATE TABLE recalled_messages (
    session_id TEXT NOT NULL, request_id TEXT, platform_message_id TEXT, content TEXT, timestamp REAL,
    pending INTEGER NOT NULL DEFAULT 0, replacement TEXT NOT NULL, recalled_at REAL NOT NULL);
CREATE INDEX idx_recalled_messages_session ON recalled_messages(session_id);
INSERT INTO recalled_messages VALUES ('s', 'p1', 'p1', NULL, NULL, 0, '[redacted]', 1);
INSERT INTO recalled_messages VALUES ('s', 'p1', NULL, '{SECRET}', NULL, 1, '[redacted]', 1);
"""


def _compress(db, parent, child, *, messages=None):
    db.publish_compression_child(parent_session_id=parent, child_session_id=child, source="yuanbao",
                                 messages=messages or [{"role": "assistant", "content": "summary"}],
                                 require_compression_lease=False)


def test_a_settled_recall_is_not_reopened_by_a_retry_after_compression(store):
    """Round 7 (Codex, retry_after_compaction): compression copied the guards but not the settled
    request, so a retry of the same recall on the continuation opened it afresh and the housekeeping
    tick redacted a newer, unrelated message with the same text."""
    from gateway.transcript_recall import settle_open_recalls

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, timestamp=1_700_000_100.0)
    recall = dict(request_id="p1", platform_message_id="p1", content=SECRET, replacement=RecallGuardMiddleware._REDACTED)
    assert store.recall_message(sid, **recall)[1] == "resolved"
    _compress(db, sid, "child")
    db.append_message(session_id="child", role="user", content=SECRET, timestamp=1_700_000_200.0)
    assert store.recall_message("child", **recall)[1] == "resolved"  # the outcome it already had
    settle_open_recalls(db)
    assert _rows(db, "child")[-1][1] == SECRET


def test_a_compression_publishing_while_a_recall_is_recorded_leaves_it_on_the_continuation(store):
    """Round 7 (Codex, open_during_rotation): the lineage was read before the request was written; a
    continuation published in between got no anchor, and pruning the ended parent then lost the recall
    while the continuation kept the recalled text."""
    from gateway.transcript_recall import settle_open_recalls
    from hermes_state_errors import SessionTurnLeaseLostError

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, platform_message_id="p1", timestamp=1_700_000_100.0)
    original_open = db.open_recall_request

    def compression_publishes_first(lineage, **kw):
        if not db.get_session("child"):
            _compress(db, sid, "child", messages=[
                {"role": "user", "content": SECRET, "platform_message_id": "p1", "timestamp": 1_700_000_100.0}])
            assert db.acquire_session_turn_lease("child", "live-turn", wait_seconds=0)
        return original_open(lineage, **kw)

    db.open_recall_request = compression_publishes_first
    try:
        with pytest.raises(SessionTurnLeaseLostError):  # the continuation's live turn fences the decision
            store.recall_message(sid, request_id="p1", platform_message_id="p1", content=SECRET,
                                 replacement=RecallGuardMiddleware._REDACTED)
    finally:
        db.open_recall_request = original_open
    db.release_session_turn_lease("child", "live-turn")
    assert db.prune_sessions(older_than_days=None, source="yuanbao", exclude_active_write_guards=True) == 1
    settle_open_recalls(db)
    assert _rows(db, "child")[0][1] == RecallGuardMiddleware._REDACTED


def test_a_tip_deleted_before_the_decision_leaves_the_surviving_ancestor_recalled(store):
    """Round 7 (Codex, deleted_tip_race): with the captured tip deleted just before resolution, every
    anchor of the captured lineage, including the retained parent's, was closed as gone and the parent
    kept the recalled text."""
    from gateway.transcript_recall import settle_open_recalls, settle_recall

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    db.append_message(session_id=sid, role="user", content=SECRET, platform_message_id="p1", timestamp=1_700_000_100.0)
    _compress(db, sid, "child")
    lineage = db.recall_lineage("child")
    db.open_recall_request(lineage, request_id="p1", platform_message_id="p1", content=SECRET,
                           replacement=RecallGuardMiddleware._REDACTED)
    original = db.resolve_recall

    def tip_deleted_first(lineage, **kw):
        if db.get_session("child"):
            assert db.delete_session("child")
        return original(lineage, **kw)

    db.resolve_recall = tip_deleted_first
    try:
        settle_recall(db, db.recall_request(lineage, "p1"))
    finally:
        db.resolve_recall = original
    settle_open_recalls(db)
    assert _rows(db, sid)[0][1] == RecallGuardMiddleware._REDACTED


@pytest.mark.parametrize("separation", ["delete-child", "detach-child", "move-child"])
def test_an_ancestor_left_without_its_continuation_still_completes_the_recall(tmp_path, separation):
    """Round 7 (Codex, delete_and_detach): after the continuation was deleted, detached or moved away,
    the retained, compression-ended parent refused every attempt as if it were still rotating, and
    kept the recalled text with an open request for good."""
    from gateway.transcript_recall import settle_open_recalls

    db = SessionDB(db_path=tmp_path / "state.db")
    moved = None
    try:
        db.create_session("parent", "yuanbao", session_key="agent:alpha:yuanbao:dm:u")
        db.append_message(session_id="parent", role="user", content=SECRET, platform_message_id="p1",
                          timestamp=1_700_000_100.0)
        _compress(db, "parent", "child", messages=[
            {"role": "user", "content": SECRET, "platform_message_id": "p1", "timestamp": 1_700_000_100.0}])
        db.open_recall_request(db.recall_lineage("child"), request_id="p1", platform_message_id="p1", content=SECRET,
                               replacement=RecallGuardMiddleware._REDACTED)
        if separation == "delete-child":
            assert db.delete_session("child")
        elif separation == "detach-child":
            db._write_sql("UPDATE sessions SET session_key = ? WHERE id = 'child'", ("agent:beta:yuanbao:dm:u",))
            assert db.sever_crossed_parents(["child"]) == 1
        else:
            moved = SessionDB(db_path=tmp_path / "moved.db")
            moved.import_moved_session(db.export_session_for_move("child"), profile_name="beta")
            db.delete_moved_session("child")
        settle_open_recalls(db)
        assert _rows(db, "parent")[0][1] == RecallGuardMiddleware._REDACTED
        assert not db.open_recall_requests()
    finally:
        db.close()
        if moved is not None:
            moved.close()


def test_a_later_undated_spool_copy_is_not_taken_for_one_already_recalled(store):
    """Round 7 (Codex, derived_collision): an undated spooled copy was identified by a timestamp derived
    from its file name, which two files could share; the first copy's recall then redacted a later,
    unrelated copy with the same text. Each undated copy is identified by its own key."""
    import zlib

    from gateway.shutdown_flush import TRANSCRIPT_CAP_DROP_REASON, _get_flush_dir, recover_pending_to_db

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    seen, names = {}, None
    for n in range(200_000):  # two real spool file names the file-derived timestamp could not tell apart
        name = f"pending-{n:08d}.json"
        fingerprint = zlib.crc32(name.encode("utf-8")) % 999_983
        if fingerprint in seen:
            names = (seen[fingerprint], name)
            break
        seen[fingerprint] = name
    assert names

    def spool(name, seq):
        (_get_flush_dir() / name).write_text(json.dumps({
            "session_key": sid, "reason": TRANSCRIPT_CAP_DROP_REASON, "ts": 1_700_000_000, "seq": seq,
            "data": {"session_id": sid, "message": {"role": "user", "content": SECRET}}}), encoding="utf-8")

    spool(names[0], 1)
    assert store.recall_message(sid, request_id="r1", content=SECRET,
                                replacement=RecallGuardMiddleware._REDACTED)[1] == "resolved"
    recover_pending_to_db(db)
    spool(names[1], 2)  # a later, distinct message spooled in the same second
    recover_pending_to_db(db)
    assert [r[1] for r in _rows(db, sid) if r[0] == "user"] == [RecallGuardMiddleware._REDACTED, SECRET]


def _spool_undated(sid, seq):
    from gateway.shutdown_flush import TRANSCRIPT_CAP_DROP_REASON, _get_flush_dir, _write_payload
    return _write_payload(_get_flush_dir(), {
        "session_key": sid, "reason": TRANSCRIPT_CAP_DROP_REASON, "ts": 1_700_000_000, "seq": seq,
        "data": {"session_id": sid, "message": {"role": "user", "content": SECRET}}})


def test_two_undated_copies_stay_two_messages_once_written(store):
    """Round 8 (Codex, boot_identity): two distinct undated copies spooled in the same second were
    written with one timestamp and no identity left, so a later recall of their text redacted both
    instead of reporting ambiguity."""
    from gateway.shutdown_flush import recover_pending_to_db

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    _spool_undated(sid, 1)
    _spool_undated(sid, 2)
    assert recover_pending_to_db(db) == 2
    assert store.recall_message(sid, request_id="p1", platform_message_id="p1", content=SECRET,
                                replacement=RecallGuardMiddleware._REDACTED, note="[recall] p1")[1] == "ambiguous"
    assert [r[1] for r in _rows(db, sid) if r[0] == "user"] == [SECRET, SECRET]


def test_a_recalled_undated_copy_cannot_be_restored_by_rewriting_its_row(store):
    """Round 8 (Codex, copy_update): the withdrawal of an undated copy was keyed to the copy only; once
    written, rewriting the stored row with its original text restored it."""
    from gateway.shutdown_flush import recover_pending_to_db

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    _spool_undated(sid, 1)
    assert store.recall_message(sid, request_id="r1", content=SECRET,
                                replacement=RecallGuardMiddleware._REDACTED)[1] == "resolved"
    recover_pending_to_db(db)
    with db._read_ctx() as conn:
        (row_id,) = conn.execute("SELECT id FROM messages WHERE session_id = ? AND role = 'user'", (sid,)).fetchone()
    db.set_user_message_content(sid, row_id, SECRET)
    assert [r[1] for r in _rows(db, sid)] == [RecallGuardMiddleware._REDACTED]


def test_a_child_detached_before_the_decision_leaves_the_parent_to_its_own_fence(tmp_path):
    """Round 8 (Codex, detach_fence): the captured chain was checked for deleted sessions and new
    continuations, not for a link that no longer held; a child detached just before resolution let the
    recall overwrite the retained parent while its conversation's live turn still held the lease."""
    from gateway.transcript_recall import settle_open_recalls, settle_recall

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", "yuanbao", session_key="agent:alpha:yuanbao:dm:u")
        db.append_message(session_id="parent", role="user", content=SECRET, platform_message_id="p1",
                          timestamp=1_700_000_100.0)
        _compress(db, "parent", "child", messages=[
            {"role": "user", "content": SECRET, "platform_message_id": "p1", "timestamp": 1_700_000_100.0}])
        lineage = db.recall_lineage("child")
        db.open_recall_request(lineage, request_id="p1", platform_message_id="p1", content=SECRET,
                               replacement=RecallGuardMiddleware._REDACTED)
        original = db.resolve_recall

        def detached_first(ids, **kw):
            if db.get_session("child")["parent_session_id"]:
                assert db.acquire_session_turn_lease("child", "live-turn", wait_seconds=0)
                db._write_sql("UPDATE sessions SET session_key = ? WHERE id = 'child'", ("agent:beta:yuanbao:dm:u",))
                assert db.sever_crossed_parents(["child"]) == 1
            return original(ids, **kw)

        db.resolve_recall = detached_first
        try:
            settle_recall(db, db.recall_request(["child"], "p1"))
        finally:
            db.resolve_recall = original
        assert _rows(db, "parent")[0][1] == SECRET  # its conversation's live turn still holds it
        assert _rows(db, "child")[0][1] == RecallGuardMiddleware._REDACTED
        db.release_session_turn_lease("parent", "live-turn")  # the lease stayed with the parent's conversation
        settle_open_recalls(db)
        assert _rows(db, "parent")[0][1] == RecallGuardMiddleware._REDACTED
    finally:
        db.close()


def test_a_recalled_copy_timestamp_is_not_given_to_a_later_unrelated_copy(store):
    """Round 9 (Codex, guarded_stamp_reuse): once a recalled copy's row held its replacement, its
    timestamp looked free to the next undated copy with the same text, which then matched the earlier
    withdrawal and was redacted too."""
    from gateway.shutdown_flush import recover_pending_to_db

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    _spool_undated(sid, 1)
    assert _db_recall(db, sid, "r1", content=SECRET,
                      replacement=RecallGuardMiddleware._REDACTED)[1] == "resolved"
    recover_pending_to_db(db)  # boot replays it at its envelope second
    _spool_undated(sid, 2)  # a different message, spooled in the same second
    recover_pending_to_db(db)
    assert [r[1] for r in _rows(db, sid) if r[0] == "user"] == [RecallGuardMiddleware._REDACTED, SECRET]


def test_two_undated_copies_written_in_one_second_are_both_displayed(store):
    """Round 9 (Codex, display_bump): the second copy's timestamp was moved after its display identity
    was computed, so the two messages shared one display position and history showed only one."""
    from gateway.shutdown_flush import recover_pending_to_db

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    _spool_undated(sid, 1)
    _spool_undated(sid, 2)
    assert recover_pending_to_db(db) == 2
    shown = [m for m in db.get_messages(sid, include_compacted=True) if m.get("role") == "user"]
    assert [m.get("content") for m in shown] == [SECRET, SECRET]
