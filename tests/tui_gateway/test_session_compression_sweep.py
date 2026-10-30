"""hardening-sweep S2-a: live compression must not break display continuity or mutate the
session key outside ``history_lock`` (pre-existing, proven by agy's convoy-resume probe A).

- The in-place history replacement left ``display_history_prefix`` untouched, so
  ``_reconcile_display_with_live`` lost its anchor (``last_shared == -1``) and post-compression
  turns vanished from messages-included payloads. The fix folds the replaced rows into the prefix:
  the display keeps the full transcript plus the summary, while the model history compacts.
- ``_sync_session_key_after_compress`` wrote ``session["session_key"]`` unlocked while readers
  sample it under ``history_lock``.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from tui_gateway import server


def test_live_compression_preserves_the_display_prefix(monkeypatch):
    import agent.conversation_compression_manual as manual

    old_rows = [{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}]
    summary = [{"role": "system", "content": "SUMMARY"}]
    monkeypatch.setattr(manual, "MIN_MESSAGES", 1)
    monkeypatch.setattr(manual, "compress_now",
                        lambda agent, msgs, request, task_id: SimpleNamespace(
                            status="compressed", after_messages=list(summary), removed=len(msgs)))
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    session = dict(agent=SimpleNamespace(session_id="k"), history=list(old_rows),
                   display_history_prefix=[], history_lock=threading.Lock(),
                   history_version=0, session_key="k")
    removed, _usage = server._compress_session_history(session, "")
    assert removed == 4
    assert session["history"] == summary
    assert session["display_history_prefix"] == old_rows, (
        "the replaced rows must move into the display prefix, or the reconcile anchor is lost "
        "and post-compression turns vanish from the transcript")


def test_compression_folds_only_the_summarized_away_rows(monkeypatch):
    """agy R2-3: compress_now PRESERVES a recent tail inside after_messages (same dict objects).
    Folding the whole old history duplicates that tail in the display and inflates counts — only
    the rows the compaction actually removed may move into the prefix."""
    import agent.conversation_compression_manual as manual
    from types import SimpleNamespace
    import threading

    u1, a1 = {"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}
    u2, a2 = {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}
    summary = {"role": "system", "content": "SUMMARY"}
    monkeypatch.setattr(manual, "MIN_MESSAGES", 1)
    # The REAL pipeline deep-copies every surviving row (agy R3-1: compression_facade deepcopy +
    # ContextCompressor fresh copies), so the preserved tail arrives as EQUAL dicts, never the
    # same objects — the fold must match by content, not identity.
    monkeypatch.setattr(manual, "compress_now",
                        lambda agent, msgs, request, task_id: SimpleNamespace(
                            status="compressed", after_messages=[dict(summary), dict(u2), dict(a2)],
                            removed=2))
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    session = dict(agent=SimpleNamespace(session_id="k"), history=[u1, a1, u2, a2],
                   display_history_prefix=[], history_lock=threading.Lock(),
                   history_version=0, session_key="k")
    server._compress_session_history(session, "")
    assert session["history"] == [summary, u2, a2]
    assert session["display_history_prefix"] == [u1, a1], (
        "the preserved tail (returned as fresh copies by the real pipeline) was duplicated "
        "into the display prefix")


def test_key_rotation_lands_under_the_history_lock(monkeypatch):
    violations = []
    lock = threading.Lock()

    class GuardedSession(dict):
        def __setitem__(self, key, value):
            if key == "session_key" and not lock.locked():
                violations.append(value)
            super().__setitem__(key, value)

    session = GuardedSession(agent=SimpleNamespace(session_id="new-key"), session_key="old-key",
                             history=[], history_lock=lock, history_version=0)
    monkeypatch.setattr(server, "_transfer_active_session_slot", lambda *a, **k: True)
    monkeypatch.setattr(server, "_emit_approval_request", lambda *a, **k: None)
    server._sync_session_key_after_compress("sid-x", session, clear_pending_title=False,
                                            restart_slash_worker=False)
    assert session["session_key"] == "new-key"
    assert not violations, f"session_key mutated outside history_lock: {violations}"


def test_concurrent_key_rotation_transfers_the_lease_once(monkeypatch):
    """agy R2-8: the key CAS decides the winner BEFORE the lease-transfer I/O runs — a second
    caller syncing the same rotation must not repeat the filesystem transfer."""
    import threading
    from types import SimpleNamespace

    transfers = []
    monkeypatch.setattr(server, "_transfer_active_session_slot",
                        lambda *a, **k: transfers.append(1) or True)
    monkeypatch.setattr(server, "_emit_approval_request", lambda *a, **k: None)
    session = dict(agent=SimpleNamespace(session_id="new-key"), session_key="old-key",
                   history=[], history_lock=threading.Lock(), history_version=0)
    server._sync_session_key_after_compress("sid-y", session, clear_pending_title=False,
                                            restart_slash_worker=False)
    server._sync_session_key_after_compress("sid-y", session, clear_pending_title=False,
                                            restart_slash_worker=False)
    assert session["session_key"] == "new-key"
    assert len(transfers) == 1, f"the lease transfer ran {len(transfers)} times for one rotation"


def _compression_session():
    """A 4-row session plus the row objects, with compress_now preconditions patched by callers."""
    import agent.conversation_compression_manual as manual
    u1, a1 = {"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}
    u2, a2 = {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}
    manual.MIN_MESSAGES = 1
    session = dict(agent=SimpleNamespace(session_id="k"), history=[u1, a1, u2, a2],
                   display_history_prefix=[], history_lock=threading.Lock(),
                   history_version=0, session_key="k")
    return session, (u1, a1, u2, a2)


def test_fold_survives_a_synthetic_replay_row_appended_after_the_tail(monkeypatch):
    """agy R4-5(a): _reappend_inflight_user_task appends a synthetic user row at
    after_messages[-1]. A suffix alignment anchored at the end sees zero overlap and folds the
    ENTIRE old history - duplicating the preserved tail. Matching must not depend on alignment."""
    import agent.conversation_compression_manual as manual
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    session, (u1, a1, u2, a2) = _compression_session()
    summary = {"role": "user", "content": "[COMPACTED] summary"}
    replay = {"role": "user", "content": "the in-flight task, replayed"}
    monkeypatch.setattr(manual, "compress_now",
                        lambda agent, msgs, request, task_id: SimpleNamespace(
                            status="compressed",
                            after_messages=[dict(summary), dict(u2), dict(a2), dict(replay)],
                            removed=2))
    server._compress_session_history(session, "")
    assert session["display_history_prefix"] == [u1, a1], (
        "the appended synthetic replay row made the fold duplicate the preserved tail")


def test_a_mutated_kept_row_folds_once_and_is_not_refolded_next_compression(monkeypatch):
    """agy R4-5(b): lean/media/tool passes rewrite CONTENT of kept tail rows. The pre-mutation
    original may fold once (it no longer exists anywhere in after), but the mutated row the
    history now holds must not fold again on the NEXT compression."""
    import agent.conversation_compression_manual as manual
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    session, (u1, a1, u2, a2) = _compression_session()
    summary = {"role": "user", "content": "[COMPACTED] summary"}
    a2_lean = {"role": "assistant", "content": "a2 content [tool result pruned]"}
    monkeypatch.setattr(manual, "compress_now",
                        lambda agent, msgs, request, task_id: SimpleNamespace(
                            status="compressed",
                            after_messages=[dict(summary), dict(u2), a2_lean], removed=2))
    server._compress_session_history(session, "")
    assert session["display_history_prefix"] == [u1, a1, a2], (
        "expected exactly the removed rows plus the pre-mutation original, nothing else")
    # Second compression keeps everything but replaces the summary; the previously mutated row
    # is returned (as a copy) unchanged this time.
    summary2 = {"role": "user", "content": "[COMPACTED] summary 2"}
    monkeypatch.setattr(manual, "compress_now",
                        lambda agent, msgs, request, task_id: SimpleNamespace(
                            status="compressed",
                            after_messages=[dict(summary2), dict(u2), dict(a2_lean)], removed=1))
    server._compress_session_history(session, "")
    assert session["display_history_prefix"] == [u1, a1, a2, summary], (
        "a kept-but-previously-mutated row was folded again on the next compression")


def test_fold_distinguishes_rows_by_tool_calls_not_just_content(monkeypatch):
    """agy R5-1: two assistant rows with equal role+content but different tool_calls are
    DIFFERENT turns. A content-only fingerprint lets the survivor be consumed by the removed
    row, erasing the wrong turn from the display. The fingerprint must cover the whole row."""
    import agent.conversation_compression_manual as manual
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    a_tc1 = {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "read"}]}
    a_tc2 = {"role": "assistant", "content": "", "tool_calls": [{"id": "t2", "name": "edit"}]}
    manual.MIN_MESSAGES = 1
    session = dict(agent=SimpleNamespace(session_id="k"), history=[a_tc1, a_tc2],
                   display_history_prefix=[], history_lock=threading.Lock(),
                   history_version=0, session_key="k")
    summary = {"role": "system", "content": "SUMMARY"}
    monkeypatch.setattr(manual, "compress_now",
                        lambda agent, msgs, request, task_id: SimpleNamespace(
                            status="compressed",
                            after_messages=[dict(summary), {k: (list(v) if isinstance(v, list) else v)
                                                            for k, v in a_tc2.items()}],
                            removed=1))
    server._compress_session_history(session, "")
    assert session["display_history_prefix"] == [a_tc1], (
        "the removed tool-call turn must fold - not the surviving one with equal content")


def test_fold_ignores_private_bookkeeping_keys(monkeypatch):
    """agy round 6: real DB-backed compactions stamp msg["_row_id"] = lastrowid in place on
    every SURVIVING row, while the old history rows hold their pre-compaction _row_id. A
    whole-row fingerprint then matches 0% of the kept tail and re-duplicates everything
    (regressing R2-3/R3-1 a third time). Underscore-prefixed keys are storage bookkeeping,
    not conversation identity."""
    import agent.conversation_compression_manual as manual
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    session, (u1, a1, u2, a2) = _compression_session()
    for i, row in enumerate(session["history"]):
        row["_row_id"] = 100 + i
    summary = {"role": "system", "content": "SUMMARY"}
    u2_new = {**{k: v for k, v in u2.items() if k != "_row_id"}, "_row_id": 901}
    a2_new = {**{k: v for k, v in a2.items() if k != "_row_id"}, "_row_id": 902}
    monkeypatch.setattr(manual, "compress_now",
                        lambda agent, msgs, request, task_id: SimpleNamespace(
                            status="compressed",
                            after_messages=[dict(summary), u2_new, a2_new], removed=2))
    server._compress_session_history(session, "")
    assert session["display_history_prefix"] == [u1, a1], (
        "restamped _row_id made every kept row read as removed - the tail duplicated again")
