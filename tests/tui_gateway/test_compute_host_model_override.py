"""Persistent compute host vs a parent-side model switch (agy R4-6 + R5-2): when a turn
frame carries a model_override differing from the cached host session's, the live child agent is
switched IN PLACE (exactly what _commit_agent_switch does in-process) - never torn down. agy
R5-2 proved a rebuild is destructive: close() marks the live session ended in state.db, kills
its sandboxes/browsers, bricks > max_resume_messages sessions, and can wipe unpersisted seed
history. A cleared override (None while one is set) is logged and recorded, not guessed at."""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace

from tui_gateway.compute_host import ComputeHost


def _server(sessions, scopes=None):
    """A server stub with the profile-scope context the in-place switch must bind (agy R6:
    switch_model reads config/credential pools, so an unscoped call leaks the launch profile
    or fails closed under multiplex)."""

    @contextlib.contextmanager
    def scope(session, hydrate_secrets=False):
        if scopes is not None:
            scopes.append({"hydrate_secrets": hydrate_secrets})
        yield

    return SimpleNamespace(_sessions=sessions, _session_profile_runtime_scope=scope)


def _host():
    host = ComputeHost.__new__(ComputeHost)
    host._transport = SimpleNamespace(write=lambda frame: None)
    return host


def test_override_change_switches_the_live_agent_in_place():
    switched = {}
    closed = []
    agent = SimpleNamespace(
        switch_model=lambda **kw: switched.update(kw),
        close=lambda: closed.append(True))
    session = {"model_override": {"model": "old"}, "agent": agent,
               "history_lock": threading.Lock(), "history_version": 3}
    scopes = []
    server = _server({"sid-1": session}, scopes)
    host = _host()
    out = host._ensure_server_session(server, {
        "sid": "sid-1", "history_version": 3,
        "model_override": {"model": "new", "provider": "prov", "api_key": "k",
                           "base_url": "b", "api_mode": "m"}})
    assert out is session, "the live session must be kept, never torn down (agy R5-2)"
    assert closed == [], "the live agent must not be closed - that ends the session in state.db"
    assert switched.get("new_model") == "new"
    assert switched.get("new_provider") == "prov"
    assert session["model_override"] == {"model": "new", "provider": "prov", "api_key": "k",
                                         "base_url": "b", "api_mode": "m"}
    assert scopes == [{"hydrate_secrets": True}], (
        "the in-place switch must run under the session profile scope with secrets hydrated")


def test_failed_in_place_switch_raises_instead_of_running_on_the_old_model():
    def boom(**kw):
        raise RuntimeError("switch failed")

    session = {"model_override": {"model": "old"}, "agent": SimpleNamespace(switch_model=boom),
               "history_lock": threading.Lock(), "history_version": 1}
    server = _server({"sid-1": session})
    host = _host()
    try:
        host._ensure_server_session(server, {
            "sid": "sid-1", "history_version": 1, "model_override": {"model": "new"}})
    except Exception:
        assert session["model_override"] == {"model": "old"}, (
            "a failed switch must not pretend the override was adopted")
    else:
        raise AssertionError("a failed switch must error the turn, not run it on the old model")


def test_same_override_keeps_the_cached_session_untouched():
    session = {"model_override": {"model": "same"}, "agent": SimpleNamespace(),
               "history_lock": threading.Lock(), "history_version": 1}
    server = _server({"sid-1": session})
    out = _host()._ensure_server_session(server, {
        "sid": "sid-1", "history_version": 1, "model_override": {"model": "same"}})
    assert out is session


def test_cleared_override_is_recorded_without_teardown():
    """None-while-set means the parent cleared its pin. Resolving the profile default from the
    host is a separate (recorded) problem; what must NOT happen is a teardown or a crash."""
    agent = SimpleNamespace()
    session = {"model_override": {"model": "old"}, "agent": agent,
               "history_lock": threading.Lock(), "history_version": 1}
    server = _server({"sid-1": session})
    out = _host()._ensure_server_session(server, {
        "sid": "sid-1", "history_version": 1, "model_override": None})
    assert out is session
    assert session.get("agent") is agent


def test_string_override_switches_model_and_keeps_the_current_provider():
    """agy round 6 (R5-2a): switch_model requires new_provider positionally - a bare
    new_model call raises TypeError. A string override IS just a model name (the
    session.create shorthand), so the provider stays the agent's current one."""
    switched = {}
    agent = SimpleNamespace(switch_model=lambda **kw: switched.update(kw), provider="curprov")
    session = {"model_override": {"model": "old"}, "agent": agent,
               "history_lock": threading.Lock(), "history_version": 1}
    server = _server({"sid-1": session})
    out = _host()._ensure_server_session(server, {
        "sid": "sid-1", "history_version": 1, "model_override": "bare-model-name"})
    assert out is session
    assert switched.get("new_model") == "bare-model-name"
    assert switched.get("new_provider") == "curprov"
    assert session["model_override"] == "bare-model-name"


def test_dict_override_without_provider_keeps_the_current_provider():
    """agy round 6 (R5-2c): overrides from session.create / DB restore may carry only a model.
    Endpoint/key resolution belongs to switch_model's destination resolution under the bound
    profile scope - but the provider default must be the agent's current one, not ''."""
    switched = {}
    agent = SimpleNamespace(switch_model=lambda **kw: switched.update(kw), provider="curprov")
    session = {"model_override": None, "agent": agent,
               "history_lock": threading.Lock(), "history_version": 1}
    server = _server({"sid-1": session})
    _host()._ensure_server_session(server, {
        "sid": "sid-1", "history_version": 1, "model_override": {"model": "m2"}})
    assert switched.get("new_provider") == "curprov"


def test_host_side_switch_is_not_undone_by_the_next_turn():
    """Codex HS-C3: the parent's stored override lags a host-side /model switch (the ack only
    updates the parent's mirror). The next turn re-sends the stale parent value; comparing it
    against the HOST's current override read that echo as a fresh request and switched back.
    The host must act only when the PARENT's value actually changes."""
    switched = []
    agent = SimpleNamespace(switch_model=lambda **kw: switched.append(kw), provider="p")
    session = {"model_override": {"model": "A"}, "agent": agent,
               "history_lock": threading.Lock(), "history_version": 1}
    server = _server({"sid-1": session})
    host = _host()
    # Turn 1: parent sends its current override A; host already on A - no switch, seeds last-seen.
    host._ensure_server_session(server, {"sid": "sid-1", "history_version": 1,
                                         "model_override": {"model": "A"}})
    assert switched == []
    # Host-side /model B: the slash path updates the HOST session's override directly.
    session["model_override"] = {"model": "B"}
    # Turn 2: the parent still carries A (its stored override was never updated by the ack).
    host._ensure_server_session(server, {"sid": "sid-1", "history_version": 2,
                                         "model_override": {"model": "A"}})
    assert switched == [], "the stale parent echo switched the host back (HS-C3)"
    assert session["model_override"] == {"model": "B"}, "the host-side switch must stick"
    # Turn 3: the parent GENUINELY changes to C - that must apply.
    host._ensure_server_session(server, {"sid": "sid-1", "history_version": 3,
                                         "model_override": {"model": "C"}})
    assert [k.get("new_model") for k in switched] == ["C"]
    assert session["model_override"] == {"model": "C"}


def test_first_build_seeds_parent_tracking_so_a_host_switch_sticks():
    """Codex HS-C3 round 2: a session built on the FIRST turn never seeded the last-seen parent
    value, so the fallback compared the echo against the host's current override and undid a
    host-side switch. The ensure path must seed tracking at build time; pinned from an empty
    session registry."""
    switched = []

    def fake_build(srv, frame, sid):
        srv._sessions[sid] = {
            "model_override": frame.get("model_override"),
            "agent": SimpleNamespace(switch_model=lambda **kw: switched.append(kw), provider="p"),
            "history_lock": threading.Lock(), "history_version": 1}
        return srv._sessions[sid]

    host = _host()
    host._build_server_session = fake_build
    server = _server({})
    # Turn 1 builds the session under parent override A.
    host._ensure_server_session(server, {"sid": "sid-1", "history_version": 1,
                                         "model_override": {"model": "A"}})
    session = server._sessions["sid-1"]
    # Host-side /model B before turn 2.
    session["model_override"] = {"model": "B"}
    # Turn 2 echoes the parent's stale A.
    host._ensure_server_session(server, {"sid": "sid-1", "history_version": 2,
                                         "model_override": {"model": "A"}})
    assert switched == [], "the first-build session undid the host-side switch (HS-C3 round 2)"
    assert session["model_override"] == {"model": "B"}


def test_failed_switch_keeps_the_request_pending():
    """Codex HS-C4: tracking advanced before the switch succeeded, so a failed adoption made
    the SAME request read as 'no parent change' next turn and the session silently ran on the
    old model. A repeated request must switch successfully or stay an error."""
    attempts = []

    def flaky(**kw):
        attempts.append(kw)
        if len(attempts) == 1:
            raise RuntimeError("transient switch failure")

    session = {"model_override": {"model": "A"},
               "agent": SimpleNamespace(switch_model=flaky, provider="p"),
               "history_lock": threading.Lock(), "history_version": 1,
               "_parent_model_override_seen": {"model": "A"}}
    server = _server({"sid-1": session})
    host = _host()
    try:
        host._ensure_server_session(server, {"sid": "sid-1", "history_version": 1,
                                             "model_override": {"model": "B"}})
    except Exception:
        pass
    else:
        raise AssertionError("the failed switch must error the turn")
    assert session["model_override"] == {"model": "A"}
    # The parent re-sends the SAME B frame; enforcement must still be pending.
    host._ensure_server_session(server, {"sid": "sid-1", "history_version": 2,
                                         "model_override": {"model": "B"}})
    assert len(attempts) == 2, "the repeated request was skipped - execution stayed on A"
    assert session["model_override"] == {"model": "B"}


def test_control_ack_reports_a_transportable_override_and_advances_last_seen():
    """HS-C5/HS-C7: the ack carries a credential-free JSON-serializable selection (callable
    key providers stay local), and producing it advances the host's last-seen parent value -
    so an immediate explicit re-selection genuinely differs from the adopted echo."""
    import json

    host = _host()
    session = {"model_override": {"model": "B", "provider": "p", "base_url": "u",
                                  "api_mode": "m", "api_key": lambda: "token"},
               "agent": None, "history_lock": threading.Lock(), "history": [],
               "history_version": 2, "session_key": "k",
               "_parent_model_override_seen": {"model": "A"}}
    server = SimpleNamespace(
        _mirror_slash_side_effects=lambda sid, sess, cmd: "",
        _history_to_messages=lambda rows: [],
        _session_info=lambda agent, sess: {"model": "B"})
    ack = host._control_ack(server, {"sid": "s1", "command": "/status"}, session)
    assert ack["model_override"] == {"model": "B", "provider": "p", "base_url": "u",
                                     "api_mode": "m"}
    json.dumps(ack["model_override"])
    assert session["_parent_model_override_seen"] == ack["model_override"]


def test_reselection_right_after_a_host_side_switch_applies():
    """HS-C5 full sequence: host on B after a host-side switch whose ack advanced last-seen;
    the parent re-selects A before any turn; the next frame (A) must switch the host back."""
    switched = []
    agent = SimpleNamespace(switch_model=lambda **kw: switched.append(kw), provider="p")
    session = {"model_override": {"model": "B", "provider": "p"},
               "_parent_model_override_seen": {"model": "B", "provider": "p"},
               "agent": agent, "history_lock": threading.Lock(), "history_version": 3}
    server = _server({"sid-1": session})
    _host()._ensure_server_session(server, {"sid": "sid-1", "history_version": 3,
                                            "model_override": {"model": "A", "provider": "p"}})
    assert [k.get("new_model") for k in switched] == ["A"]
    assert session["model_override"] == {"model": "A", "provider": "p"}


def test_mirror_apply_never_adopts_the_override_itself():
    """Codex HS-C6: compress/prompt acks can carry an override older than a selection the
    parent made meanwhile. The generic mirror-apply must leave the parent override alone;
    adoption lives only at the slash site behind a send-time snapshot."""
    from tui_gateway import server as srv

    session = {"model_override": {"model": "C"}, "history_lock": threading.Lock(),
               "session_key": "k", "history_version": 1}
    srv._apply_compute_host_metadata_mirror(session, {"model_override": {"model": "A"}})
    assert session["model_override"] == {"model": "C"}


def test_publish_model_override_bumps_the_selection_sequence_under_the_lock():
    """HS-C6 round 2: value equality cannot order selections. Every parent-side writer goes
    through one helper that assigns (None pops) and bumps a selection sequence under
    history_lock, returning the previous value."""
    from tui_gateway import server as srv

    session = {"model_override": {"model": "A"}, "history_lock": threading.Lock()}
    prev = srv._publish_model_override(session, {"model": "A"})  # equal value, NEW selection
    assert prev == {"model": "A"}
    assert session["_model_override_seq"] == 1
    srv._publish_model_override(session, None)
    assert "model_override" not in session
    assert session["_model_override_seq"] == 2


def test_adoption_is_guarded_by_the_selection_sequence_not_by_value():
    """HS-C6 round 2: an equal-value re-selection made while /model ran is NEWER intent; the
    ack must not overwrite it. Adoption applies only when no selection was published since
    the control was sent, and adoption itself is a publication."""
    from tui_gateway import server as srv

    session = {"model_override": {"model": "A"}, "history_lock": threading.Lock()}
    r = srv._reserve_model_override_selection(session)
    srv._adopt_ack_model_override(session, {"model_override": {"model": "B"}}, sent_seq=r)
    assert session["model_override"] == {"model": "B"}
    assert session["_model_override_seq"] == r, "adoption commits the reserved order"
    r = srv._reserve_model_override_selection(session)
    srv._publish_model_override(session, {"model": "B"})  # equal value, newer selection
    srv._adopt_ack_model_override(session, {"model_override": {"model": "Z"}}, sent_seq=r)
    assert session["model_override"] == {"model": "B"}, (
        "an equal-value re-selection made during the control was overwritten by the ack")
    srv._adopt_ack_model_override(session, {"output": "x"},
                                  sent_seq=srv._reserve_model_override_selection(session))
    assert session["model_override"] == {"model": "B"}


def test_only_model_slash_acks_adopt_the_host_override(monkeypatch):
    """HS-C6 round 2: an unrelated forwarded command (/compress, /prompt) acks the host's
    CURRENT override, which may predate a parent selection the host has not received yet.
    Only a /model ack reflects a host-side selection, so only it may adopt."""
    from tui_gateway import server as srv

    monkeypatch.setattr(srv, "_send_compute_host_control",
                        lambda sid, **kw: {"model_override": {"model": "A"}, "output": ""})
    monkeypatch.setattr(srv, "_compute_host_compress_wait_seconds", lambda: 1)
    session = {"model_override": {"model": "C"}, "history_lock": threading.Lock(),
               "session_key": "k", "history_version": 1}
    status, _ = srv._compute_host_slash("sid", session, "compress", "/compress")
    assert status == "ok"
    assert session["model_override"] == {"model": "C"}, (
        "a /compress ack carrying the host's older override erased the pending selection")
    status, _ = srv._compute_host_slash("sid", session, "prompt", "/prompt x")
    assert session["model_override"] == {"model": "C"}
    status, _ = srv._compute_host_slash("sid", session, "model", "/model A")
    assert status == "ok"
    assert session["model_override"] == {"model": "A"}, "a /model ack must adopt"


def test_reset_clears_the_pin_through_the_publisher(monkeypatch):
    """Codex HS-C6 reset arm: /new (tools.configure reset) cleared model_override directly,
    so a /model ack still in flight from before the reset reinstalled the pin. The reset must
    publish the clear (sequence advances) so the delayed ack is rejected."""
    from tui_gateway import server as srv

    for name, value in {
        "_load_show_reasoning": lambda: False, "_load_tool_progress_mode": lambda: "all",
        "_set_session_context": lambda key: None, "_clear_session_context": lambda tokens: None,
        "_rebuild_session_agent": lambda sid, session, **kw: SimpleNamespace(),
        "_session_source": lambda session: None,
        "_context_cwd_is_launch_artifact": lambda session: False,
        "_session_info": lambda agent, session: {}, "_emit": lambda *a, **k: None,
        "_restart_slash_worker": lambda sid, session: None,
    }.items():
        monkeypatch.setattr(srv, name, value)
    session = {"model_override": {"model": "B"}, "_model_override_seq": 4,
               "session_key": "k", "history_lock": threading.Lock(), "history": [],
               "history_version": 1}
    sent_seq = session["_model_override_seq"]
    srv._reset_session_agent("sid", session)
    assert "model_override" not in session
    assert session["_model_override_seq"] == 5, "the reset must publish the clear"
    srv._adopt_ack_model_override(session, {"model_override": {"model": "B"}}, sent_seq=sent_seq)
    assert "model_override" not in session, (
        "a /model ack from before the reset reinstalled the cleared pin")


def test_model_slash_reserves_a_selection_order_at_send(monkeypatch):
    """Codex HS-C8: a /model control must RESERVE its selection order when sent, not merely
    read the sequence; unrelated commands only read."""
    from tui_gateway import server as srv

    seen = []
    monkeypatch.setattr(srv, "_send_compute_host_control",
                        lambda sid, **kw: seen.append(kw.get("route_name")) or
                        {"model_override": {"model": "B"}, "output": ""})
    monkeypatch.setattr(srv, "_compute_host_compress_wait_seconds", lambda: 1)
    session = {"model_override": {"model": "A"}, "_model_override_seq": 2,
               "history_lock": threading.Lock(), "session_key": "k", "history_version": 1}
    srv._compute_host_slash("sid", session, "compress", "/compress")
    assert session["_model_override_seq"] == 2, "a non-model control must not reserve"
    srv._compute_host_slash("sid", session, "model", "/model B")
    # reserved 3 at send; adopting B's ack commits exactly that order
    assert session["_model_override_seq"] == 3
    assert session["model_override"] == {"model": "B"}


def test_overlapping_model_controls_let_the_later_selection_win_in_both_ack_orders():
    """Codex HS-C8: /model B then /model C overlap; the host ends on C. Whichever ack arrives
    first, the parent must end on C."""
    from tui_gateway import server as srv

    for first, second in ((("B", 1), ("C", 2)), (("C", 2), ("B", 1))):
        session = {"model_override": {"model": "A"}, "history_lock": threading.Lock()}
        r_b = srv._reserve_model_override_selection(session)
        r_c = srv._reserve_model_override_selection(session)
        reserved = {1: r_b, 2: r_c}
        for model, which in (first, second):
            srv._adopt_ack_model_override(session, {"model_override": {"model": model}},
                                          sent_seq=reserved[which])
        assert session["model_override"] == {"model": "C"}, (
            f"ack order {first[0]} then {second[0]}: the earlier selection won")


def test_a_failed_newer_control_does_not_discard_an_earlier_successful_selection():
    """Codex round 7 (HS-C8 residual): /model B reserves, /model C reserves and FAILS
    (control.error / timeout). C committed nothing, so B's successful ack must still adopt -
    in either completion order. Reservations are not commits."""
    from tui_gateway import server as srv

    # Order 1: C fails before B's ack is released.
    session = {"model_override": {"model": "A"}, "history_lock": threading.Lock()}
    r_b = srv._reserve_model_override_selection(session)
    srv._reserve_model_override_selection(session)  # C reserves ... and fails (no adoption)
    srv._adopt_ack_model_override(session, {"model_override": {"model": "B"}}, sent_seq=r_b)
    assert session["model_override"] == {"model": "B"}, "C's failed reservation blocked B"
    # Order 2: B adopts first, then C fails.
    session = {"model_override": {"model": "A"}, "history_lock": threading.Lock()}
    r_b = srv._reserve_model_override_selection(session)
    srv._adopt_ack_model_override(session, {"model_override": {"model": "B"}}, sent_seq=r_b)
    srv._reserve_model_override_selection(session)  # C reserves and fails
    assert session["model_override"] == {"model": "B"}
    # The barriers still hold: a reset/newer publication supersedes every open reservation.
    r_d = srv._reserve_model_override_selection(session)
    srv._publish_model_override(session, None)
    srv._adopt_ack_model_override(session, {"model_override": {"model": "D"}}, sent_seq=r_d)
    assert "model_override" not in session, "a pre-reset reservation adopted after the reset"
