"""A Stop that lands while a submitted turn is starting ends it; it is never wiped by the turn start.

Found by the shared-backend concurrency harness (2026-09-25): session.interrupt waits for the same
agent build as the submitted turn's thread, so both wake together. The turn thread passed its
pre-start cancel check, the interrupt then set ``_turn_cancel_requested`` and interrupted the agent,
and the turn start called ``agent.clear_interrupt()``: the user was told "interrupted" while the turn
ran to its end (3 of 3 stops, ~34 s each).
"""

from __future__ import annotations

import threading

import pytest

from tui_gateway import server


class _TurnProceeded(Exception):
    pass


class _Agent:
    session_id = "agent-live"

    def __init__(self) -> None:
        self.cleared = 0

    def clear_interrupt(self, **_kw) -> bool:
        self.cleared += 1
        return True


def test_a_stop_landing_after_the_pre_start_check_ends_the_submitted_turn(monkeypatch):
    sid = "stop-race"
    ready = threading.Event()
    ready.set()
    agent = _Agent()
    session = {"history_lock": threading.RLock(), "running": True, "_turn_cancel_requested": False,
               "session_key": "stored-key", "agent": agent, "agent_ready": ready, "attached_images": [],
               "inflight_turn": {"user": "long answer please"}, "history": [], "transport": None}
    emitted = []

    def stop_lands_now(_session):
        # Runs after _run_after_agent_ready's own cancel check, before the turn is admitted:
        # exactly where session.interrupt landed in the harness.
        with session["history_lock"]:
            session["_turn_cancel_requested"] = True
        return True

    def proceeded(*_a, **_kw):
        raise _TurnProceeded("the turn went on to run the model after the user stopped it")

    monkeypatch.setattr(server, "_ensure_session_db_row", stop_lands_now)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", proceeded)
    monkeypatch.setattr(server, "_emit", lambda event, s, payload=None: emitted.append((event, payload)))

    try:
        server._run_after_agent_ready("r1", sid, session, "long answer please", None, None, None)
    except _TurnProceeded as exc:
        pytest.fail(str(exc))

    assert session["running"] is False
    assert ("error", {"message": "Turn cancelled before it started"}) in emitted
    assert agent.cleared == 0, "the pending stop was cleared instead of honored"
