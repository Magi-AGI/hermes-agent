"""Compute-host recovery (deploy gate G1): the crash-loop latch recovers, and a session whose live
state was in a host that is gone continues from the session store, never from the parent's copy.

2026-09-15: the host crash-looped, the supervisor latched ``_stopped_respawning`` and nothing ever
cleared it, so every turn-isolated session in the profile stayed dead until a backend restart.
The parent only mirrors a host session's metadata, so its ``history`` stops at the moment the
session first went to a host; a respawned host (or the in-process fallback) that rebuilt from it
silently dropped every turn the lost host had run.
"""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import types

import pytest

from hermes_state import SessionDB
from tui_gateway import host_supervisor, server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.host_supervisor import HostSupervisor

# A host that says hello and then crashes.
_CRASHING_HOST = (
    "import json, sys, time\n"
    "print(json.dumps({'type': 'hello', 'boot_id': 'b'}), flush=True)\n"
    "time.sleep(0.2)\n"
    "sys.exit(1)\n")


def _wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_crash_loop_latch_recovers_after_its_cooldown(tmp_path, monkeypatch):
    sup = HostSupervisor(
        registry_path=tmp_path / "host.json", argv=[sys.executable, "-c", _CRASHING_HOST],
        cwd=tmp_path, respawn_max=2, expected_build_sha="test", expected_hermes_home="",
        autostart=False)
    try:
        sup.start()
        assert _wait_until(lambda: sup._stopped_respawning), "the crash loop never latched"
        with pytest.raises(RuntimeError, match="starts it again"):
            sup.start()

        monkeypatch.setattr(host_supervisor, "_LATCH_COOLDOWN_SECS", 0.0)
        sup.start()  # the paused host is spawned again, not refused forever
        assert sup.hello.get("type") == "hello"
    finally:
        sup.shutdown()


def _store_with_transcript(tmp_path, monkeypatch, key: str) -> list[dict]:
    db = SessionDB(tmp_path / "state.db")
    db.create_session(key, source="desktop")
    for role, text in (("user", "first"), ("assistant", "one"), ("user", "second"), ("assistant", "two")):
        db.append_message(key, role, text)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    return [{"role": m["role"], "content": m["content"]}
            for m in db.get_messages_as_conversation(key, repair_alternation=True)]


def _host_session(key: str, stale_history: list) -> dict:
    """The parent's record of a session whose turns ran in a (now lost) compute host."""
    return dict(agent=None, agent_ready=threading.Event(), session_key=key, history=list(stale_history),
                history_version=3, history_lock=threading.Lock(), running=True, attached_images=[], cols=80,
                source="desktop", inflight_turn=None, created_at=time.time(), last_active=time.time(),
                _compute_host_active=True)


def _content(history: list) -> list[tuple]:
    return [(m.get("role"), m.get("content")) for m in history]


def test_a_lost_hosts_session_continues_from_the_store(tmp_path, monkeypatch):
    key = "host-key"
    durable = _store_with_transcript(tmp_path, monkeypatch, key)
    stale = durable[:2]  # all the parent saw before the session moved to the host

    # A respawned host builds the session from the frame the parent sends.
    session = _host_session(key, stale)
    frame = server._compute_host_turn_frame("rid", "sid-r", session, "third")
    monkeypatch.setattr(server, "_make_agent", lambda *a, **k: object())

    def _init_session(sid, key, agent, history, **_kw):
        server._sessions[sid] = {"history": history, "history_lock": threading.Lock(), "history_version": 0}

    monkeypatch.setattr(server, "_init_session", _init_session)
    host = ComputeHost(heartbeat_secs=0)
    try:
        built = host._build_server_session(server, frame, "sid-r")
    finally:
        host.close()
        server._sessions.pop("sid-r", None)
    assert _content(built["history"]) == _content(durable)

    # The in-process fallback reloads it too, and then owns the session.
    session = _host_session(key, stale)
    assert server._adopt_host_session_inline("rid", session, "worker paused") is None
    assert _content(session["history"]) == _content(durable)
    assert session["_compute_host_active"] is False


def test_a_lost_control_pipe_is_not_reported_as_a_dead_gateway(tmp_path):
    """The 2026-09-15 exits said 'stdin EOF ... parent gone' while the gateway was alive and busy, and
    five of them latched the host off. A real host whose pipe ends with its gateway alive reports
    that, and the supervisor classifies it as a lost pipe, not an orphan exit."""
    sup = HostSupervisor(registry_path=tmp_path / "host.json", respawn_max=0, expected_build_sha="unknown",
                         autostart=False)
    failures: list[dict] = []
    try:
        sup.start()
        with sup._lock:
            sup._pending_turns["r1"] = ("s1", failures.append)
        sup._proc.stdin.close()  # our end of the control pipe goes away; this process stays alive
        assert _wait_until(lambda: failures), "the host never exited"
        assert failures[0]["reason"] == "host_stdin_lost"
        assert any("alive" in line and "code=19" in line for line in sup._stderr_tail), sup._stderr_tail
    finally:
        sup.shutdown()


_FROZEN_HOST = (
    "import json, time\n"
    "print(json.dumps({'type': 'hello', 'boot_id': 'b'}), flush=True)\n"
    "time.sleep(600)\n")  # never heart-beats again: the 2026-09-14 whole-process freeze


def test_a_frozen_host_is_recovered_by_default(tmp_path):
    """A host whose every thread froze (heartbeat included) kept its session dead until someone killed
    the right PIDs by hand. The watchdog that recovers it is on by default now; it fails the turn."""
    cfg = server._load_dashboard_process_isolation_config({"dashboard": {}})
    assert cfg["compute_host_heartbeat_timeout_secs"] > 0
    sup = HostSupervisor(
        registry_path=tmp_path / "host.json", argv=[sys.executable, "-c", _FROZEN_HOST], cwd=tmp_path,
        heartbeat_secs=1, heartbeat_timeout_secs=3, respawn_max=0, expected_build_sha="test",
        expected_hermes_home="", autostart=False)
    failures: list[dict] = []
    try:
        sup.start()
        frozen = sup._proc
        with sup._lock:
            sup._pending_turns["r1"] = ("s1", failures.append)
        assert _wait_until(lambda: failures, timeout=30), "the frozen host was never recovered"
        assert failures[0]["type"] == "turn.error"
        assert frozen.poll() is not None
    finally:
        sup.shutdown()


def test_a_turn_the_host_cannot_start_settles_as_a_turn_error():
    """A raising turn.start handler answered with a bare ``error``, which only control waiters
    consume: the parent's pending turn never settled and the session stayed busy for good."""
    import io
    import json

    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    host._executor.shutdown(wait=False)  # submit now raises, as it does once the host is closing
    try:
        host.handle_frame({"type": "turn.start", "sid": "s1", "request_id": "r1", "text": "hi"})
    finally:
        host.close()
    frames = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [(f["type"], f["sid"], f["request_id"]) for f in frames] == [("turn.error", "s1", "r1")]


def test_a_start_that_fails_after_the_spawn_leaves_nothing_running(tmp_path, monkeypatch):
    """The registry write sat outside the cleanup: a failure there raised to the caller while the host
    (and its job) stayed alive, retained by the supervisor's own threads."""
    sup = HostSupervisor(
        registry_path=tmp_path / "host.json", argv=[sys.executable, "-c", _FROZEN_HOST], cwd=tmp_path,
        heartbeat_secs=1, respawn_max=3, expected_build_sha="test", expected_hermes_home="", autostart=False)
    spawned = []

    def _fail_publish():
        spawned.append(sup._proc)
        raise OSError("registry write failed")

    monkeypatch.setattr(sup, "_persist_registry", _fail_publish)
    try:
        with pytest.raises(OSError, match="registry write failed"):
            sup.start()
        assert _wait_until(lambda: spawned[0].poll() is not None, timeout=15), "the failed host kept running"
        time.sleep(1.0)
        assert sup._proc is None and not sup.is_running(), "a failed start was respawned as a crash"
    finally:
        sup.shutdown()


class _ExitedProc:
    def __init__(self, code):
        self.code, self.pid = code, 0

    def wait(self):
        return self.code

    def poll(self):
        return self.code


def test_a_replaced_hosts_exit_settles_only_its_own_turns():
    """A host's exit is handled after a replacement may already run: it cleared nothing and returned
    early (stranding its own turns), or failed every pending turn, the replacement's included."""
    sup = HostSupervisor(autostart=False, rpc_sink=lambda _o: None, expected_build_sha="test")
    old, new = _ExitedProc(1), _ExitedProc(None)
    old_done: list[dict] = []
    new_done: list[dict] = []
    with sup._lock:
        sup._proc = new  # start() already replaced the dead host
        sup._pending_turns = {"r-old": ("s1", old_done.append), "r-new": ("s2", new_done.append)}
        sup._request_hosts = {"r-old": old, "r-new": new}
    sup._wait_for_exit(old)
    assert [(f["type"], f["request_id"]) for f in old_done] == [("turn.error", "r-old")]
    assert new_done == [] and "r-new" in sup._pending_turns
    assert sup._proc is new and not sup._stopped_respawning


def test_the_watchdog_spares_a_host_whose_heartbeat_just_arrived(monkeypatch):
    sup = HostSupervisor(heartbeat_secs=1, heartbeat_timeout_secs=5, autostart=False,
                         rpc_sink=lambda _o: None, expected_build_sha="test")
    sup._closing = True  # no background watchdog loop: drive one check by hand

    class _Alive:
        pid = 0
        polls = 0

        def poll(self):
            self.polls += 1
            if self.polls == 2:  # a heartbeat lands between the first look and the kill
                sup._last_hb_monotonic = time.monotonic()
            return None

    killed = []
    monkeypatch.setattr(sup, "_kill_host_tree", killed.append)
    monkeypatch.setattr(sup, "_kill_process_tree", killed.append)
    with sup._lock:
        sup._proc = _Alive()
        sup._last_hb_monotonic = time.monotonic() - 100.0
    sup._watchdog_check_once(5.0)
    assert killed == []


def test_the_watchdog_does_not_count_a_suspend_as_host_silence(monkeypatch):
    """Resuming from sleep made every host look silent for the whole suspend: killed on wake."""
    sup = HostSupervisor(heartbeat_secs=1, heartbeat_timeout_secs=5, autostart=False,
                         rpc_sink=lambda _o: None, expected_build_sha="test")
    sup._closing = True
    clock = {"now": 1000.0, "sleeps": 0}

    def _sleep(seconds):
        clock["sleeps"] += 1
        clock["now"] += seconds + (3600.0 if clock["sleeps"] == 1 else 0.0)  # the first tick spans a suspend
        if clock["sleeps"] >= 2:
            sup._closing = True

    monkeypatch.setattr(host_supervisor, "time", types.SimpleNamespace(
        monotonic=lambda: clock["now"], sleep=_sleep))

    class _Alive:
        pid = 0

        def poll(self):
            return None

    killed = []
    monkeypatch.setattr(sup, "_kill_host_tree", killed.append)
    monkeypatch.setattr(sup, "_kill_process_tree", killed.append)
    with sup._lock:
        sup._proc = _Alive()
        sup._last_hb_monotonic = clock["now"]
    sup._closing = False
    sup._heartbeat_watchdog_loop()
    assert killed == []


def test_switching_isolation_off_continues_a_host_session_from_the_store(tmp_path, monkeypatch):
    """With dashboard.turn_isolation turned off, a session whose turns ran in a compute host routed
    straight inline and the in-process agent continued from this process's stale copy."""
    key = "host-key"
    durable = _store_with_transcript(tmp_path, monkeypatch, key)
    sid = "iso-off"
    session = _host_session(key, durable[:2])
    session.update(running=False, agent_ready=threading.Event(), transport=None, queued_prompt=None)
    started_with: list[list] = []
    monkeypatch.setattr(server, "_sessions", {sid: session})
    monkeypatch.setattr(server, "current_transport", lambda: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a: None)
    monkeypatch.setattr(server, "_legacy_group_fence_error", lambda *a: None)
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda *a: {"turn_isolation": False})
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: False)
    monkeypatch.setattr(server, "_start_agent_build",
                        lambda _sid, s: started_with.append(list(s["history"])))
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *a, **k: None)

    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "prompt.submit",
                                      "params": {"session_id": sid, "text": "third"}})

    assert response.get("result", {}).get("status") == "streaming", response
    assert [_content(h) for h in started_with] == [_content(durable)]
    assert session["_compute_host_active"] is False



class _FakeHost:
    """A host process double: never a real process, so no signal or handle leaves the test."""

    def __init__(self, pid, code=None):
        self.pid, self.code = pid, code
        self.stdin = io.StringIO()

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def terminate(self):
        self.code = 1

    kill = terminate


def _quiet_supervisor(tmp_path, **kwargs):
    return HostSupervisor(registry_path=tmp_path / "host.json", expected_build_sha="test", expected_hermes_home="",
                          heartbeat_timeout_secs=0, autostart=False, **kwargs)


def test_a_host_exit_settles_a_control_still_waiting_on_it(tmp_path, monkeypatch):
    """Exit claimed turns and late handlers but not a waiter still blocked on the host: at its deadline
    it turned into a late handler for a host whose exit was already processed, and waited forever."""
    sup = _quiet_supervisor(tmp_path)
    old = _FakeHost(101)
    sup._proc = old
    monkeypatch.setattr(sup, "_maybe_respawn_after_crash", lambda: None)
    send = sup._send_frame

    def send_then_die(frame):
        send(frame)
        old.code = 1
        sup._wait_for_exit(old)

    monkeypatch.setattr(sup, "_send_frame", send_then_die)
    late: list = []
    reply = sup.control("s", route_name="session.compress", timeout=0.05, on_late_ack=late.append)
    assert reply["type"] == "control.error" and reply["reason"] == "crash"
    assert not sup._late_control_handlers and late == []


def test_a_broken_send_completes_its_turn_once(tmp_path, monkeypatch):
    """The exit could claim the turn between the failed write and the send's own cleanup: the turn
    completed as a crash AND as send_failed (and the dispatch error then also started it inline)."""
    sup = _quiet_supervisor(tmp_path)
    proc = _FakeHost(606)
    sup._proc = proc
    monkeypatch.setattr(sup, "_maybe_respawn_after_crash", lambda: None)
    send_failed, exit_done = threading.Event(), threading.Event()
    lock = threading.RLock()
    sender = threading.get_ident()

    class _PausingLock:  # the sender pauses right after releasing the lock on its failed write
        depth = 0

        def __enter__(self):
            lock.acquire()
            if threading.get_ident() == sender:
                self.depth += 1

        def __exit__(self, exc_type, *_):
            is_sender = threading.get_ident() == sender
            if is_sender:
                self.depth -= 1
            lock.release()
            if is_sender and exc_type is BrokenPipeError and self.depth == 0:
                send_failed.set()
                assert exit_done.wait(10)

    sup._lock = _PausingLock()

    class _Broken:
        def write(self, _data):
            proc.code = 1
            raise BrokenPipeError("host died during write")

    proc.stdin = _Broken()
    completions: list = []

    def _exit_waiter():
        try:
            assert send_failed.wait(10)
            sup._wait_for_exit(proc)
        finally:
            exit_done.set()

    waiter = threading.Thread(target=_exit_waiter)
    waiter.start()
    try:
        sup.submit_turn({"sid": "s", "request_id": "r"}, on_complete=completions.append)
    except BrokenPipeError:
        pass
    finally:
        send_failed.set()
        waiter.join(10)
    assert [c["reason"] for c in completions] == ["crash"], completions


def test_an_old_exit_never_deletes_its_replacements_registry(tmp_path, monkeypatch):
    sup = _quiet_supervisor(tmp_path, respawn_max=0)
    old, new = _FakeHost(404, 1), _FakeHost(505)
    sup._proc = old
    reached, resume = threading.Event(), threading.Event()
    remove = sup._remove_registry

    def paused_remove():
        reached.set()
        assert resume.wait(10)
        remove()

    def spawn_new(**_kw):
        sup._proc = new
        sup._persist_registry()

    monkeypatch.setattr(sup, "_remove_registry", paused_remove)
    monkeypatch.setattr(sup, "reconcile_startup_orphan", lambda: "none")
    monkeypatch.setattr(sup, "_spawn_locked", spawn_new)
    waiter = threading.Thread(target=sup._wait_for_exit, args=(old,))
    waiter.start()
    submitter = threading.Thread(target=lambda: sup.submit_turn({"sid": "new", "request_id": "new"}))
    try:
        assert reached.wait(10)
        submitter.start()  # a replacement starts and publishes while the old exit is mid-removal
        time.sleep(0.3)
    finally:
        resume.set()
        waiter.join(10)
        submitter.join(10)
    assert not waiter.is_alive() and not submitter.is_alive()
    assert sup._proc is new
    assert json.loads(sup.registry_path.read_text(encoding="utf-8"))["host_pid"] == new.pid


def test_a_monitor_thread_that_will_not_start_leaves_nothing_running(tmp_path, monkeypatch):
    from hermes_cli.local_runtime import processes

    sup = _quiet_supervisor(tmp_path)
    proc = _FakeHost(303)
    closed: list = []
    job = types.SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(sup, "reconcile_startup_orphan", lambda: "none")
    monkeypatch.setattr(processes, "spawn_server", lambda *a, **k: (proc, job))
    monkeypatch.setattr(host_supervisor, "_write_handle", lambda _p: None)

    class _NoThread:
        def __init__(self, **_kw):
            pass

        def start(self):
            raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(host_supervisor, "threading", types.SimpleNamespace(Thread=_NoThread))
    with pytest.raises(RuntimeError, match="cannot start new thread"):
        sup.start()
    assert sup._proc is None and proc.poll() is not None and closed == [True]


def test_a_queued_turn_after_isolation_is_off_continues_from_the_store(tmp_path, monkeypatch):
    """The queued drain goes straight to _run_prompt_submit (no prompt.submit, and with a parent agent
    already built, no agent build either): it ran on this process's stale copy."""
    key, sid = "queued-key", "queued"
    durable = _store_with_transcript(tmp_path, monkeypatch, key)
    ready = threading.Event()
    ready.set()
    session = _host_session(key, [])
    session.update(agent=types.SimpleNamespace(session_id=key), agent_ready=ready, agent_build_started=True,
                   running=False, transport=None, queued_prompt={"text": "third", "transport": None})
    monkeypatch.setattr(server, "_sessions", {sid: session})
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda *a: {"turn_isolation": False})
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *a, **k: True)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)
    seen: list = []
    reached = threading.Event()

    def _prepare(_sid, s, *_a, **_k):  # the turn body's first read of the history
        seen.append(_content(s["history"]))
        reached.set()
        return None

    monkeypatch.setattr(server, "_prepare_turn_input", _prepare)
    server._drain_queued_prompt("r", sid, session)
    assert reached.wait(10), "the queued turn never reached its body"
    assert seen == [_content(durable)]
    assert session["_compute_host_active"] is False



def test_a_control_reply_at_the_deadline_reaches_the_waiter_or_the_late_handler(tmp_path, monkeypatch):
    """Reply delivery picked the waiter's queue under the lock but enqueued after it: the deadline could
    retire that queue and install the late handler in between, and the reply (a compression ack with the
    rotated session id) reached neither (review probe, Codex round 4)."""
    import queue

    sup = _quiet_supervisor(tmp_path)
    sup._proc = _FakeHost(101)
    delivering, handed_off = threading.Event(), threading.Event()
    ack = {"type": "control.ack", "request_id": "r", "sid": "s", "session_key": "rotated"}
    delivery_threads: list = []
    late: list = []

    class _DeadlineQueue:
        def __init__(self, maxsize=1):
            self.inner = queue.Queue(maxsize=maxsize)

        def get(self, timeout=None):
            assert delivering.wait(10), "the reply never reached delivery"
            raise queue.Empty  # the deadline lands after lookup and before the enqueue

        def get_nowait(self):
            return self.inner.get_nowait()

        def put_nowait(self, frame):
            delivering.set()
            handed_off.wait(2)  # an enqueue holding the supervisor lock cannot be handed off meanwhile
            self.inner.put_nowait(frame)

    monkeypatch.setattr(host_supervisor, "queue", types.SimpleNamespace(
        Queue=_DeadlineQueue, Empty=queue.Empty, Full=queue.Full))
    register = sup._register_late_control_handler_locked

    def _register_then_release(rid, callback):
        register(rid, callback)
        handed_off.set()

    monkeypatch.setattr(sup, "_register_late_control_handler_locked", _register_then_release)
    send = sup._send_frame

    def _send_then_reply(frame):
        send(frame)
        thread = threading.Thread(target=sup._deliver_control_frame, args=("r", ack))
        delivery_threads.append(thread)
        thread.start()

    monkeypatch.setattr(sup, "_send_frame", _send_then_reply)
    reply = None
    try:
        reply = sup.control("s", route_name="session.compress", payload={"request_id": "r"},
                            timeout=0.01, on_late_ack=late.append)
    except queue.Empty:
        pass
    finally:
        handed_off.set()
        for thread in delivery_threads:
            thread.join(10)
    assert (reply == ack and late == []) or (reply is None and late == [ack]), (reply, late)


def _live_host_session(tmp_path, monkeypatch, key, sid):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(key, source="desktop")
    session = dict(agent=None, agent_ready=threading.Event(), session_key=key, history=[], history_version=0,
                   history_lock=threading.Lock(), running=True, attached_images=[], cols=80, source="desktop",
                   inflight_turn=None, created_at=time.time(), last_active=time.time(), transport=None)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_sessions", {sid: session})
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_compute_host_session_info", lambda *a: {})
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)
    return db, session


def test_an_rpc_during_a_live_host_turn_never_adopts_a_partial_transcript(tmp_path, monkeypatch):
    """With isolation switched off mid-turn, an attachment RPC adopted the host's still-unwritten
    transcript and cleared host ownership; the next turn then ran on an empty history (review probe,
    Codex round 4: the real image.detach handler, bridge completion, queued drain and admission)."""
    key, sid = "live-key", "live"
    db, session = _live_host_session(tmp_path, monkeypatch, key, sid)
    cfg = {"turn_isolation": True}
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda *a: cfg)
    completions: list = []

    def _submit(frame, *, on_complete):
        completions.append((frame, on_complete))
        return frame["request_id"]

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: types.SimpleNamespace(submit_turn=_submit))
    assert "result" in server._submit_prompt_to_compute_host("r1", sid, session, "first")
    session.update(agent=types.SimpleNamespace(session_id=key), agent_build_started=True)  # parent prewarm done
    session["agent_ready"].set()
    cfg["turn_isolation"] = False
    assert server._methods["image.detach"]("attach", {"session_id": sid, "path": "unused.png"}) is not None
    durable = [("user", "first"), ("assistant", "one")]
    for role, text in durable:
        db.append_message(key, role, text)  # the host finishes writing its turn
    frame, complete = completions[0]
    complete({"type": "turn.end", "request_id": frame["request_id"], "sid": sid, "session_key": key,
              "history_version": 1})
    seen: list = []
    reached = threading.Event()

    def _prepare(_sid, s, *_a, **_k):
        server._adopt_out_of_band_turns(s)
        seen.append([(m["role"], m.get("content")) for m in s["history"]])
        reached.set()
        return None

    monkeypatch.setattr(server, "_prepare_turn_input", _prepare)
    session["queued_prompt"] = {"text": "second", "transport": None}
    assert server._drain_queued_prompt("r2", sid, session)
    assert reached.wait(10)
    assert seen == [durable]


def test_an_old_submit_never_re_marks_a_session_adopted_inline_meanwhile(tmp_path, monkeypatch):
    """A fast completion drained the next queued prompt, which adopted the session in-process; the old
    submit_turn then returned and re-marked it host-owned, so turning isolation back on routed the
    adopted session to a worker again (review probe, Codex round 4)."""
    key, sid = "fast-key", "fast"
    db, session = _live_host_session(tmp_path, monkeypatch, key, sid)
    for role, text in (("user", "first"), ("assistant", "one")):
        db.append_message(key, role, text)
    session.update(agent=types.SimpleNamespace(session_id=key), agent_build_started=True,
                   _compute_host_active=True, queued_prompt={"text": "second", "transport": None})
    session["agent_ready"].set()
    cfg = {"turn_isolation": True}
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda *a: cfg)
    reached = threading.Event()
    monkeypatch.setattr(server, "_prepare_turn_input", lambda *a, **k: reached.set())

    def _submit(frame, *, on_complete):
        cfg["turn_isolation"] = False
        on_complete({"type": "turn.error", "reason": "crash", "sid": sid, "request_id": frame["request_id"]})
        assert reached.wait(10)
        assert session["_compute_host_active"] is False
        return frame["request_id"]

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: types.SimpleNamespace(submit_turn=_submit))
    assert "result" in server._submit_prompt_to_compute_host("r1", sid, session, "first")
    cfg["turn_isolation"] = True
    assert not server._session_uses_compute_host(session)


def _host_turn_in_flight(tmp_path, monkeypatch, key="live-key", sid="live", durable=(), isolation_after=False):
    """A built session whose isolated turn is still in flight, then isolation switched to ``isolation_after``."""
    db, session = _live_host_session(tmp_path, monkeypatch, key, sid)
    for role, text in durable:
        db.append_message(key, role, text)
    session.update(agent=types.SimpleNamespace(session_id=key, interrupt=lambda *a, **k: None),
                   agent_build_started=True, cwd=str(tmp_path))
    session["agent_ready"].set()
    cfg = {"turn_isolation": True}
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda *a: cfg)
    host = types.SimpleNamespace(frames=[], completions=[], interrupts=[], controls=[])

    def _submit(frame, *, on_complete):
        host.frames.append(frame)
        host.completions.append(on_complete)
        return frame["request_id"]

    def _control(sid_, *, route_name, payload, **_k):
        host.controls.append(route_name)
        return {"type": "control.ack", "result": {"file": "saved-by-the-host.json"}}

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: types.SimpleNamespace(
        submit_turn=_submit, interrupt=lambda s, **k: host.interrupts.append(s), control=_control))
    assert "result" in server._submit_prompt_to_compute_host("r1", sid, session, "in flight")
    cfg["turn_isolation"] = isolation_after
    return db, session, host


def test_stop_then_submit_during_a_live_host_turn_never_runs_inline(tmp_path, monkeypatch):
    """With isolation switched off mid-turn, Stop routed by the new config to the idle parent, cleared
    ``running`` while the host turn went on, and the next submit adopted its partial transcript and ran
    inline beside it (review probe, Codex round 5)."""
    _db, session, host = _host_turn_in_flight(tmp_path, monkeypatch)
    server._interrupt_session_turn("live", session)
    assert host.interrupts == ["live"]  # the running turn's owner is the one stopped
    reached: list = []
    monkeypatch.setattr(server, "_prepare_turn_input", lambda *a, **k: reached.append(a))
    response = server._methods["prompt.submit"]("r2", {"session_id": "live", "text": "next"})
    thread = session.get("_run_thread")
    if thread is not None:
        thread.join(10)
    assert reached == [], response
    assert session["_compute_host_active"] is True


def test_a_completions_rotation_is_visible_before_its_turn_reads_as_settled(tmp_path, monkeypatch):
    """Completion retired the turn marker, released the lock, and only then applied the rotated
    session_key: an RPC in between adopted the pre-rotation transcript, and the session went on under
    the new key with the old history and an in-process build of the old id (review probe, Codex round 5)."""
    old_key, new_key = "old-key", "rotated-key"
    db, session, host = _host_turn_in_flight(
        tmp_path, monkeypatch, key=old_key, durable=(("user", "old question"), ("assistant", "old answer")))
    session["resume_session_id"] = old_key
    db.create_session(new_key, source="desktop", parent_session_id=old_key)
    on_done = server._on_compute_host_turn_done

    def _rpc_then_done(*args):
        server._methods["image.detach"]("attach", {"session_id": "live", "path": "unused.png"})
        on_done(*args)

    monkeypatch.setattr(server, "_on_compute_host_turn_done", _rpc_then_done)
    frame = host.frames[0]
    host.completions[0]({"type": "turn.end", "sid": "live", "request_id": frame["request_id"],
                         "session_key": new_key, "history_version": 2})
    assert session["session_key"] == new_key
    assert session["_compute_host_active"] is True
    refusal = server._adopt_if_isolation_off("inline", session)
    assert refusal is not None and refusal["error"]["code"] == 5019


@pytest.mark.parametrize("route", ["session.save", "prompt.btw"])
def test_a_history_reader_during_a_live_host_turn_never_reads_the_parents_copy(tmp_path, monkeypatch, route):
    """Deferring adoption while a host turn is live let ``session.save`` and ``prompt.btw`` read this
    process's stale copy: both saw [] although the store held a completed exchange (review probes,
    Codex round 5)."""
    durable = [("user", "earlier question"), ("assistant", "earlier answer")]
    _db, session, host = _host_turn_in_flight(tmp_path, monkeypatch, durable=durable)
    seen: list = []
    if route == "prompt.btw":
        import agent.side_question

        def _answer(text, history, **_k):
            seen.append([(m.get("role"), m.get("content")) for m in history])
            return "answer"

        monkeypatch.setattr(agent.side_question, "answer_side_question", _answer)

        def _inline_side_agent(rid, session, task_id, parent, event, body, **_k):
            body()
            return server._ok(rid, {"task_id": task_id})

        monkeypatch.setattr(server, "_spawn_side_agent", _inline_side_agent)
    response = server._methods[route]("read", {"session_id": "live", "text": "what was the earlier answer?"})
    assert "result" in response, response
    if route == "session.save":
        assert host.controls == ["session.save"]  # the host owns the transcript file
    else:
        assert seen == [durable]


def test_a_failed_dispatch_keeps_its_images_and_those_attached_meanwhile(tmp_path, monkeypatch):
    """The rollback restored the failed prompt's images only when nothing had been attached since, so an
    image attached during a slow dispatch discarded the original's (review probe, Codex round 5)."""
    _db, session = _live_host_session(tmp_path, monkeypatch, "img-key", "img")
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda *a: {"turn_isolation": True})
    session["attached_images"] = ["original.png"]

    def _fail(frame, *, on_complete):
        assert frame["attached_images"] == ["original.png"]
        session["attached_images"].append("newer.png")
        on_complete({"type": "turn.error", "reason": "send_failed", "request_id": frame["request_id"]})
        raise BrokenPipeError("failed dispatch")

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: types.SimpleNamespace(submit_turn=_fail))
    response = server._submit_prompt_to_compute_host("r", "img", session, "with an image")
    assert response["error"]["code"] == 5019
    assert session["attached_images"] == ["original.png", "newer.png"]


@pytest.mark.parametrize("rotate_inline", [False, True])
def test_an_old_host_completion_never_settles_a_turn_admitted_after_it(tmp_path, monkeypatch, rotate_inline):
    """Completion retired the host marker, released the lock, and only then cleared running and the
    in-flight turn and re-applied its session key: Stop then Submit in that gap started an inline turn,
    which the old completion then marked idle -- and, after that turn rotated its id, re-keyed back to the
    old id together with the real registry lease (review probes, Codex round 6). The finished turn now
    keeps the session through its tail: that Stop reaches its host, the Submit queues behind it, and the
    queued turn starts only once nothing of the old completion is left to run."""
    from hermes_cli.active_sessions import try_acquire_active_session

    key = "live-key"
    db, session, host = _host_turn_in_flight(tmp_path, monkeypatch, key=key)
    lease = None
    if rotate_inline:
        lease, refusal = try_acquire_active_session(session_id=key, surface="desktop", config={},
                                                   registry_home=tmp_path, track_liveness=True)
        assert refusal is None and lease is not None
        session["active_session_lease"] = lease
        db.create_session("newer-inline-key", source="desktop", parent_session_id=key)
    for role, text in (("user", "in flight"), ("assistant", "host answer")):
        db.append_message(key, role, text)
    entered, release = threading.Event(), threading.Event()
    threads: list = []

    def _prepare(*_a, **_k):
        if rotate_inline:
            session["agent"].session_id = "newer-inline-key"
            server._sync_session_key_after_compress("live", session, restart_slash_worker=False)
        entered.set()
        assert release.wait(10)
        return None

    monkeypatch.setattr(server, "_prepare_turn_input", _prepare)
    monkeypatch.setattr(server, "_tts_stream_stop", lambda: None)
    on_done = server._on_compute_host_turn_done

    def _stop_submit_then_done(*args):
        assert "result" in server._methods["session.interrupt"]("stop", {"session_id": "live"})
        assert host.interrupts == ["live"]
        response = server._methods["prompt.submit"]("new", {"session_id": "live", "text": "new inline turn"})
        assert response["result"]["status"] == "queued", response
        assert not entered.is_set()
        on_done(*args)

    monkeypatch.setattr(server, "_on_compute_host_turn_done", _stop_submit_then_done)
    try:
        frame = host.frames[0]
        host.completions[0]({"type": "turn.end", "sid": "live", "request_id": frame["request_id"],
                             "session_key": key, "history_version": 1})
        assert entered.wait(10)
        threads.append(session["_run_thread"])
        newer = "newer-inline-key" if rotate_inline else key
        assert session["running"] is True
        assert (session.get("inflight_turn") or {}).get("user") == "new inline turn"
        assert session["session_key"] == newer
        if lease is not None:
            assert lease.session_id == newer
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
        if lease is not None:
            lease.release()


@pytest.mark.parametrize("mode", ["interrupt", "steer"])
def test_busy_input_during_a_live_host_turn_goes_to_the_host_not_an_idle_parent(tmp_path, monkeypatch, mode):
    """Busy input chose the parent agent whenever one had been built: with isolation switched off mid-turn,
    interrupt mode stopped the idle parent and left the worker running, and steer mode reported a correction
    as delivered while parking it on the parent (review probes, Codex round 6)."""
    from agent.interrupt_control import InterruptControlMixin

    _db, session, _host = _host_turn_in_flight(tmp_path, monkeypatch)
    targets: list = []
    delivered = threading.Event()

    def _target(name):
        def _interrupt(*_a, **_k):
            targets.append(name)
            delivered.set()
        return _interrupt

    session["agent"].interrupt = _target("parent")
    session["agent"].steer = types.MethodType(InterruptControlMixin.steer, session["agent"])
    monkeypatch.setattr(server, "_get_compute_host_supervisor",
                        lambda *a: types.SimpleNamespace(interrupt=_target("host")))
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: mode)
    response = server._methods["prompt.submit"]("busy", {"session_id": "live", "text": "correction"})
    assert response["result"]["status"] == "queued", response
    assert (session.get("queued_prompt") or {}).get("text") == "correction"
    if mode == "interrupt":
        assert delivered.wait(10)
        assert targets == ["host"]
    assert not getattr(session["agent"], "_pending_steer", None)


def test_a_completions_terminal_events_reach_the_client_before_the_next_turn_starts(tmp_path, monkeypatch):
    """A completion's tail sent its error and idle session.info after the turn had released the session: a
    prompt admitted while the (slow) snapshot was built had its message.start overtaken by the old idle
    session.info, which the desktop takes as the end of the live turn; a replacement host's newer metadata
    could be replaced by the old frame's the same way (review probes, Codex round 7). A prompt arriving in the
    tail now queues and starts after them."""
    _db, session, host = _host_turn_in_flight(tmp_path, monkeypatch, isolation_after=True)
    events: list = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: events.append((event, payload)))
    snapshot = server._compute_host_session_info
    replies: list = []

    def _snapshot_while_a_prompt_arrives(s):
        replies.append(server._methods["prompt.submit"]("next", {"session_id": "live", "text": "next turn"}))
        return snapshot(s)

    monkeypatch.setattr(server, "_compute_host_session_info", _snapshot_while_a_prompt_arrives)
    frame = host.frames[0]
    host.completions[0]({"type": "turn.error", "message": "old worker exited", "sid": "live",
                         "request_id": frame["request_id"], "session_key": "live-key", "history_version": 1,
                         "session_info": {"model": "finished-turn-model"}})
    assert replies[0]["result"]["status"] == "queued", replies
    assert [event for event, _ in events][:2] == ["message.complete", "session.info"]
    assert events[1][1]["running"] is False
    assert [f["text"] for f in host.frames] == ["in flight", "next turn"]  # dispatched after them
    assert session["_compute_host_turn_id"] == host.frames[1]["request_id"]
    assert session["_metadata_mirror"]["model"] == "finished-turn-model"


def test_a_close_during_a_completions_tail_releases_only_that_turns_lease(tmp_path, monkeypatch):
    """A delayed completion tail released whatever deferred lease the session held: a turn admitted after
    the old one retired, then closed, had its REAL lease released while still running, and a second writer
    acquired the stored session (review probe, Codex round 7). The lease a close defers during the tail is
    the finishing turn's, and no later turn can start on the closed session."""
    from hermes_cli.active_sessions import try_acquire_active_session

    _db, session, host = _host_turn_in_flight(tmp_path, monkeypatch, isolation_after=True)
    lease, refusal = try_acquire_active_session(session_id="live-key", surface="desktop", config={},
                                               registry_home=tmp_path, track_liveness=True)
    assert refusal is None and lease is not None
    session["active_session_lease"] = lease
    monkeypatch.setattr(server, "_TURN_SETTLE_BEFORE_CLOSE_SECONDS", 0)
    monkeypatch.setattr(server, "_deferred_active_session_leases", {})
    on_done = server._on_compute_host_turn_done

    def _submit_close_then_done(*args):
        response = server._methods["prompt.submit"]("next", {"session_id": "live", "text": "next turn"})
        assert response["result"]["status"] == "queued", response
        assert server._pop_session_by_id("live") is session
        server._settle_isolated_turn_before_close(session)
        assert session["_deferred_active_session_lease"] is lease and not lease.released
        on_done(*args)

    monkeypatch.setattr(server, "_on_compute_host_turn_done", _submit_close_then_done)
    try:
        frame = host.frames[0]
        host.completions[0]({"type": "turn.end", "sid": "live", "request_id": frame["request_id"],
                             "session_key": "live-key", "history_version": 1})
        assert len(host.frames) == 1  # nothing ran after the finishing turn
        assert lease.released and not session.get("_compute_host_turn_id")
    finally:
        lease.release()


@pytest.mark.parametrize("route", ["session.steer", "session.redirect"])
@pytest.mark.parametrize("settles", ["before_enqueue", "after_enqueue"])
def test_a_correction_queued_as_the_host_turn_ends_still_runs(tmp_path, monkeypatch, route, settles):
    """The correction RPCs checked ``running`` and enqueued in separate steps: a completion in between drained
    an empty queue, and the correction, answered "queued", never ran (review probe, Codex round 7)."""
    _db, session, host = _host_turn_in_flight(tmp_path, monkeypatch, isolation_after=True)
    frame = host.frames[0]

    def _complete():
        host.completions[0]({"type": "turn.end", "sid": "live", "request_id": frame["request_id"],
                             "session_key": "live-key", "history_version": 1})

    if settles == "before_enqueue":
        routes = server._session_uses_compute_host

        def _settle_after_the_running_check(s, *a):
            answer = routes(s, *a)
            if host.completions and len(host.frames) == 1 and s.get("_compute_host_turn_id") == frame["request_id"]:
                _complete()
            return answer

        monkeypatch.setattr(server, "_session_uses_compute_host", _settle_after_the_running_check)
    response = server._methods[route]("fix", {"session_id": "live", "text": "please do this next"})
    assert response["result"]["status"] == "queued", response
    if settles == "after_enqueue":
        _complete()
    assert [f["text"] for f in host.frames] == ["in flight", "please do this next"]


@pytest.mark.parametrize("failure", ["error_event", "session_info"])
def test_a_completion_whose_events_fail_still_settles_its_turn(tmp_path, monkeypatch, failure):
    """Holding the session through the completion's tail made its events load-bearing: a raising snapshot
    or transport left the marker and running set and the lease a close had deferred held, and the supervisor
    had already consumed the completion, so nothing settled it again (review probes, Codex round 8)."""
    from hermes_cli.active_sessions import try_acquire_active_session

    _db, session, host = _host_turn_in_flight(tmp_path, monkeypatch, isolation_after=True)
    lease, refusal = try_acquire_active_session(session_id="live-key", surface="desktop", config={},
                                               registry_home=tmp_path, track_liveness=True)
    assert refusal is None and lease is not None
    session["active_session_lease"] = lease
    monkeypatch.setattr(server, "_TURN_SETTLE_BEFORE_CLOSE_SECONDS", 0)
    monkeypatch.setattr(server, "_deferred_active_session_leases", {})
    assert server._pop_session_by_id("live") is session
    server._settle_isolated_turn_before_close(session)
    assert session["_deferred_active_session_lease"] is lease

    def _fail(*_a, **_k):
        raise OSError("stream I/O failure")

    if failure == "error_event":
        monkeypatch.setattr(server, "_emit", _fail)
    else:
        monkeypatch.setattr(server, "_compute_host_session_info", _fail)
    try:
        frame = host.frames[0]
        host.completions[0]({"type": "turn.error", "message": "worker exited", "sid": "live",
                             "request_id": frame["request_id"], "session_key": "live-key"})
        assert not session.get("_compute_host_turn_id") and session["running"] is False
        assert lease.released and str(lease.lease_id) not in server._deferred_active_session_leases
    finally:
        lease.release()


@pytest.mark.parametrize("mode", ["queue", "steer", "interrupt"])
def test_a_prompt_queued_as_a_host_turn_finishes_runs_in_the_host(tmp_path, monkeypatch, mode):
    """In interrupt mode (the default) busy input sent the host a Stop, which advanced the child's queue
    generation and not the parent's: the prompt it made room for reached the child with the old generation
    and ended as interrupted without running, its queue entry already gone (review probe, Codex round 8:
    the parent's real frames replayed through the real ComputeHost handlers)."""
    _db, session, host = _host_turn_in_flight(tmp_path, monkeypatch, isolation_after=True)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: mode)
    wire: list = []
    interrupted = threading.Event()
    link = types.SimpleNamespace(start=lambda: None,
                                 _send_frame=lambda frame: (wire.append(dict(frame)), interrupted.set()))

    def _submit(frame, *, on_complete):
        host.frames.append(frame)
        host.completions.append(on_complete)
        wire.append(dict(frame))
        return frame["request_id"]

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: types.SimpleNamespace(
        submit_turn=_submit, interrupt=lambda sid, **kw: HostSupervisor.interrupt(link, sid, **kw)))
    on_done = server._on_compute_host_turn_done

    def _prompt_in_the_tail(*args):
        response = server._methods["prompt.submit"]("next", {"session_id": "live", "text": "next prompt"})
        assert response["result"]["status"] == "queued", response
        if mode == "interrupt":
            assert interrupted.wait(10)
        on_done(*args)

    monkeypatch.setattr(server, "_on_compute_host_turn_done", _prompt_in_the_tail)
    first = host.frames[0]
    host.completions[0]({"type": "turn.end", "sid": "live", "request_id": first["request_id"],
                         "session_key": "live-key", "session_info_emitted": True})
    assert [f["text"] for f in host.frames] == ["in flight", "next prompt"]
    child = dict(agent=types.SimpleNamespace(session_id="live-key", interrupt=lambda *a, **k: None),
                 session_key="live-key", history=[], history_version=0, history_lock=threading.Lock(),
                 running=False, attached_images=[], cols=80, inflight_turn=None, source="desktop",
                 _queued_prompt_generation=int(session.get("_queued_prompt_generation", 0)))
    ran: list = []
    compute_host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        with monkeypatch.context() as child_scope:  # the child's view of the same session: first turn done
            child_scope.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
            child_scope.setattr(server, "_sessions", {"live": child})
            child_scope.setattr(server, "_ensure_session_db_row", lambda *a: None)
            child_scope.setattr(server, "_persist_branch_seed", lambda *a: None)
            child_scope.setattr(server, "_session_info", lambda *a: {})
            child_scope.setattr(server, "_run_prompt_submit", lambda rid, sid, s, text, **kw: ran.append(text))
            for frame in wire:  # recorded after the first turn went out: it already ran and finished there
                if frame["type"] == "interrupt":
                    compute_host.handle_frame(frame)
                else:
                    compute_host._run_real_turn(frame)
    finally:
        compute_host.close()
    assert ran == ["next prompt"]


def test_a_busy_interrupt_still_stops_the_turn_it_names(monkeypatch):
    """The soft, turn-named interrupt that busy input sends interrupts that turn's agent while it runs, and
    leaves the child's queue generation alone; a different (finished) turn's name is a no-op."""
    stops: list = []
    child = dict(agent=types.SimpleNamespace(interrupt=lambda *a, **k: stops.append("agent")),
                 history_lock=threading.Lock(), running=True, _host_turn_request_id="t2",
                 _queued_prompt_generation=3)
    monkeypatch.setattr(server, "_sessions", {"live": child})
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *a, **k: stops.append("stop"))
    out = io.StringIO()
    compute_host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        compute_host.handle_frame({"type": "interrupt", "sid": "live", "request_id": "a", "turn_id": "t1"})
        compute_host.handle_frame({"type": "interrupt", "sid": "live", "request_id": "b", "turn_id": "t2"})
    finally:
        compute_host.close()
    acks = [json.loads(line) for line in out.getvalue().splitlines() if '"interrupt.ack"' in line]
    assert [a["applied"] for a in acks] == [False, True]
    assert stops == ["agent"] and child["_queued_prompt_generation"] == 3


def _interruptible_agent(session_id="live-key"):
    """The real AIAgent interrupt mixin, without a provider client."""
    from agent.interrupt_control import InterruptControlMixin

    agent = InterruptControlMixin()
    agent.session_id = session_id
    agent._execution_thread_id = None
    agent._active_children_lock = threading.Lock()
    agent._active_children = []
    agent.quiet_mode = True
    agent._interrupt_requested = False
    agent._hard_interrupt_requested = threading.Event()
    agent._pending_redirect_lock = threading.Lock()
    agent._pending_redirect = None
    return agent


def test_a_busy_interrupt_names_the_turn_that_was_running_when_its_prompt_queued(tmp_path, monkeypatch):
    """The busy interrupt read the host turn id after the enqueue had released the lock: when the running turn
    finished and drained the queued prompt in that gap, it named -- and the child interrupted -- the prompt it
    was sent to make room for (review probe, Codex round 9)."""
    _db, session, host = _host_turn_in_flight(tmp_path, monkeypatch, isolation_after=True)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    wire: list = []
    link = types.SimpleNamespace(start=lambda: None, _send_frame=lambda frame: wire.append(dict(frame)))

    def _submit(frame, *, on_complete):
        host.frames.append(frame)
        host.completions.append(on_complete)
        return frame["request_id"]

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: types.SimpleNamespace(
        submit_turn=_submit, interrupt=lambda sid, **kw: HostSupervisor.interrupt(link, sid, **kw)))
    busy_interrupt = server._interrupt_busy_session
    workers: list = []

    def _finish_then_interrupt(sid, s, agent, **kwargs):
        first = host.frames[0]
        host.completions[0]({"type": "turn.end", "sid": "live", "request_id": first["request_id"],
                             "session_key": "live-key", "session_info_emitted": True})
        assert [f["text"] for f in host.frames] == ["in flight", "queued follow-up"]
        before = set(threading.enumerate())
        busy_interrupt(sid, s, agent, **kwargs)
        workers.extend(t for t in threading.enumerate() if t not in before)

    monkeypatch.setattr(server, "_interrupt_busy_session", _finish_then_interrupt)
    response = server._methods["prompt.submit"]("next", {"session_id": "live", "text": "queued follow-up"})
    assert response["result"]["status"] == "queued", response
    for worker in workers:
        worker.join(10)
    follow_up = _interruptible_agent()
    child = dict(agent=follow_up, history_lock=threading.Lock(), running=True, session_key="live-key",
                 _host_turn_request_id=host.frames[1]["request_id"], _queued_prompt_generation=0)
    monkeypatch.setattr(server, "_sessions", {"live": child})
    compute_host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        for frame in wire:
            compute_host.handle_frame(frame)
    finally:
        compute_host.close()
    assert [f.get("turn_id") for f in wire] in ([], [host.frames[0]["request_id"]])
    assert not follow_up._interrupt_requested


def test_a_busy_interrupt_releases_the_clarification_its_turn_waits_on(monkeypatch):
    """The soft busy interrupt set the agent's interrupt flag but left the turn blocked in a clarification
    wait, which that flag never releases: the host acknowledged, and the turn and its queued follow-up stayed
    stuck until an answer, a Stop or the timeout (review probe, Codex round 9). It keeps the queue."""
    from tui_gateway import server_requests

    sid, agent = "clarifying", _interruptible_agent()
    child = dict(agent=agent, session_key="live-key", history_lock=threading.Lock(), running=True,
                 _host_turn_request_id="turn-a", _queued_prompt_generation=7,
                 queued_prompt={"text": "the next prompt"})
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    monkeypatch.setattr(server, "_sessions", {sid: child})
    monkeypatch.setattr(server, "_clarify_timeout_seconds", lambda: None)
    monkeypatch.setattr(server_requests, "_open", {})
    monkeypatch.setattr(server_requests, "_answerable", lambda _sid: True)
    asked, answered = threading.Event(), threading.Event()
    results: list = []
    monkeypatch.setattr(server_requests, "_write", lambda frame: asked.set())
    monkeypatch.setattr(server_requests, "_emit", lambda *a: None)

    def _clarify():
        agent._execution_thread_id = threading.get_ident()
        try:
            results.append(server._clarify_block(sid, "Which option?", ["a", "b"]))
        finally:
            answered.set()

    worker = threading.Thread(target=_clarify, daemon=True)
    child["_run_thread"] = worker
    compute_host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    worker.start()
    try:
        assert asked.wait(10)
        link = types.SimpleNamespace(start=lambda: None, _send_frame=compute_host.handle_frame)
        HostSupervisor.interrupt(link, sid, request_id="busy", turn_id="turn-a")
        assert answered.wait(5)
        assert results == [""] and not server_requests.open_requests(sid)
        assert child["_queued_prompt_generation"] == 7 and child["queued_prompt"]["text"] == "the next prompt"
    finally:
        server_requests.cancel(sid)
        worker.join(10)
        agent.clear_interrupt()
        compute_host.close()


def test_a_host_busy_interrupt_never_lands_on_the_in_process_turn_after_it(tmp_path, monkeypatch):
    """The busy interrupt re-read the route after the enqueue: with isolation switched off, once the host turn
    finished and its completion started the queued prompt in-process, the interrupt meant for that host turn
    went to the parent agent -- now running the queued prompt (review probe, Codex round 14)."""
    db, session = _live_host_session(tmp_path, monkeypatch, "live-key", "live")
    monkeypatch.chdir(tmp_path)
    cfg = {"turn_isolation": True}
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda *a: cfg)
    completions: list = []

    def _submit(frame, *, on_complete):
        completions.append((frame, on_complete))
        return frame["request_id"]

    wire: list = []
    link = types.SimpleNamespace(start=lambda: None, _send_frame=lambda frame: wire.append(dict(frame)))
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: types.SimpleNamespace(
        submit_turn=_submit, interrupt=lambda sid, **kw: HostSupervisor.interrupt(link, sid, **kw)))
    assert "result" in server._submit_prompt_to_compute_host("r1", "live", session, "host turn")
    parent = _interruptible_agent("live-key")  # prewarmed in-process while the host worked
    session.update(agent=parent, agent_build_started=True, cwd=str(tmp_path))
    session["agent_ready"].set()
    cfg["turn_isolation"] = False
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "interrupt")
    started, release = threading.Event(), threading.Event()
    seen: list = []

    def _prepare(sid, s, st, text, images):
        seen.append((text, parent._interrupt_requested))
        started.set()
        assert release.wait(15)
        return None

    monkeypatch.setattr(server, "_prepare_turn_input", _prepare)
    busy_interrupt = server._interrupt_busy_session
    workers: list = []

    def _complete_then_interrupt(sid, s, agent, **kwargs):
        frame, complete = completions[0]
        complete({"type": "turn.end", "sid": "live", "request_id": frame["request_id"],
                  "session_key": "live-key", "session_info_emitted": True})
        assert started.wait(10)  # the queued prompt now runs in-process on the parent agent
        before = set(threading.enumerate())
        busy_interrupt(sid, s, agent, **kwargs)
        workers.extend(t for t in threading.enumerate() if t not in before)

    monkeypatch.setattr(server, "_interrupt_busy_session", _complete_then_interrupt)
    try:
        reply = server._methods["prompt.submit"]("next", {"session_id": "live", "text": "queued prompt"})
        assert reply["result"]["status"] == "queued", reply
        for worker in workers:
            worker.join(10)
        assert seen == [("queued prompt", False)]
        assert not parent._interrupt_requested
        assert [f.get("turn_id") for f in wire] in ([], [completions[0][0]["request_id"]])
    finally:
        release.set()
        if (thread := session.get("_run_thread")) is not None:
            thread.join(10)
        parent.clear_interrupt()
