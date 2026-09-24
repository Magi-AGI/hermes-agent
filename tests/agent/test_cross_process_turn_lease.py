"""AIAgent enters turns only after acquiring and reloading durable state."""

from __future__ import annotations

import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent import relay_runtime
from hermes_state import SessionDB
from run_agent import AIAgent


class _DB:
    def __init__(self, session_exists=True, acquire_result=True):
        self.events = []
        self.session_exists = session_exists
        self.acquire_result = acquire_result

    def get_session(self, session_id):
        return {"id": session_id} if self.session_exists else None

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        self.events.append(("acquire", session_id, holder))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None and self.acquire_result is False:
            on_wait(0.0)
        return self.acquire_result

    def resolve_resume_session_id(self, session_id):
        self.events.append(("resolve", session_id))
        return "compressed-tip"

    def get_messages_as_conversation(self, session_id, **kwargs):
        self.events.append(("reload", session_id, kwargs))
        return [{"role": "user", "content": "durable latest"}]

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def release_session_turn_lease(self, session_id, holder):
        self.events.append(("release", session_id, holder))


def _agent_with_db(db, *, session_id="stale-parent", platform="desktop"):
    agent = AIAgent.__new__(AIAgent)
    agent.session_id = session_id
    agent.platform = platform
    agent.model = "test-model"
    agent._session_db = db
    agent._session_db_created = True
    agent._persist_disabled = False
    agent._parent_session_id = None
    agent._relay_pending_turn_id = None
    agent._reset_activity_labels_after_turn = lambda: None
    agent._conversation_root_id = lambda: session_id
    agent.log_prefix = ""
    agent._vprint = lambda *a, **k: None
    agent.status_callback = None
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._pending_redirect = None
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    return agent


def test_run_conversation_acquires_then_reloads_latest_tip(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)
    status_events = []
    agent.status_callback = lambda kind, text=None: status_events.append(
        (kind, text)
    )

    observed = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["history"] = history
        observed["session_id"] = _agent.session_id
        return {"final_response": "ok", "messages": history, "failed": False}

    # Simulate a contended wait so the resume status path is covered.
    def acquire_with_wait(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None:
            on_wait(0.0)
        return True

    db.acquire_session_turn_lease = acquire_with_wait

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "stale"}],
    )

    assert result["final_response"] == "ok"
    assert observed == {
        "history": [{"role": "user", "content": "durable latest"}],
        "session_id": "compressed-tip",
    }
    assert [event[0] for event in db.events] == [
        "acquire",
        "resolve",
        "reload",
        "release",
    ]
    assert db.events[2][2] == {
        "repair_alternation": True,
        "include_row_ids": True,
    }
    assert any(
        kind == "lifecycle"
        and text
        and "waiting for it to finish" in text
        for kind, text in status_events
    )
    assert any(
        kind == "lifecycle"
        and text
        and "loading the latest transcript" in text
        for kind, text in status_events
    )


def test_run_conversation_acquires_lease_when_session_probe_raises(monkeypatch):
    """A locked / non-WAL get_session must not skip the durable lease; the row is rechecked under it."""
    db = _DB()
    probes = []

    def locked_get_session(session_id):
        probes.append(session_id)
        if len(probes) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"id": session_id}

    db.get_session = locked_get_session
    agent = _agent_with_db(db)

    # Simulate a contended wait so the resolve+reload path is exercised.
    def acquire_with_wait(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None:
            on_wait(0.0)
        return True

    db.acquire_session_turn_lease = acquire_with_wait

    observed = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["history"] = history
        observed["session_id"] = _agent.session_id
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "stale"}],
    )

    assert result["final_response"] == "ok"
    assert observed == {
        "history": [{"role": "user", "content": "durable latest"}],
        "session_id": "compressed-tip",
    }
    assert [event[0] for event in db.events] == [
        "acquire",
        "resolve",
        "reload",
        "release",
    ]


def test_fresh_session_keeps_caller_seed_under_durable_lease(monkeypatch):
    db = _DB(session_exists=False)
    agent = _agent_with_db(db, session_id="fresh", platform="subagent")
    agent._session_db_created = False
    agent._parent_session_id = "parent"
    agent._conversation_root_id = lambda: "parent"

    observed = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["history"] = history
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    seed = [{"role": "user", "content": "delegated context"}]

    AIAgent.run_conversation(agent, "work", conversation_history=seed)

    # Leased (a fresh id is serialized too) but never reloaded: the seed is the only history.
    assert observed["history"] is seed
    assert [event[0] for event in db.events] == ["acquire", "release"]
    assert agent._session_db_created is False


def test_run_conversation_lease_timeout_returns_resend_notice(monkeypatch):
    db = _DB(acquire_result=False)
    agent = _agent_with_db(db)
    status_events = []
    agent.status_callback = lambda kind, text=None: status_events.append(
        (kind, text)
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("turn must not start without a lease")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", boom)
    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "stale"}],
    )

    assert result["failed"] is True
    assert result["completed"] is False
    assert "session_turn_lease_timeout:" in result["error"]
    assert "send it again" in result["final_response"]
    assert [event[0] for event in db.events] == ["acquire"]
    assert any(
        kind == "lifecycle"
        and text
        and "waiting for it to finish" in text
        for kind, text in status_events
    )
    assert any(
        kind == "warn" and text and "send it again" in text
        for kind, text in status_events
    )


def test_run_conversation_lease_wait_honors_interrupt(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)

    def acquire_with_abort(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        should_abort = kwargs.get("should_abort")
        assert callable(should_abort)
        agent._interrupt_requested = True
        agent._interrupt_message = "follow-up while waiting"
        assert should_abort()
        return False

    db.acquire_session_turn_lease = acquire_with_abort

    def boom(*_args, **_kwargs):
        raise AssertionError("turn must not start when lease wait is aborted")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", boom)
    result = AIAgent.run_conversation(
        agent,
        "[Monday 12:00] new message",
        conversation_history=[{"role": "user", "content": "stale"}],
        persist_user_message="new message",
        persist_user_timestamp=123.0,
        persist_user_display_kind="internal_notification",
        persist_user_display_metadata={"kind": "test"},
        persist_user_platform_id="platform-1",
    )

    assert result.get("interrupted") is True
    assert result.get("failed") is not True
    assert result.get("final_response")
    assert "not processed" in result["final_response"]
    assert result.get("interrupt_message") == "follow-up while waiting"
    assert result["messages"][-1] == {
        "role": "user",
        "content": "new message",
        "api_content": "[Monday 12:00] new message",
        "timestamp": 123.0,
        "display_kind": "internal_notification",
        "display_metadata": {"kind": "test"},
        "platform_message_id": "platform-1",
        "_persist_after_admission_interrupt": True,
    }
    assert "session_turn_lease_timeout" not in str(result.get("error", ""))
    assert [event[0] for event in db.events] == ["acquire"]
    assert agent._interrupt_requested is False
    assert agent._interrupt_message is None


def test_pre_admission_user_row_in_history_is_flushed_once():
    db = MagicMock()
    db.append_messages_batch.return_value = [1, 2]
    agent = AIAgent.__new__(AIAgent)
    agent._session_db = db
    agent._session_db_created = True
    agent._persist_disabled = False
    agent._persist_user_message_idx = 2
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    agent._persist_user_message_platform_id = None
    agent._flushed_db_message_ids = set()
    agent._last_flushed_db_idx = 0
    agent.session_id = "session"

    persisted = {"role": "assistant", "content": "old reply"}
    interrupted = {
        "role": "user",
        "content": "original",
        "_persist_after_admission_interrupt": True,
    }
    current = {"role": "user", "content": "follow-up"}
    history = [persisted, interrupted]
    messages = [*history, current]

    agent._flush_messages_to_session_db(messages, conversation_history=history)
    agent._flush_messages_to_session_db(messages, conversation_history=history)

    rows = db.append_messages_batch.call_args.kwargs["messages"]
    assert [(row["role"], row["content"]) for row in rows] == [
        ("user", "original"),
        ("user", "follow-up"),
    ]
    assert db.append_messages_batch.call_count == 1


def test_run_conversation_second_turn_after_lease_wait_abort(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)
    turns = {"n": 0}

    def acquire_then_succeed(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        should_abort = kwargs.get("should_abort")
        if turns["n"] == 0:
            agent._interrupt_requested = True
            agent._interrupt_message = "follow-up while waiting"
            assert should_abort()
            return False
        assert not should_abort()
        return True

    db.acquire_session_turn_lease = acquire_then_succeed

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    first = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "stale"}],
    )
    assert first.get("interrupted") is True
    turns["n"] = 1
    second = AIAgent.run_conversation(
        agent,
        "follow-up",
        conversation_history=[{"role": "user", "content": "stale"}],
    )
    assert second["final_response"] == "ok"
    assert agent._interrupt_requested is False


def test_carried_input_survives_waited_reload_on_follow_up_turn(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)
    turns = {"n": 0}

    def acquire_false_then_true_after_wait(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        if turns["n"] == 0:
            agent._interrupt_requested = True
            agent._interrupt_message = "follow-up while waiting"
            return False
        kwargs["on_wait"](0.0)  # the follow-up also has to wait before admission
        return True

    db.acquire_session_turn_lease = acquire_false_then_true_after_wait
    observed = {}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["history"] = history
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    first = AIAgent.run_conversation(
        agent, "original", conversation_history=[{"role": "user", "content": "stale"}]
    )
    assert first.get("interrupted") is True
    carried = first["messages"][-1]
    assert carried["_persist_after_admission_interrupt"] is True
    turns["n"] = 1
    AIAgent.run_conversation(agent, "follow-up", conversation_history=first["messages"])

    assert observed["history"] == [
        {"role": "user", "content": "durable latest"},
        carried,
    ]


def test_run_conversation_interrupts_when_lease_refresh_lost(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)
    agent._session_turn_lease_refresh_interval = 0.01
    interrupt_calls = []

    def track_interrupt(message=None, hard_cancel=False, **kwargs):
        interrupt_calls.append((message, hard_cancel))
        agent._interrupt_requested = True
        agent._interrupt_message = message

    agent.interrupt = track_interrupt

    def refresh_lost(session_id, holder, **kwargs):
        return False

    db.refresh_session_turn_lease = refresh_lost

    observed = {"started": False}

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        observed["started"] = True
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if getattr(_agent, "_interrupt_requested", False):
                return {
                    "final_response": "",
                    "messages": history,
                    "api_calls": 0,
                    "completed": False,
                    "interrupted": True,
                }
            time.sleep(0.01)
        raise AssertionError("refresh loss did not interrupt the turn")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)

    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "seed"}],
    )

    assert observed["started"] is True
    assert result.get("interrupted") is True
    assert interrupt_calls
    assert interrupt_calls[0][1] is True
    assert "lease lost" in str(interrupt_calls[0][0]).lower()


def test_run_conversation_interrupts_when_lease_refresh_errors(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)
    agent._session_turn_lease_refresh_interval = 0.01
    interrupt_calls = []

    def track_interrupt(message=None, hard_cancel=False, **kwargs):
        interrupt_calls.append((message, hard_cancel))
        agent._interrupt_requested = True
        agent._interrupt_message = message

    agent.interrupt = track_interrupt

    def refresh_error(session_id, holder, **kwargs):
        raise OSError("database unavailable")

    db.refresh_session_turn_lease = refresh_error

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if getattr(_agent, "_interrupt_requested", False):
                return {
                    "final_response": "",
                    "messages": history,
                    "api_calls": 0,
                    "completed": False,
                    "interrupted": True,
                }
            time.sleep(0.01)
        raise AssertionError("refresh error did not interrupt the turn")

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)

    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "seed"}],
    )

    assert result.get("interrupted") is True
    assert interrupt_calls
    assert interrupt_calls[0][1] is True
    assert "could not be refreshed" in str(interrupt_calls[0][0]).lower()


def test_refresh_error_after_loop_completion_does_not_poison_next_turn(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)
    agent._session_turn_lease_refresh_interval = 0.01
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    interrupt_started = threading.Event()
    interrupt_calls = []

    def track_interrupt(message=None, hard_cancel=False, **kwargs):
        interrupt_calls.append((message, hard_cancel))
        interrupt_started.set()
        release_refresh.wait(timeout=2.0)
        agent._interrupt_requested = True
        agent._interrupt_message = message

    agent.interrupt = track_interrupt

    def delayed_refresh_error(session_id, holder, **kwargs):
        refresh_started.set()
        raise OSError("database unavailable")

    db.refresh_session_turn_lease = delayed_refresh_error

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        assert refresh_started.wait(timeout=2.0)
        assert interrupt_started.wait(timeout=2.0)
        threading.Timer(0.05, release_refresh.set).start()
        return {"final_response": "ok", "messages": history, "failed": False}

    original_finish = relay_runtime.SESSION_COORDINATOR.finish_logical_calls

    def finish_after_refresh(turn, *, outcome):
        time.sleep(0.05)
        return original_finish(turn, outcome=outcome)

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    monkeypatch.setattr(
        relay_runtime.SESSION_COORDINATOR,
        "finish_logical_calls",
        finish_after_refresh,
    )

    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "seed"}],
    )

    assert result["final_response"] == "ok"
    assert len(interrupt_calls) == 1
    assert interrupt_calls[0][1] is True
    assert agent._interrupt_requested is False
    assert agent._interrupt_message is None


def test_late_refresh_miss_after_release_does_not_interrupt(monkeypatch):
    db = _DB()
    agent = _agent_with_db(db)
    agent._session_turn_lease_refresh_interval = 0.01
    released = threading.Event()
    interrupt_calls = []

    def track_interrupt(message=None, hard_cancel=False, **kwargs):
        interrupt_calls.append((message, hard_cancel))
        agent._interrupt_requested = True
        agent._interrupt_message = message

    agent.interrupt = track_interrupt

    def refresh_after_release(session_id, holder, **kwargs):
        released.wait(timeout=2.0)
        return False

    db.refresh_session_turn_lease = refresh_after_release

    orig_release = db.release_session_turn_lease

    def release_and_signal(session_id, holder):
        orig_release(session_id, holder)
        released.set()

    db.release_session_turn_lease = release_and_signal

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        time.sleep(0.03)
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "seed"}],
    )

    time.sleep(0.05)
    assert result["final_response"] == "ok"
    assert interrupt_calls == []
    assert agent._interrupt_requested is False


def test_run_conversation_exposes_holder_for_fenced_flush(monkeypatch):
    """The acquired holder is visible to persist, then cleared on release."""
    db = _DB()
    captured = {}

    def append_messages_batch(session_id, messages, **kwargs):
        captured["session_id"] = session_id
        captured["turn_lease_holder"] = kwargs.get("turn_lease_holder")
        captured["count"] = len(messages)
        return len(messages)

    db.append_messages_batch = append_messages_batch
    agent = _agent_with_db(db)
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._db_flush_scan_prefix = None
    agent._pending_cli_user_message = None
    agent._session_persist_lock = None

    # Simulate a contended wait so the resolve+reload path is exercised.
    def acquire_with_wait(session_id, holder, **kwargs):
        db.events.append(("acquire", session_id, holder))
        on_wait = kwargs.get("on_wait")
        if on_wait is not None:
            on_wait(0.0)
        return True

    db.acquire_session_turn_lease = acquire_with_wait

    def fake_run(_agent, _message, _system, history, *_args, **_kwargs):
        captured["active"] = getattr(
            _agent, "_active_session_turn_lease_holder", None
        )
        ok = _agent._flush_messages_to_session_db(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "done"},
            ],
            [],
        )
        captured["flush_ok"] = ok
        return {"final_response": "done", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    result = AIAgent.run_conversation(
        agent,
        "new message",
        conversation_history=[{"role": "user", "content": "durable latest"}],
    )

    assert result["final_response"] == "done"
    assert captured["flush_ok"] is True
    assert captured["active"]
    assert captured["active"].startswith("pid=")
    assert captured["turn_lease_holder"] == captured["active"]
    assert captured["session_id"] == "compressed-tip"
    assert captured["count"] == 2
    assert getattr(agent, "_active_session_turn_lease_holder", None) is None
    assert [event[0] for event in db.events] == [
        "acquire",
        "resolve",
        "reload",
        "release",
    ]


def _flush_agent(db, session_id):
    """Bind the real flush onto a stand-in so we can use a live SessionDB."""
    agent = SimpleNamespace(
        _session_db=db,
        _session_db_created=True,
        _persist_disabled=False,
        session_id=session_id,
        _session_persist_lock=None,
        _flushed_db_message_ids=set(),
        _flushed_db_message_session_id=None,
        _last_flushed_db_idx=0,
        _db_flush_scan_prefix=None,
        _persist_user_message_idx=None,
        _persist_user_message_override=None,
        _persist_user_message_timestamp=None,
        _pending_cli_user_message=None,
        _active_session_turn_lease_holder=None,
        _last_persistence_error_cause=None,
    )
    agent._ensure_db_session = lambda: None
    agent._flush_messages_to_session_db = (
        AIAgent._flush_messages_to_session_db.__get__(agent, AIAgent)
    )
    agent._flush_messages_to_session_db_unlocked = (
        AIAgent._flush_messages_to_session_db_unlocked.__get__(agent, AIAgent)
    )
    return agent


def test_flush_messages_to_session_db_fences_stale_holder_on_live_db(tmp_path):
    """A-loses / B-acquires / A-late-flush, through the real persist path."""
    path = tmp_path / "state.db"
    first = SessionDB(path)
    second = SessionDB(path)
    first.create_session("shared", source="test")
    stale_holder = "pid=1:turn=stale"
    next_holder = "pid=2:turn=next"
    assert first.try_acquire_session_turn_lease(
        "shared", stale_holder, ttl_seconds=5
    )

    agent = _flush_agent(first, "shared")
    agent._active_session_turn_lease_holder = stale_holder
    owned = [{"role": "user", "content": "stale-owned"}]
    assert agent._flush_messages_to_session_db(owned, []) is True
    assert [m["content"] for m in first.get_messages("shared")] == ["stale-owned"]

    first.release_session_turn_lease("shared", stale_holder)
    assert second.try_acquire_session_turn_lease(
        "shared", next_holder, ttl_seconds=5
    )

    late = [{"role": "assistant", "content": "late stale reply"}]
    assert agent._flush_messages_to_session_db(late, []) is False
    assert agent._last_persistence_error_cause == "turn_lease"
    assert [m["content"] for m in second.get_messages("shared")] == ["stale-owned"]

    agent._active_session_turn_lease_holder = next_holder
    assert agent._flush_messages_to_session_db(late, []) is True
    assert [m["content"] for m in second.get_messages("shared")] == [
        "stale-owned",
        "late stale reply",
    ]
    second.release_session_turn_lease("shared", next_holder)
    first.close()
    second.close()


def _wait_for_path(predicate, timeout_s: float, what: str) -> None:
    """Poll the filesystem barrier ``predicate`` (a deadlock guard, not a synchronization sleep)."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"barrier never reached: {what}")
        time.sleep(0.02)


_RACE_SID = "brand-new-first-turn"


def _first_turn_race_worker(name: str, other: str, directory: str) -> None:
    """Child PROCESS for the first-turn race: its own SessionDB on the shared state.db, the production
    admission (``AIAgent.run_conversation``), lazy row creation (``_ensure_db_session``) and the fenced
    flush (``_flush_messages_to_session_db``). Only the provider loop is replaced."""
    import json
    import os
    from pathlib import Path

    import agent.conversation_loop as conversation_loop

    root = Path(directory)

    class _BarrierDB(SessionDB):
        _probed = False

        def get_session(self, session_id):
            row = super().get_session(session_id)
            if session_id == _RACE_SID and not self._probed:
                # Admission's first row probe: publish what we saw, then hold until the parent has
                # seen BOTH workers observe the row absent (neither has admitted anything yet).
                self._probed = True
                (root / f"{'absent' if row is None else 'present'}-{name}").touch()
                _wait_for_path((root / "go").exists, 60, "go")
            return row

        def try_acquire_session_turn_lease(self, session_id, holder, **kwargs):
            acquired = super().try_acquire_session_turn_lease(session_id, holder, **kwargs)
            if not acquired:
                (root / f"refused-{name}").touch()  # attempted admission while the other held it
            return acquired

    db = _BarrierDB(root / "state.db")
    agent = _agent_with_db(db, session_id=_RACE_SID, platform="desktop")
    agent._session_db_created = False
    agent._session_init_model_config = {}
    agent._cached_system_prompt = "system"
    for attr, value in (
        ("_session_persist_lock", None), ("_flushed_db_message_ids", set()),
        ("_flushed_db_message_session_id", None), ("_last_flushed_db_idx", 0),
        ("_db_flush_scan_prefix", None), ("_persist_user_message_idx", None),
        ("_persist_user_message_override", None), ("_persist_user_message_timestamp", None),
        ("_pending_cli_user_message", None), ("_last_persistence_error_cause", None),
    ):
        setattr(agent, attr, value)

    def fake_run(_agent, message, _system, history, *_args, **_kwargs):
        history = list(history or [])
        try:
            os.close(os.open(root / "first-inside", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            first = True
        except FileExistsError:
            first = False
        (root / f"inside-{name}").touch()
        (root / f"history-{name}.json").write_text(json.dumps([m.get("content") for m in history]))
        _agent._ensure_db_session()  # production lazy row creation
        if first:
            # Hold the turn until the other worker either was refused admission (fixed) or entered
            # its own turn alongside this one (the unserialized base behaviour).
            _wait_for_path(
                lambda: (root / f"refused-{other}").exists() or (root / f"inside-{other}").exists(),
                60, f"{other} attempted admission")
            if (root / f"inside-{other}").exists():
                (root / "overlap").touch()
        messages = [*history, {"role": "user", "content": message},
                    {"role": "assistant", "content": f"reply {name}"}]
        ok = _agent._flush_messages_to_session_db(messages, history)
        (root / f"flush-{name}.json").write_text(json.dumps(
            {"ok": ok, "holder": bool(getattr(_agent, "_active_session_turn_lease_holder", None))}))
        return {"final_response": "ok", "messages": messages, "failed": False}

    conversation_loop.run_conversation = fake_run
    AIAgent.run_conversation(agent, f"turn {name}", conversation_history=[])
    db.close()


def test_concurrent_first_turns_on_brand_new_id_are_serialized_across_processes(tmp_path):
    """Invariant (Magi sync, bucket C R3; review: Codex, A10 round 1): two OS processes, each with its
    own SessionDB on one state.db, race the first turn of one brand-new session id through the
    production admission, lazy row creation and fenced flush. Barriers prove (a) both observed the
    row absent before either admitted, and (b) the loser attempted admission while the winner held
    the turn; the turns never overlap, both exit cleanly, and the transcript holds both turns in order
    with the loser having reloaded the winner's turn."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HERMES_HOME": str(home),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(repo_root), os.environ.get("PYTHONPATH")]))}
    procs = {
        name: subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), name, other, str(tmp_path)],
            cwd=str(repo_root), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for name, other in (("A", "B"), ("B", "A"))
    }
    try:
        _wait_for_path(
            lambda: all((tmp_path / f"absent-{n}").exists() for n in procs)
            or any(p.poll() is not None for p in procs.values()),
            120, "both workers probed the row")
        assert all((tmp_path / f"absent-{n}").exists() for n in procs), "a worker exited before probing"
        (tmp_path / "go").touch()
        outputs = {name: proc.communicate(timeout=180)[0].decode("utf-8", "replace")
                   for name, proc in procs.items()}
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
    for name, proc in procs.items():
        assert proc.returncode == 0, f"worker {name} failed:\n{outputs[name]}"

    assert not (tmp_path / "overlap").exists(), "two first turns on one new id ran concurrently"
    refused = [n for n in procs if (tmp_path / f"refused-{n}").exists()]
    assert len(refused) == 1, refused  # exactly the loser attempted admission while the winner held it
    loser = refused[0]
    winner = "A" if loser == "B" else "B"
    assert json.loads((tmp_path / f"history-{winner}.json").read_text()) == []
    # The loser reloaded the durable turn the winner persisted before its own turn ran.
    assert json.loads((tmp_path / f"history-{loser}.json").read_text()) == [f"turn {winner}", f"reply {winner}"]
    for name in procs:
        assert json.loads((tmp_path / f"flush-{name}.json").read_text()) == {"ok": True, "holder": True}
    db = SessionDB(tmp_path / "state.db")
    try:
        assert db.get_session(_RACE_SID) is not None  # created lazily by the winner's turn
        assert [m["content"] for m in db.get_messages(_RACE_SID)] == [
            f"turn {winner}", f"reply {winner}", f"turn {loser}", f"reply {loser}"]
        # Both leases released.
        assert db.try_acquire_session_turn_lease(_RACE_SID, "pid=1:turn=after", ttl_seconds=5)
    finally:
        db.close()


def test_fresh_row_that_appears_before_admission_is_reloaded_but_empty_keeps_seed():
    """The row probe runs before the lease; a racer may create (and run) the session in between.
    Re-probing UNDER the lease reloads it even on an immediate acquisition, while an empty durable
    transcript never erases the caller's seed."""
    from agent.turn_facade_lease import admit_durable_turn_lease

    class _AppearingDB(_DB):
        def __init__(self, reloaded):
            super().__init__(session_exists=False)
            self._reloaded = reloaded

        def acquire_session_turn_lease(self, session_id, holder, **kwargs):
            self.session_exists = True  # the racer created the row and released
            return super().acquire_session_turn_lease(session_id, holder, **kwargs)

        def resolve_resume_session_id(self, session_id):
            self.events.append(("resolve", session_id))
            return session_id

        def get_messages_as_conversation(self, session_id, **kwargs):
            self.events.append(("reload", session_id))
            return list(self._reloaded)

    seed = [{"role": "user", "content": "seed"}]
    for reloaded, expected in (
        ([{"role": "user", "content": "racer turn"}], [{"role": "user", "content": "racer turn"}]),
        ([], seed),
    ):
        db = _AppearingDB(reloaded)
        agent = _agent_with_db(db, session_id="fresh")
        agent._session_db_created = False
        agent._emit_status = lambda *_a, **_k: None
        admission = admit_durable_turn_lease(
            agent, session_id="fresh", relay_turn_id="fresh:t:1",
            task_context={"session_id": "fresh", "task_id": "t", "platform": "desktop"},
            conversation_history=seed,
        )
        try:
            assert admission.lease is not None
            assert admission.conversation_history == expected
            assert agent._session_db_created is True
            assert [e[0] for e in db.events] == ["acquire", "resolve", "reload"]
        finally:
            if admission.lease is not None:
                admission.lease.release()


def test_fresh_lease_key_is_stable_when_the_turn_creates_its_row(tmp_path):
    """The fresh lease is keyed on the bare id (no row to walk). The rows a first turn creates must
    keep that key, or refresh/release/fenced writes would miss mid-turn: a plain row, and a delegated
    child (``_delegate_from`` fork marker) whose parent already ended by compression."""
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("parent", source="desktop")
        db.end_session("parent", "compression")
        for sid, kwargs in (
            ("plain-first-turn", {}),
            ("delegated-child", {"parent_session_id": "parent",
                                 "model_config": {"_delegate_from": "parent"}}),
        ):
            holder = f"pid=1:turn={sid}"
            assert db.try_acquire_session_turn_lease(sid, holder, ttl_seconds=5)
            db.create_session(sid, source="subagent" if kwargs else "desktop", **kwargs)
            assert db._session_turn_lease_key(sid) == sid
            assert db.refresh_session_turn_lease(sid, holder, ttl_seconds=5)
            db.release_session_turn_lease(sid, holder)
            assert db.try_acquire_session_turn_lease(sid, "pid=2:turn=next", ttl_seconds=5)
    finally:
        db.close()


def test_caller_authoritative_history_is_never_swapped_for_another_chats_transcript(tmp_path, monkeypatch):
    """Invariant (review: Codex, A10 round 1): two header-less API chats with the same system prompt
    and first message share one derived session id. The second waits on the lease while the first
    persists ITS chat; the second's model must still see only the second chat's own history."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "agent.turn_liveness.resolve_turn_liveness_settings", lambda cfg: (None, 1.0)
    )
    sid = "api-0123456789abcdef"
    loser_refused = threading.Event()

    class _ObservedDB(SessionDB):
        def try_acquire_session_turn_lease(self, session_id, holder, **kwargs):
            acquired = super().try_acquire_session_turn_lease(session_id, holder, **kwargs)
            if not acquired:
                loser_refused.set()
            return acquired

    path = tmp_path / "state.db"
    winner_db, loser_db = SessionDB(path), _ObservedDB(path)
    winner_inside = threading.Event()
    observed = {}

    def fake_run(_agent, message, _system, history, *_args, **_kwargs):
        observed[message] = [m["content"] for m in history or []]
        if message == "chat A: second question":
            winner_inside.set()
            _agent._session_db.create_session(sid, source="api_server")
            _agent._session_db.append_message(sid, role="user", content="chat A private context")
            assert loser_refused.wait(30), "loser never attempted admission"
        return {"final_response": "ok", "messages": history, "failed": False}

    monkeypatch.setattr("agent.conversation_loop.run_conversation", fake_run)
    winner = _agent_with_db(winner_db, session_id=sid, platform="api_server")
    loser = _agent_with_db(loser_db, session_id=sid, platform="api_server")
    for agent in (winner, loser):
        agent._session_db_created = False
    chat_a = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "A reply"}]
    chat_b = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "B reply"}]
    errors = []

    def run(agent, message, history):
        try:
            AIAgent.run_conversation(
                agent, message, conversation_history=history, caller_history_authoritative=True)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    t1 = threading.Thread(target=run, args=(winner, "chat A: second question", chat_a))
    t1.start()
    assert winner_inside.wait(30)
    t2 = threading.Thread(target=run, args=(loser, "chat B: second question", chat_b))
    t2.start()
    t1.join(60)
    t2.join(60)
    try:
        assert not errors
        assert loser_refused.is_set()  # the lease serialized the two chats
        assert observed["chat B: second question"] == ["hi", "B reply"]
        assert "chat A private context" not in observed["chat B: second question"]
        assert loser._session_db_created is True  # row proven present under the lease
    finally:
        winner_db.close()
        loser_db.close()


def test_caller_authoritative_admission_skips_reload_after_wait():
    """Unit: a waited admission of an EXISTING row keeps the caller's authoritative history (no
    resolve/reload), while the default durable continuation still reloads."""
    from agent.turn_facade_lease import admit_durable_turn_lease

    class _WaitingDB(_DB):
        def acquire_session_turn_lease(self, session_id, holder, **kwargs):
            self.events.append(("acquire", session_id, holder))
            kwargs["on_wait"](0.0)
            return True

    seed = [{"role": "user", "content": "client history"}]
    for authoritative, expected, events in (
        (True, seed, ["acquire", "release"]),
        (False, [{"role": "user", "content": "durable latest"}], ["acquire", "resolve", "reload", "release"]),
    ):
        db = _WaitingDB()
        agent = _agent_with_db(db, session_id="shared")
        statuses = []
        agent._emit_status = statuses.append
        admission = admit_durable_turn_lease(
            agent, session_id="shared", relay_turn_id="shared:t:1",
            task_context={"session_id": "shared", "task_id": "t", "platform": "api_server"},
            conversation_history=seed, caller_history_authoritative=authoritative,
        )
        admission.lease.release()
        assert admission.conversation_history == expected
        assert [e[0] for e in db.events] == events
        assert agent.session_id == ("shared" if authoritative else "compressed-tip")


if __name__ == "__main__":
    import sys

    _first_turn_race_worker(sys.argv[1], sys.argv[2], sys.argv[3])
