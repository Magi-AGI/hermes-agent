"""prompt.submit rechecks ``running`` and claims the turn in ONE critical section (review: Codex, A13)."""

import threading

import pytest

from tui_gateway import server


class _InterleavingLock:
    """``history_lock`` stand-in: when the submit's busy check releases it, a competing submit claims
    the session (running + its own in-flight turn) before this submit re-enters to claim."""

    def __init__(self, session):
        self._lock = threading.RLock()
        self._session = session
        self._releases = 0

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._releases += 1
        self._lock.release()
        if self._releases == 1:  # prompt.submit's busy check just saw running False
            with self._lock:
                self._session["running"] = True
                self._session["inflight_turn"] = {"user": "competing submit"}
                self._session["_run_thread"] = "competing worker"
        return False

    def acquire(self, *a, **kw):
        return self._lock.acquire(*a, **kw)

    def release(self):
        self._lock.release()


def test_submit_that_loses_the_claim_goes_through_busy_handling(monkeypatch):
    sid = "claim-race"
    session = dict(running=False, history=[], session_key="stored", agent=None, queued_prompt=None,
                   transport=None, attached_images=[], inflight_turn=None)
    session["history_lock"] = _InterleavingLock(session)
    busy_calls = []
    monkeypatch.setattr(server, "_sessions", {sid: session})
    monkeypatch.setattr(server, "current_transport", lambda: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_legacy_group_fence_error", lambda *a: None)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *a: False)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_handle_busy_submit",
                        lambda *a, **kw: busy_calls.append(a[3]) or {"result": {"status": "queued"}})
    monkeypatch.setattr(server, "_persist_session_row_for_submit",
                        lambda *a, **kw: pytest.fail("the losing submit started a second turn"))
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **kw: pytest.fail("second agent build"))

    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "prompt.submit",
                                      "params": {"session_id": sid, "text": "late submit"}})

    assert response.get("result") == {"status": "queued"}, response
    assert busy_calls == ["late submit"]
    # The winner's turn state is untouched by the loser.
    assert session["running"] is True
    assert session["inflight_turn"] == {"user": "competing submit"}
    assert session["_run_thread"] == "competing worker"


def test_lock_in_refuses_an_already_running_session_without_side_effects():
    session = {"history_lock": threading.RLock(), "running": True, "history": [{"role": "user"}],
               "inflight_turn": {"user": "live"}, "_turn_cancel_requested": True}
    err, fields = server._lock_in_submit_turn("r", "s", session, "hello", {}, False, None, None, None)
    assert err is server._SUBMIT_CLAIM_LOST and fields == {}
    assert session["inflight_turn"] == {"user": "live"}
    assert session["_turn_cancel_requested"] is True
    assert session["history"] == [{"role": "user"}]
