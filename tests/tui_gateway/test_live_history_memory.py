"""The warm/live session payload never materializes a transcript past the resume limit (checklist D5).

``_live_visible_history`` serves the persisted display lineage when a window switches to a live session.
Unlike cold resume it read the whole lineage unguarded, so the open 102 MB session of the 09-12 desktop
OOM was re-read in full on every switch. Past ``sessions.max_resume_messages`` it now keeps the live
in-memory history (the full transcript is REST-paged), as cold resume keeps the tip.
"""

import tracemalloc

import hermes_state
from hermes_state import SessionDB
from tui_gateway import server

ROWS, PAYLOAD = 1200, 20_000  # ~24 MB of message content


def _seed(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("huge", source="desktop")
    db.append_messages_batch("huge", [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i:05d}" + "x" * PAYLOAD}
        for i in range(ROWS)])
    return db


def test_live_switch_to_a_session_past_the_resume_limit_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_state, "resolved_max_resume_messages", lambda: 500)
    db = _seed(tmp_path)
    in_memory = [{"role": "user", "content": "live tail"}]
    try:
        tracemalloc.start()
        try:
            history = server._live_visible_history({"session_key": "huge"}, db, in_memory)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    finally:
        db.close()
    assert history == in_memory
    assert peak < ROWS * PAYLOAD / 10, f"peak {peak / 1e6:.1f} MB for a {ROWS * PAYLOAD / 1e6:.0f} MB transcript"


def test_live_switch_within_the_limit_still_serves_the_persisted_display(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_state, "resolved_max_resume_messages", lambda: ROWS)
    db = _seed(tmp_path)
    try:
        history = server._live_visible_history({"session_key": "huge"}, db, [])
    finally:
        db.close()
    assert [m["content"][:5] for m in history] == [f"{i:05d}" for i in range(ROWS)]


def test_history_surfaces_past_the_resume_limit_never_materialize_the_lineage(tmp_path, monkeypatch):
    """``session.history`` (the desktop's rewind row-id lookup) and ``/history`` read the whole lineage too
    (D5 r1, Codex). Past the bound the RPC refuses with 4130 (rewind then resubmits instead of trusting
    a partial list's unique match), and ``/history`` lists the live session with a note."""
    import threading

    monkeypatch.setattr(hermes_state, "resolved_max_resume_messages", lambda: 500)
    db = _seed(tmp_path)
    monkeypatch.setattr(server, "_db", db)
    session = {"session_key": "huge", "history": [{"role": "user", "content": "live tail"}],
               "history_lock": threading.Lock(), "running": False, "agent": None}
    server._sessions["runtime-huge"] = session
    try:
        tracemalloc.start()
        try:
            response = server.handle_request(
                {"id": "1", "method": "session.history", "params": {"session_id": "runtime-huge"}})
            listing = server._format_live_history_output("runtime-huge", session, "")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    finally:
        server._sessions.pop("runtime-huge", None)
        db.close()
    assert response["error"]["code"] == 4130, response
    assert "too long to list in full" in listing and "live tail" in listing
    assert peak < ROWS * PAYLOAD / 10, f"peak {peak / 1e6:.1f} MB for a {ROWS * PAYLOAD / 1e6:.0f} MB transcript"
