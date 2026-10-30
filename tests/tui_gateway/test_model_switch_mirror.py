"""Model-switch mirror hygiene (agy R4-7): a committed switch invalidates the metadata
mirror's model/provider BEFORE the switch's own session.info broadcast, for every committed
switch including one-turn switches - otherwise the emit (and every info until the next host
turn) reports the stale pre-switch host model."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from tui_gateway import server


def _patch_commit_helpers(monkeypatch, emit):
    # model_switch functions are rebound onto server's globals (bind_module), so the helpers
    # they call resolve there — patch on server, like every other model_switch test.
    monkeypatch.setattr(server, "_restart_slash_worker", lambda sid, s: None)
    monkeypatch.setattr(server, "_persist_live_session_runtime", lambda s: None)
    monkeypatch.setattr(server, "_persist_live_session_system_prompt", lambda s: None)
    monkeypatch.setattr(server, "_append_model_switch_marker", lambda s, **kw: None)
    monkeypatch.setattr(server, "_emit_session_info", emit)


def _result():
    return SimpleNamespace(new_model="new-model", target_provider="prov", api_key="k",
                           base_url="", api_mode="", runtime_capabilities=None)


def test_commit_pops_mirror_model_before_the_switch_broadcast(monkeypatch):
    session = {
        "_metadata_mirror": {"model": "stale-host-model", "provider": "stale-host-provider",
                             "usage": {"total": 1}},
        "history_lock": threading.Lock(),
    }
    seen = {}

    def emit(sid, sess):
        seen["mirror_at_emit"] = dict(sess.get("_metadata_mirror") or {})

    _patch_commit_helpers(monkeypatch, emit)
    agent = SimpleNamespace(switch_model=lambda **kw: None, model="new-model")
    server._commit_agent_switch("sid", session, agent, _result(), "old-model", None)
    assert "model" not in seen["mirror_at_emit"], (
        "the switch broadcast carried the stale mirror model")
    assert "provider" not in seen["mirror_at_emit"]
    assert session["_metadata_mirror"].get("usage") == {"total": 1}, (
        "only model/provider go stale on a switch; other mirror keys must survive")


def test_commit_pops_mirror_for_one_turn_switches_too(monkeypatch):
    """A one-turn switch swaps the live agent in place just the same; _commit runs for it, so a
    pop in _commit covers it. This pins that the pop does not depend on the pin-override branch
    (which a one-turn switch skips)."""
    session = {"_metadata_mirror": {"model": "stale"}, "history_lock": threading.Lock()}
    _patch_commit_helpers(monkeypatch, lambda sid, s: None)
    agent = SimpleNamespace(switch_model=lambda **kw: None)
    server._commit_agent_switch("sid", session, agent, _result(), "old", {"snap": True})
    assert "model" not in session["_metadata_mirror"]


def test_pin_branch_invalidates_the_mirror_for_agentless_sessions():
    """agy R5-3: an isolated (agent=None) session's switch never reaches _commit_agent_switch,
    and S8 removed the pin-branch pop - so the stale host model survived the switch again. The
    shared invalidation helper must exist and both sites must use it; this pins the helper."""
    import threading

    from tui_gateway import model_switch as ms

    session = {"_metadata_mirror": {"model": "stale", "provider": "p", "usage": {"t": 1}}}
    ms._invalidate_mirror_model(session)
    assert "model" not in session["_metadata_mirror"]
    assert "provider" not in session["_metadata_mirror"]
    assert session["_metadata_mirror"].get("usage") == {"t": 1}
    assert isinstance(session.get("history_lock"), type(threading.Lock())), (
        "the helper must take/create the history lock like every other mirror writer")
