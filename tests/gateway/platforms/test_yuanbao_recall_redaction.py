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
    original = db.redact_user_message
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

    db.redact_user_message = rotate_first
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
    everywhere = ([r[1] for r in _rows(db, sid) if r[0] == "user"]  # the note's append flushes the queue
                  + [m["content"] for m in store._dirty_transcripts.get(sid, [])]
                  + [m["content"] for _p, _pl, m in spooled_transcript_messages({sid})])
    assert everywhere.count("repeat") == 3 and RecallGuardMiddleware._REDACTED not in everywhere
    assert any(r[0] == "system" and "missing" in r[1] for r in _rows(db, sid))

    tasks.clear()
    db.append_message(session_id=sid, role="user", content="same words")  # an unrelated, older message
    with store._transcript_retry_lock:
        store._dirty_transcripts[sid] = [{"role": "user", "content": "same words", "message_id": "p9"}]
    _recall(adapter, tasks, "p9", "same words")
    assert store._dirty_transcripts[sid][0]["content"] == RecallGuardMiddleware._REDACTED
    assert ("user", "same words", 1) in _rows(db, sid)


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
        store.redact_user_message(sid, platform_message_id="target", content="repeat",
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
        store.redact_user_message(sid, platform_message_id="target", content=SECRET, replacement="x")
    with store._transcript_retry_lock:
        store._dirty_transcripts[sid] = [{"role": "user", "content": SECRET}]
    with pytest.raises(SessionTurnLeaseLostError):
        store.redact_user_message(sid, content=SECRET, replacement="x")
    assert store._dirty_transcripts[sid][0]["content"] == SECRET  # untouched while fenced


@pytest.mark.windows_only
def test_a_failed_spool_rewrite_is_retried_and_replay_still_writes_the_redaction(store):
    """Round 3 (Codex): the spool was readable but its replacement failed; the recall task ended and
    the next replay restored an id-less original beside its redacted copy."""
    import time as _time

    from gateway.shutdown_flush import TranscriptSpoolIncompleteError, spool_dropped_transcript_message

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    db = store._db
    stamp = _time.time() - 5
    path = spool_dropped_transcript_message(sid, {"role": "user", "content": SECRET, "timestamp": stamp})
    store._spooled_drop_sessions = {sid}
    release = _hold_windows_file(path, 0x1)  # FILE_SHARE_READ only: readable, not replaceable
    try:
        with pytest.raises(TranscriptSpoolIncompleteError):
            store.redact_user_message(sid, content=SECRET, replacement=RecallGuardMiddleware._REDACTED)
    finally:
        release()
    store.append_to_transcript(sid, {"role": "assistant", "content": "next"})  # replays the spool
    assert not [r for r in _rows(db, sid) if SECRET in (r[1] or "")]
    assert ("user", RecallGuardMiddleware._REDACTED, 1) in _rows(db, sid)


@pytest.mark.windows_only
def test_a_pending_recall_survives_a_restart_and_boot_recovery_honours_it(store, tmp_path):
    """Round 3 (Codex): the pending recall lived only in memory; a new process's boot recovery replayed
    the unread spool file and inserted the original text."""
    import subprocess
    import sys
    from pathlib import Path

    from gateway.shutdown_flush import TranscriptSpoolIncompleteError, spool_dropped_transcript_message
    from hermes_constants import get_hermes_home

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    path = spool_dropped_transcript_message(sid, {"role": "user", "content": SECRET, "message_id": "p1"})
    release = _hold_windows_file(path, 0)  # no sharing: unreadable
    try:
        with pytest.raises(TranscriptSpoolIncompleteError):
            store.redact_user_message(sid, platform_message_id="p1", content=SECRET,
                                      replacement=RecallGuardMiddleware._REDACTED)
    finally:
        release()
    store._db.close()
    repo = str(Path(__file__).resolve().parents[3])
    boot = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from pathlib import Path\n"
        "from hermes_state import SessionDB\n"
        "from gateway.shutdown_flush import recover_pending_to_db\n"
        "db = SessionDB(db_path=Path(sys.argv[2]))\n"
        "print('recovered', recover_pending_to_db(db))\n"
        "db.close()\n")
    proc = subprocess.run([sys.executable, "-c", boot, repo, str(tmp_path / "state.db")],
                          capture_output=True, text=True, timeout=120,
                          env={**__import__("os").environ, "HERMES_HOME": str(get_hermes_home())})
    assert "recovered 1" in proc.stdout, proc.stderr[-2000:]
    reopened = SessionDB(db_path=tmp_path / "state.db")
    try:
        rows = _rows(reopened, sid)
    finally:
        reopened.close()
    assert rows and not [r for r in rows if SECRET in (r[1] or "")]


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
        "DROP TABLE recalled_messages; DROP TRIGGER IF EXISTS messages_recall_insert; "
        "DROP TRIGGER IF EXISTS messages_recall_update;"))
    old.close()
    db = SessionDB(db_path=path)
    try:
        db.redact_user_message(["s"], platform_message_id="p1", content="secretword here", replacement="[redacted]")
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
        assert db.redact_user_message(["s"], content="repeat", request_id="r1", replacement="[x]")[0] == 1
        db.append_message(session_id="s", role="user", content="repeat", timestamp=1_700_000_200.0)
        assert db.redact_user_message(["s"], content="repeat", request_id="r2", replacement="[x]")[0] == 1
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
    source.redact_user_message(["s"], platform_message_id="p1", replacement="[redacted]")
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
    fresh process's boot recovery then inserted the original text. The unresolved text identity is now
    a durable pending note: replay holds the copy and boot recovery resolves it first."""
    import subprocess
    import sys
    from pathlib import Path

    from gateway.shutdown_flush import TranscriptSpoolIncompleteError, spool_dropped_transcript_message
    from hermes_constants import get_hermes_home

    tasks: list = []
    adapter = _adapter(store, tasks)
    sid = _session(store, adapter)
    path = spool_dropped_transcript_message(sid, {"role": "user", "content": SECRET, "timestamp": 1_700_000_100.0})
    release = _hold_windows_file(path, 0)
    try:
        with pytest.raises(TranscriptSpoolIncompleteError):
            store.redact_user_message(sid, platform_message_id="p1", content=SECRET,
                                      replacement=RecallGuardMiddleware._REDACTED)
    finally:
        release()
    store._db.close()
    repo = str(Path(__file__).resolve().parents[3])
    boot = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from pathlib import Path\n"
        "from hermes_state import SessionDB\n"
        "from gateway.shutdown_flush import recover_pending_to_db\n"
        "db = SessionDB(db_path=Path(sys.argv[2]))\n"
        "print('recovered', recover_pending_to_db(db))\n"
        "db.close()\n")
    proc = subprocess.run([sys.executable, "-c", boot, repo, str(tmp_path / "state.db")],
                          capture_output=True, text=True, timeout=120,
                          env={**__import__("os").environ, "HERMES_HOME": str(get_hermes_home())})
    assert "recovered 1" in proc.stdout, proc.stderr[-2000:]
    reopened = SessionDB(db_path=tmp_path / "state.db")
    try:
        rows = _rows(reopened, sid)
    finally:
        reopened.close()
    assert rows and not [r for r in rows if SECRET in (r[1] or "")]
