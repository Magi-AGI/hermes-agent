"""A background-process notice turn is typed at submit, so the window never paints it as the user's words."""

from __future__ import annotations

import tools.async_delegation as async_delegation
# Bodies are rebound onto server.py globals (method_ctx.bind_module): patch and call through the facade.
import tui_gateway.server as notifications


def _dispatch(monkeypatch, evt, text):
    submitted = {}
    monkeypatch.setattr(async_delegation, "claim_event_delivery", lambda e, who: object())
    monkeypatch.setattr(async_delegation, "complete_event_delivery", lambda e, claim: None)
    monkeypatch.setattr(async_delegation, "release_event_delivery", lambda e, claim: None)
    monkeypatch.setattr(notifications, "_async_delegation_display_metadata", lambda e: {"task_count": 1})

    def fake_submit(rid, sid, session, body, what, **kwargs):
        submitted.update(body=body, **kwargs)

    monkeypatch.setattr(notifications, "_notif_submit", fake_submit)
    notifications._notif_dispatch_event("sid-1", {}, evt, text)
    return submitted


def test_process_completion_turn_is_submitted_as_a_system_notice(monkeypatch):
    text = "[IMPORTANT: Background process proc_3 completed (exit code 0).]\nOutput: done"
    submitted = _dispatch(monkeypatch, {"type": "completion", "session_id": "proc_3"}, text)
    assert submitted["body"] == text  # the model still gets the notice verbatim
    assert submitted["display_kind"] == "system_notice"
    assert submitted["display_metadata"] == {"notice": "background_process"}


def test_delegation_completion_keeps_its_own_timeline_kind(monkeypatch):
    submitted = _dispatch(monkeypatch, {"type": "async_delegation"}, "[ASYNC DELEGATION complete]")
    assert submitted["display_kind"] == "async_delegation_complete"
