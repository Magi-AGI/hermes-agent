"""convoy-resume: the resume payload build must not run under the process-global resume lock.

Measured on the live system (py-spy, 2026-10-28/29): every desktop `session.resume` serializes
behind `_session_resume_lock`, which `_resume_reuse_live` holds across the whole payload build —
config/model resolution (`_fallback_session_info` → `_resolve_model` → `_load_cfg`, which waits on
the config lock), git probes, project lookups and (without ``omit_messages``) a session-DB read.
Under config-lock contention any resume queued past the desktop's 30 s client timeout loses its
send. The repair (spec: ``review-artifacts/convoy-resume/SPEC.md`` rev 4.1) keeps only the
ownership CLAIM under the lock (refusal check, reap cancel, transport attach, cols/touch), builds
the payload OUTSIDE it — exactly as ``session.activate`` already does — then RE-CHECKS the refusal
and re-samples the volatile turn fields so a turn that settled mid-build is reported settled.
`session.resume` treats a mid-build loss of the live slot as "fall through once to the non-live
dispatch", never as a false "session not found" (agy S4-1: group chats fork their history on 4007).

Temp HERMES_HOME via the autouse fixture; no network, no real agents, no model calls.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from tui_gateway import server


class _TrackedLock:
    """The real resume lock plus a main-thread-visible ``held`` flag (tests are single-threaded
    around the tracked sections)."""

    def __init__(self):
        self._inner = threading.Lock()
        self.held = False

    def __enter__(self):
        self._inner.acquire()
        self.held = True

    def __exit__(self, *args):
        self.held = False
        self._inner.release()

    def acquire(self, *args, **kwargs):
        got = self._inner.acquire(*args, **kwargs)
        if got:
            self.held = True
        return got

    def release(self):
        self.held = False
        self._inner.release()


def _live_lazy_session(monkeypatch, tmp_path, sid="split-live", key="stored-split", **extra):
    session = dict(
        transport=server._detached_ws_transport, running=False, agent=None,
        history=[{"role": "user", "content": "hi"}], display_history_prefix=[],
        history_lock=threading.Lock(), session_key=key, created_at=1_000_000.0,
        cwd=str(tmp_path), cols=80, last_active=0.0)
    session.update(extra)
    monkeypatch.setattr(server, "_sessions", {sid: session})
    monkeypatch.setattr(server, "_pending_ws_reaps", {})
    return sid, session


def _ctx(target="stored-split", **over):
    base = dict(rid=1, cols=100, omit_messages=True, defer_history=False, target=target)
    base.update(over)
    return SimpleNamespace(**base)


def test_config_git_and_project_reads_leave_the_resume_lock(monkeypatch, tmp_path):
    """T1 arm 1: zero config-lock acquisitions, git probes or project lookups while
    ``_session_resume_lock`` is held on a warm reuse resume (agy S4-5.3's strengthened form)."""
    import hermes_cli.config as config_mod

    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    tracked = _TrackedLock()
    monkeypatch.setattr(server, "_session_resume_lock", tracked)
    violations = []
    real_cfg_lock = config_mod._CONFIG_LOCK

    class GuardedConfigLock:
        def __enter__(self):
            if tracked.held:
                violations.append("config")
            real_cfg_lock.acquire()

        def __exit__(self, *args):
            real_cfg_lock.release()

        def acquire(self, *args, **kwargs):
            if tracked.held:
                violations.append("config")
            return real_cfg_lock.acquire(*args, **kwargs)

        def release(self):
            real_cfg_lock.release()

    monkeypatch.setattr(config_mod, "_CONFIG_LOCK", GuardedConfigLock())
    real_project = server._project_info_for_cwd

    def project_guard(cwd):
        if tracked.held:
            violations.append("projects")
        return real_project(cwd)

    monkeypatch.setattr(server, "_project_info_for_cwd", project_guard)
    real_branch = server.git_probe.branch

    def branch_guard(cwd):
        if tracked.held:
            violations.append("git")
        return real_branch(cwd)

    monkeypatch.setattr(server.git_probe, "branch", branch_guard)
    response = server._resume_reuse_live(_ctx(), sid, session)
    assert "error" not in response
    assert not violations, f"slow reads ran under _session_resume_lock: {violations}"


def test_an_unrelated_resume_proceeds_while_one_build_is_blocked(monkeypatch, tmp_path):
    """T1 arm 2: with resume A gated inside its payload build, the resume lock is free — a probe
    acquires it and an unrelated resume B completes end-to-end."""
    sid_a, session_a = _live_lazy_session(monkeypatch, tmp_path, sid="split-a", key="stored-a")
    session_b = dict(session_a, history_lock=threading.Lock(),
                     session_key="stored-b", transport=server._detached_ws_transport)
    server._sessions["split-b"] = session_b
    gate, entered = threading.Event(), threading.Event()
    real_info = server._fallback_session_info

    def gated_info(sess):
        if sess is session_a:
            entered.set()
            assert gate.wait(10), "test gate never released"
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", gated_info)
    result_a = {}
    worker = threading.Thread(
        target=lambda: result_a.update(resp=server._resume_reuse_live(_ctx(target="stored-a"), sid_a, session_a)))
    worker.start()
    try:
        assert entered.wait(5), "resume A never reached its payload build"
        acquired = server._session_resume_lock.acquire(timeout=5)
        assert acquired, "_session_resume_lock held while a build is blocked"
        server._session_resume_lock.release()
        response_b = server._resume_reuse_live(_ctx(target="stored-b"), "split-b", session_b)
        assert "error" not in response_b
    finally:
        gate.set()
        worker.join(10)
    assert not worker.is_alive()
    assert "error" not in result_a["resp"]


def test_a_turn_that_settles_during_the_build_is_reported_settled(monkeypatch, tmp_path):
    """agy S4-2: a frozen ``running: true`` returned after the turn's own ``message.complete``
    permanently wedges the desktop composer. Volatile fields must be re-sampled post-build."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path, running=True)
    real_info = server._fallback_session_info

    def settle_then_info(sess):
        with sess["history_lock"]:
            sess["running"] = False
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", settle_then_info)
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    response = server._resume_reuse_live(_ctx(), sid, session)
    payload = response["result"]
    assert payload["running"] is False, "a turn that settled mid-build was reported still running"
    assert payload["status"] != "streaming"


def test_live_slot_lost_mid_build_refuses_instead_of_publishing_a_dead_payload(monkeypatch, tmp_path):
    """A session closed/evicted during the unlocked build must not be answered with a payload
    describing the dead runtime — the re-check refuses (and ``session.resume`` falls through)."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    real_info = server._fallback_session_info

    def pop_then_info(sess):
        server._sessions.pop(sid, None)
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", pop_then_info)
    response = server._resume_reuse_live(_ctx(), sid, session)
    assert response.get("error", {}).get("code") == 4007


class _FakeDb:
    """The two SessionDB calls the resume dispatch needs, with a deletable row."""

    def __init__(self, row):
        self.rows = {row["id"]: row}

    def get_session(self, target):
        row = self.rows.get(target)
        return dict(row) if row else None

    def close(self):
        pass


def _wire_session_resume(monkeypatch, tmp_path, sid, session, key):
    row = {"id": key, "created_at": 1_000_000.0, "message_count": 1, "cwd": str(tmp_path)}
    db = _FakeDb(row)
    monkeypatch.setattr(server, "_profile_session_db", lambda home: (db, True))
    monkeypatch.setattr(server, "_find_live_session_by_key",
                        lambda target, home: (sid, session) if sid in server._sessions else None)
    def _sentinel(path):
        # Contract-conformant stand-in (handle_request validates SessionResumeResult): the reached
        # dispatch path is carried in ``resumed``.
        return lambda ctx: server._ok(ctx.rid, {"session_id": f"ft-{path}", "message_count": 0,
                                                "messages": [], "info": {},
                                                "resumed": f"fell-through:{path}"})

    monkeypatch.setattr(server, "_resume_cold", _sentinel("cold"))
    monkeypatch.setattr(server, "_resume_deferred", _sentinel("deferred"))
    return db


def test_session_resume_falls_through_when_the_live_slot_vanishes(monkeypatch, tmp_path):
    """agy S4-1: the live slot vanishing mid-build, with the DURABLE row intact, must land on the
    non-live dispatch — not a 4007 that every consumer reads as "session does not exist"."""
    key = "stored-fallthrough"
    sid, session = _live_lazy_session(monkeypatch, tmp_path, sid="split-ft", key=key)
    _wire_session_resume(monkeypatch, tmp_path, sid, session, key)
    real_info = server._fallback_session_info

    def pop_then_info(sess):
        server._sessions.pop(sid, None)
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", pop_then_info)
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "session.resume",
                                      "params": {"session_id": key, "omit_messages": True}})
    assert "error" not in response, f"a surviving stored session was refused: {response}"
    assert response["result"]["resumed"] in ("fell-through:cold", "fell-through:deferred")


def test_session_resume_keeps_4007_when_the_stored_row_is_gone_too(monkeypatch, tmp_path):
    """The honest arm of the fall-through: live slot AND durable row both gone → 4007 stands."""
    key = "stored-deleted"
    sid, session = _live_lazy_session(monkeypatch, tmp_path, sid="split-del", key=key)
    db = _wire_session_resume(monkeypatch, tmp_path, sid, session, key)
    real_info = server._fallback_session_info

    def delete_everything_then_info(sess):
        server._sessions.pop(sid, None)
        db.rows.clear()
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", delete_everything_then_info)
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "session.resume",
                                      "params": {"session_id": key, "omit_messages": True}})
    assert response.get("error", {}).get("code") == 4007


def test_the_claim_touches_last_active_before_the_build(monkeypatch, tmp_path):
    """Pin: ``last_active`` (idle-reap / LRU eviction ordering) advances at the CLAIM, so a long
    build cannot leave the session looking idle to the reaper mid-resume."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    seen = {}
    real_info = server._fallback_session_info

    def record_then_info(sess):
        seen["last_active_at_build"] = sess.get("last_active")
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", record_then_info)
    response = server._resume_reuse_live(_ctx(), sid, session)
    assert "error" not in response
    assert seen["last_active_at_build"] > 0.0


def test_a_lazy_child_run_overlay_survives_the_volatile_refresh(monkeypatch, tmp_path):
    """Pin (agy S4-4.1): a lazy watch window whose child run is active reports
    ``running/streaming`` even though the session dict itself says idle."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_child_run_active", lambda target: target == "stored-split")
    response = server._resume_reuse_live(_ctx(), sid, session)
    payload = response["result"]
    assert payload["running"] is True
    assert payload["status"] == "streaming"


def test_a_claim_loser_retries_when_the_winners_slot_vanishes(monkeypatch, tmp_path):
    """agy C1-1: `_Resume.claim` (the lazy/deferred/cold loser path) must not surface the false
    4007 either — the winner's slot vanishing means the slot is free, so the claim retries once."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    claims = []

    def fake_claim_or_reuse(sid_, target, record, extra):
        claims.append(1)
        if len(claims) == 1:
            return (sid, session)  # lost to a concurrent winner
        return None  # retry wins: register our own record

    monkeypatch.setattr(server, "_claim_or_reuse_live", fake_claim_or_reuse)
    real_info = server._fallback_session_info

    def pop_then_info(sess):
        server._sessions.pop(sid, None)
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", pop_then_info)
    ctx = server._Resume(1, {}, "stored-split")
    ctx.db = _FakeDb({"id": "stored-split", "created_at": 1.0, "message_count": 0})  # row survives
    result = ctx.claim("fresh-sid", {})
    assert result is None, f"the claim loser surfaced a false refusal: {result}"
    assert len(claims) == 2


def test_session_resume_surfaces_4009_without_fallthrough(monkeypatch, tmp_path):
    """Pin: only the live-slot-gone 4007 falls through; a client-gone 4009 settling is refused
    as today (falling through would race the disconnect interrupt)."""
    key = "stored-4009"
    sid, session = _live_lazy_session(monkeypatch, tmp_path, sid="split-4009", key=key)
    _wire_session_resume(monkeypatch, tmp_path, sid, session, key)
    real_info = server._fallback_session_info

    def interrupt_then_info(sess):
        sess["_client_gone_interrupt_requested"] = True
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", interrupt_then_info)
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "session.resume",
                                      "params": {"session_id": key, "omit_messages": True}})
    assert response.get("error", {}).get("code") == 4009


def test_a_stdio_caller_does_not_refire_the_recovery_rebind(monkeypatch, tmp_path):
    """agy C1-3: the RE-CHECK fanout-recovery branch is for LIVE peers pruned mid-build. A
    stdio/in-process caller is never 'contained' beside a live WS peer, so without the gate it
    re-fires on every such resume (re-stamping viewers and re-locking under the resume lock)."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    session["transport"] = SimpleNamespace(_closed=False)  # a live WS-ish peer already attached
    rebinds = []
    real_rebind = server._rebind_live_transport

    def counting_rebind(sid_, session_, transport):
        rebinds.append(transport)
        return real_rebind(sid_, session_, transport)

    monkeypatch.setattr(server, "_rebind_live_transport", counting_rebind)
    response = server._resume_reuse_live(_ctx(), sid, session)
    assert "error" not in response
    assert len(rebinds) == 1, "the stdio fallback re-fired the fanout-recovery rebind"


def test_message_count_refreshes_when_a_turn_settles_mid_build(monkeypatch, tmp_path):
    """agy C1-2.1 (omit_messages arm): a row committed mid-build must be counted — the client
    sees running=False and must not be told the pre-settle count."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path, running=True)
    real_info = server._fallback_session_info

    def settle_commit_then_info(sess):
        with sess["history_lock"]:
            sess["history"].append({"role": "assistant", "content": "done"})
            sess["running"] = False
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", settle_commit_then_info)
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    response = server._resume_reuse_live(_ctx(), sid, session)
    payload = response["result"]
    assert payload["running"] is False
    assert payload["message_count"] == 2, "the settled turn's committed row was not counted"


def test_the_build_reruns_when_running_transitions_and_messages_are_included(monkeypatch, tmp_path):
    """agy C1-2.1: with ``omit_messages=False`` a running transition mid-build means the messages
    snapshot predates the settle (the completed assistant reply would vanish) — rebuild once."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path, running=True)
    calls = []

    def fake_payload(sid_, session_, *, cols=None, touch=False, transport=None, omit_messages=False):
        calls.append(1)
        built_running = bool(session_.get("running"))
        if len(calls) == 1:
            with session_["history_lock"]:
                session_["history"].append({"role": "assistant", "content": "done"})
                session_["running"] = False
        return {"session_id": sid_, "message_count": len(session_["history"]),
                "messages": [] if omit_messages else list(session_["history"]),
                "messages_omitted": omit_messages, "info": {}, "running": built_running,
                "turn_started_at": None, "status": "working" if built_running else "idle",
                "session_key": session_.get("session_key"), "started_at": 1.0}

    monkeypatch.setattr(server, "_live_session_payload", fake_payload)
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    response = server._resume_reuse_live(_ctx(omit_messages=False), sid, session)
    payload = response["result"]
    assert len(calls) == 2, "the stale pre-settle messages snapshot was returned without a rebuild"
    assert payload["running"] is False
    assert any(m.get("content") == "done" for m in payload["messages"])


def test_a_queued_drain_aba_transition_still_rebuilds(monkeypatch, tmp_path):
    """agy C2-1: running True→False→True (a queued prompt drains the instant a turn settles) left
    the 1-bit watermark blind — the settled turn vanished from a messages-included payload."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path, running=True)
    calls = []

    def fake_payload(sid_, session_, *, cols=None, touch=False, transport=None, omit_messages=False):
        calls.append(1)
        built_running = bool(session_.get("running"))
        if len(calls) == 1:
            with session_["history_lock"]:
                session_["history"].append({"role": "assistant", "content": "done"})
                session_["running"] = True  # settled AND immediately restarted by the queued drain
        return {"session_id": sid_, "message_count": len(session_["history"]),
                "messages": [] if omit_messages else list(session_["history"]),
                "messages_omitted": omit_messages, "info": {}, "running": built_running,
                "turn_started_at": None, "status": "working" if built_running else "idle",
                "session_key": session_.get("session_key"), "started_at": 1.0}

    monkeypatch.setattr(server, "_live_session_payload", fake_payload)
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    response = server._resume_reuse_live(_ctx(omit_messages=False), sid, session)
    assert len(calls) == 2, "an ABA running transition skipped the rebuild"
    assert any(m.get("content") == "done" for m in response["result"]["messages"])


def test_an_exhausted_rebuild_returns_the_builds_own_coherent_snapshot(monkeypatch, tmp_path):
    """agy C2-1 pass-2 arm: when BOTH passes transition on a messages-included resume, the
    volatile overwrite must not pair fresh running/inflight with the pass-2 pre-transition
    messages — base returned one coherent snapshot, and so must the split."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path, running=True)
    calls = []

    def fake_payload(sid_, session_, *, cols=None, touch=False, transport=None, omit_messages=False):
        calls.append(1)
        built_running = bool(session_.get("running"))
        with session_["history_lock"]:
            session_["history"].append({"role": "assistant", "content": f"t{len(calls)}"})
            session_["running"] = not built_running  # transition on EVERY pass
        return {"session_id": sid_, "message_count": len(session_["history"]) - 1,
                "messages": [] if omit_messages else list(session_["history"])[:-1],
                "messages_omitted": omit_messages, "info": {}, "running": built_running,
                "turn_started_at": None, "status": "working" if built_running else "idle",
                "session_key": session_.get("session_key"), "started_at": 1.0}

    monkeypatch.setattr(server, "_live_session_payload", fake_payload)
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    response = server._resume_reuse_live(_ctx(omit_messages=False), sid, session)
    payload = response["result"]
    assert len(calls) == 2
    assert payload["running"] is False, (
        "the exhausted-rebuild payload no longer matches its own build-time snapshot")


def test_defer_history_reports_the_live_count_once_hydration_finished(monkeypatch, tmp_path):
    """agy C2-3: Desktop sends defer_history+omit_messages together; a stale
    ``resume_message_count`` from the original cold open must not clobber the refreshed live
    count once hydration is over."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    session["resume_hydrating"] = False
    session["resume_message_count"] = 5  # the cold-open count, never updated by later turns
    with session["history_lock"]:
        session["history"] = [{"role": "user", "content": str(i)} for i in range(7)]
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    response = server._resume_reuse_live(_ctx(omit_messages=True, defer_history=True), sid, session)
    payload = response["result"]
    assert payload["hydrating"] is False
    assert payload["message_count"] == 7, "a finished hydration's stale count clobbered the live count"


def test_a_claim_retry_never_resurrects_a_deleted_session(monkeypatch, tmp_path):
    """agy C2-2: the claim retry must re-verify the durable row — a session deleted during the
    loser's build must surface the refusal, not re-register a fresh record."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    claims = []
    monkeypatch.setattr(server, "_claim_or_reuse_live",
                        lambda *a: (claims.append(1), (sid, session) if len(claims) == 1 else None)[1])
    real_info = server._fallback_session_info

    def pop_then_info(sess):
        server._sessions.pop(sid, None)
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", pop_then_info)
    ctx = server._Resume(1, {}, "stored-split")
    ctx.db = _FakeDb({"id": "someone-else", "created_at": 1.0, "message_count": 0})  # target row GONE
    result = ctx.claim("fresh-sid", {})
    assert result is not None and result.get("error", {}).get("code") == 4007, (
        f"a deleted session was resurrected by the claim retry: {result}")
    assert len(claims) == 1


def test_a_claim_retry_defers_to_the_tip_when_compression_rotated_the_target(monkeypatch, tmp_path):
    """agy C2-2: the loser's record was built for the pre-compression target; if the tip rotated
    during the build, the claim surfaces the refusal so the caller re-resolves the lineage —
    retrying would register the stale record on the retired parent."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    claims = []
    monkeypatch.setattr(server, "_claim_or_reuse_live",
                        lambda *a: (claims.append(1), (sid, session) if len(claims) == 1 else None)[1])
    real_info = server._fallback_session_info

    def pop_then_info(sess):
        server._sessions.pop(sid, None)
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", pop_then_info)

    def rotate_tip(ctx_):
        ctx_.target = "stored-split-child"

    monkeypatch.setattr(server, "_resume_follow_tip", rotate_tip)
    ctx = server._Resume(1, {}, "stored-split")
    ctx.db = _FakeDb({"id": "stored-split", "created_at": 1.0, "message_count": 0})
    result = ctx.claim("fresh-sid", {})
    assert result is not None and result.get("error", {}).get("code") == 4007
    assert ctx.target == "stored-split-child", "the rotated tip must be left for the caller's re-resolve"
    assert len(claims) == 1


def test_session_resume_retries_a_dispatch_level_false_4007(monkeypatch, tmp_path):
    """agy C1-1/C2-2: a 4007 bubbling out of the lazy/deferred/cold CLAIM path (the loser's winner
    vanished) must also re-resolve and retry once — not just the fast-path one."""
    key = "stored-dispatch-ft"
    sid, session = _live_lazy_session(monkeypatch, tmp_path, sid="split-dft", key=key)
    db = _wire_session_resume(monkeypatch, tmp_path, sid, session, key)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda *a: None)  # no fast path
    cold_calls = []

    def flaky_cold(ctx):
        cold_calls.append(1)
        if len(cold_calls) == 1:
            return server._err(ctx.rid, 4007, "session no longer live; retry resume")
        return server._ok(ctx.rid, {"session_id": "ft-cold", "message_count": 0, "messages": [],
                                    "info": {}, "resumed": "fell-through:cold"})

    monkeypatch.setattr(server, "_resume_cold", flaky_cold)
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "session.resume",
                                      "params": {"session_id": key, "omit_messages": True}})
    assert "error" not in response, f"a dispatch-level false 4007 escaped: {response}"
    assert len(cold_calls) == 2
    assert db.rows, "premise: the durable row survived throughout"


def test_claim_adopts_the_refetched_row_before_resolving_the_tip(monkeypatch, tmp_path):
    """sweep S1-a (agy C3-2): the retry gate re-fetches the durable row — follow-tip must resolve
    against THAT row, not the stale one the claim started with."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    claims = []
    monkeypatch.setattr(server, "_claim_or_reuse_live",
                        lambda *a: (claims.append(1), (sid, session) if len(claims) == 1 else None)[1])
    real_info = server._fallback_session_info

    def pop_then_info(sess):
        server._sessions.pop(sid, None)
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", pop_then_info)
    fresh_row = {"id": "stored-split", "created_at": 1.0, "message_count": 0, "title": "fresh"}
    seen = {}
    monkeypatch.setattr(server, "_resume_follow_tip", lambda ctx_: seen.update(found=ctx_.found))
    ctx = server._Resume(1, {}, "stored-split")
    ctx.found = {"id": "stored-split", "title": "stale"}
    ctx.db = _FakeDb(fresh_row)
    assert ctx.claim("fresh-sid", {}) is None
    assert seen["found"] == fresh_row, f"follow-tip resolved against a stale row: {seen['found']}"


def test_an_exhausted_rebuild_keeps_the_builds_registry_cards_too(monkeypatch, tmp_path):
    """sweep S1-b (agy C3-1): the exhausted-rebuild arm returns ONE coherent snapshot wholesale —
    refreshing the registry cards while freezing the volatile fields was an asymmetry."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path, running=True)
    monkeypatch.setattr(server, "_pending_approval_request_payload", lambda key: {"card": "fresh"})
    calls = []

    def fake_payload(sid_, session_, *, cols=None, touch=False, transport=None, omit_messages=False):
        calls.append(1)
        built_running = bool(session_.get("running"))
        with session_["history_lock"]:
            session_["history"].append({"role": "assistant", "content": f"t{len(calls)}"})
            session_["running"] = not built_running  # transition on EVERY pass
        return {"session_id": sid_, "message_count": len(session_["history"]) - 1,
                "messages": [] if omit_messages else list(session_["history"])[:-1],
                "messages_omitted": omit_messages, "info": {}, "running": built_running,
                "turn_started_at": None, "status": "working" if built_running else "idle",
                "session_key": session_.get("session_key"), "started_at": 1.0,
                "pending_approval": {"card": "built"}}

    monkeypatch.setattr(server, "_live_session_payload", fake_payload)
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    response = server._resume_reuse_live(_ctx(omit_messages=False), sid, session)
    payload = response["result"]
    assert len(calls) == 2
    assert payload.get("pending_approval") == {"card": "built"}, (
        "the exhausted snapshot's registry cards were replaced with a post-transition refresh")


def test_a_pruned_live_peer_is_reattached_at_recheck(monkeypatch, tmp_path):
    """Pin (agy C2-5/C3-5): the POSITIVE fanout-prune arm — a live caller peer dropped from the
    session transport mid-build re-attaches at RE-CHECK."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)

    class _LivePeer:  # hashable (viewers are keyed by transport), not dead, not stdio
        _closed = False

    caller = _LivePeer()
    monkeypatch.setattr(server, "current_transport", lambda: caller)
    real_info = server._fallback_session_info

    def prune_then_info(sess):
        with sess["history_lock"]:
            sess["transport"] = _LivePeer()  # the caller was pruned mid-build
        return real_info(sess)

    monkeypatch.setattr(server, "_fallback_session_info", prune_then_info)
    response = server._resume_reuse_live(_ctx(), sid, session)
    assert "error" not in response
    assert server._session_transport_contains(session, caller), "the pruned live peer was not re-attached"


def test_info_running_fields_refresh_with_the_volatile_sample(monkeypatch, tmp_path):
    """Pin (agy C3-5): when the payload's info carries running/turn_started_at, the volatile
    refresh updates them alongside the top-level fields."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path, running=True)

    def fake_payload(sid_, session_, *, cols=None, touch=False, transport=None, omit_messages=False):
        with session_["history_lock"]:
            session_["running"] = False
        return {"session_id": sid_, "message_count": 0, "messages": [], "messages_omitted": True,
                "info": {"running": True, "turn_started_at": 123.0}, "running": True,
                "turn_started_at": 123.0, "status": "working",
                "session_key": session_.get("session_key"), "started_at": 1.0}

    monkeypatch.setattr(server, "_live_session_payload", fake_payload)
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    payload = server._resume_reuse_live(_ctx(), sid, session)["result"]
    assert payload["running"] is False
    assert payload["info"]["running"] is False


def test_a_live_agents_info_build_also_leaves_the_resume_lock(monkeypatch, tmp_path):
    """Pin (agy C2-5.1): the live-agent seam — `_session_info` — runs outside the resume lock."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path, agent=SimpleNamespace())
    tracked = _TrackedLock()
    monkeypatch.setattr(server, "_session_resume_lock", tracked)
    violations = []

    def guarded_session_info(agent, sess=None):
        if tracked.held:
            violations.append("session_info")
        return {"model": "m"}

    monkeypatch.setattr(server, "_session_info", guarded_session_info)
    response = server._resume_reuse_live(_ctx(), sid, session)
    assert "error" not in response
    assert not violations


def test_the_eager_losers_agent_closes_outside_the_resume_lock(monkeypatch, tmp_path):
    """sweep S2-b (agy S3-6.1 family): when the eager double-check loses to a live session, the
    discarded agent's close() — provider/HTTP teardown — must not run under the resume lock."""
    from contextlib import nullcontext
    from unittest.mock import Mock

    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    tracked = _TrackedLock()
    monkeypatch.setattr(server, "_session_resume_lock", tracked)
    violations = []
    agent = Mock()
    agent.close = lambda: violations.append("close") if tracked.held else None
    monkeypatch.setattr(server, "_profile_build_scope", lambda *a: nullcontext())
    monkeypatch.setattr(server, "_make_agent_in_context", lambda *a, **kw: agent)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda *a: (sid, session))
    ctx = SimpleNamespace(rid=1, owns_db=False, db=None, cols=80, omit_messages=True,
                          defer_history=False, target="stored-split", profile=None,
                          profile_home=None, profile_resume_cwd=None, found={},
                          messages=lambda history: [], mint=lambda: ("fresh-sid", "tui", "."),
                          restore=lambda: ([], [], []), display_prefix=lambda: [])
    response = server._resume_eager(ctx)
    assert "error" not in response
    assert not violations, "the eager loser closed its agent while holding _session_resume_lock"


def test_split_and_inline_payloads_match_on_a_quiet_session(monkeypatch, tmp_path):
    """Equivalence: on a quiet session the split path's payload equals the fully-locked inline
    variant's (`_resume_reuse_live_locked`, the eager double-check callee) field for field."""
    sid, session = _live_lazy_session(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_child_run_active", lambda target: False)
    split = server._resume_reuse_live(_ctx(), sid, session)["result"]
    sid2, session2 = _live_lazy_session(monkeypatch, tmp_path)
    with server._session_resume_lock:
        inline = server._resume_reuse_live_locked(_ctx(), sid2, session2)["result"]
    assert split == inline
