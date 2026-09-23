"""One effective ownership token for every writer, recorded at every loss detection, and cleared only
after a FULL re-admission (review: Codex round 2 on sync bucket C).

1. A loss the lease refresher detects (not a flush) must be sticky too: otherwise a final flush that
   fails on a SQLite lock error, followed by turn cleanup, leaves a later shutdown flush with no
   token at all, which skipped the lease check while a successor owned the session.
2. In-place compression and the compressor's callback must present the same token as flushes:
   reading only the active holder let a taken-over agent commit a stale summary after cleanup.
3. A re-admission that fails after acquiring (history reload / setup raises) must not erase the
   recorded loss.
Backstop: a flush with no token at all never writes into another process's live turn.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import turn_facade_lease
from agent.turn_facade_lease import DurableTurnLease, admit_durable_turn_lease, effective_turn_lease_holder
from hermes_state import SessionDB
from run_agent import AIAgent

SID = "sess-effective-token"


@pytest.fixture()
def agent_db():
    tmp = tempfile.mkdtemp(prefix="lease_token_")
    db = SessionDB(Path(tmp) / "state.db")
    db.create_session(session_id=SID, source="desktop", model="test-model")
    agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                    skip_context_files=True, skip_memory=True, session_db=db, session_id=SID)
    agent._session_db_created = True
    try:
        yield agent, db
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _contents(db):
    return [m["content"] for m in db.get_messages(SID)]


def _expire_and_take_over(db, holder="old-executor"):
    assert db.try_acquire_session_turn_lease(SID, holder, ttl_seconds=0.2)
    time.sleep(0.4)
    assert db.try_acquire_session_turn_lease(SID, "successor", ttl_seconds=300)


def test_a_loss_the_refresher_detects_is_sticky(agent_db):
    agent, db = agent_db
    agent._active_session_turn_lease_holder = "old-executor"
    _expire_and_take_over(db)
    lease = DurableTurnLease(agent, db, SID, "old-executor")
    lease.turn_active = True
    assert lease.refresh_tick() is False  # the refresher sees the successor
    assert agent._turn_lease_lost_holder == "old-executor"

    # The final flush then fails for an unrelated reason, and cleanup clears the active holder.
    agent._active_session_turn_lease_holder = None
    assert agent._flush_messages_to_session_db([{"role": "user", "content": "stale"}]) is False
    assert _contents(db) == []


def test_a_refresh_error_also_fences_late_writers(agent_db, monkeypatch):
    agent, db = agent_db
    agent._active_session_turn_lease_holder = "old-executor"
    _expire_and_take_over(db)
    lease = DurableTurnLease(agent, db, SID, "old-executor")
    lease.turn_active = True

    def _locked(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "refresh_session_turn_lease", _locked)
    assert lease.refresh_tick() is False
    agent._active_session_turn_lease_holder = None
    assert agent._flush_messages_to_session_db([{"role": "user", "content": "stale"}]) is False
    assert _contents(db) == []


def test_a_tokenless_flush_never_writes_into_another_live_turn(agent_db):
    """Backstop when no loss was ever recorded (e.g. no refresher tick ran)."""
    agent, db = agent_db
    assert db.try_acquire_session_turn_lease(SID, "successor", ttl_seconds=300)
    agent._active_session_turn_lease_holder = None
    assert agent._flush_messages_to_session_db([{"role": "user", "content": "stale"}]) is False
    assert _contents(db) == []
    db.release_session_turn_lease(SID, "successor")
    agent._flush_messages_to_session_db([{"role": "user", "content": "leftover"}])
    assert _contents(db) == ["leftover"], "with no live turn, a tokenless flush still persists"


def test_compression_writers_present_the_recorded_lost_holder(agent_db):
    agent, db = agent_db
    from agent.conversation_compression import _effective_turn_lease_holder
    agent._active_session_turn_lease_holder = None
    agent._turn_lease_lost_holder = "old-executor"
    assert _effective_turn_lease_holder(agent) == "old-executor"
    source = getattr(agent.context_compressor, "_turn_lease_holder_source", None)
    assert source is not None and source() == "old-executor"

    # And the DB refuses that holder's in-place compaction after the successor finished and released.
    _expire_and_take_over(db)
    db.append_message(SID, role="user", content="successor's row")
    db.release_session_turn_lease(SID, "successor")
    with pytest.raises(Exception):
        db.archive_and_compact(SID, [{"role": "user", "content": "stale summary"}],
                               turn_lease_holder=_effective_turn_lease_holder(agent))
    assert _contents(db) == ["successor's row"]


def _admission_agent(db):
    agent = SimpleNamespace(
        _session_db=db, session_id="s1", _persist_disabled=False, _interrupt_requested=False,
        _interrupt_message=None, _execution_thread_id=None, _session_turn_lease_refresh_interval=60.0,
        _turn_lease_lost_holder="old-executor")
    agent._emit_status = lambda *a, **k: None
    agent._emit_warning = lambda *a, **k: None
    agent._touch_activity = lambda *a, **k: None
    import threading
    agent._liveness_activity_lock = lambda: threading.Lock()
    return agent


class _Db:
    def get_session(self, session_id):
        return {"id": session_id}

    def acquire_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def refresh_session_turn_lease(self, session_id, holder, **kwargs):
        return True

    def release_session_turn_lease(self, session_id, holder):
        pass


def _admit(agent):
    return admit_durable_turn_lease(
        agent, session_id="s1", relay_turn_id="s1:t:abcd",
        task_context={"session_id": "s1", "task_id": "t", "platform": "cli"}, conversation_history=[])


def test_a_failed_readmission_keeps_the_recorded_loss(monkeypatch):
    agent = _admission_agent(_Db())

    def _setup_fails(self):
        raise RuntimeError("history reload failed")

    monkeypatch.setattr(DurableTurnLease, "build_threads", _setup_fails)
    with pytest.raises(RuntimeError):
        _admit(agent)
    assert agent._turn_lease_lost_holder == "old-executor"
    assert effective_turn_lease_holder(agent) == "old-executor"


def test_a_successful_readmission_ends_the_fence():
    agent = _admission_agent(_Db())
    admission = _admit(agent)
    assert admission.lease is not None
    assert agent._turn_lease_lost_holder is None
    assert effective_turn_lease_holder(agent) == agent._active_session_turn_lease_holder


# --- review round 3 (Codex) ------------------------------------------------------------------------

def test_a_refused_compressor_commit_records_the_loss_and_stops_the_turn(agent_db):
    """Micro-compaction's DB sync is refused for lease loss: the token it presented stays fenced
    and the owning turn is interrupted, exactly like a refresher-detected loss."""
    from hermes_state_errors import SessionTurnLeaseLostError
    agent, _db = agent_db
    agent._active_session_turn_lease_holder = "old-executor"
    compressor = agent.context_compressor

    class _RefusingDb:
        def archive_and_compact(self, *_a, **_k):
            raise SessionTurnLeaseLostError("taken over")

    compressor._session_db = _RefusingDb()
    compressor._session_id = SID
    compressor._sync_micro_compact_to_db([{"role": "assistant", "content": "summary"}])
    agent._active_session_turn_lease_holder = None  # turn cleanup
    assert agent._turn_lease_lost_holder == "old-executor"
    assert getattr(agent, "_interrupt_requested", False) is True


def test_the_turn_level_compression_handler_records_and_interrupts(agent_db):
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, _db = agent_db
    on_turn_lease_lost(agent, "old-executor")
    assert agent._turn_lease_lost_holder == "old-executor"
    assert getattr(agent, "_interrupt_requested", False) is True


def test_a_refresher_that_outlived_its_turn_cannot_reinstate_an_old_fence(agent_db, monkeypatch):
    """Codex's interleaving: the refresher passes its stop check, pauses while logging the miss, the
    old turn ends and the next one is fully admitted, then the refresher resumes. Recording under
    the lifecycle lock (after the same stop check shutdown takes) means it records nothing, so the
    newer turn's clean release is not refused later."""
    agent, db = agent_db
    _expire_and_take_over(db)
    lease = DurableTurnLease(agent, db, SID, "old-executor")
    lease.turn_active = True

    def _log_then_turn_ends(*_a, **_k):
        lease.stop_refresher()               # old turn ends while the callback is paused here
        agent._turn_lease_lost_holder = None  # next turn fully admitted

    monkeypatch.setattr(turn_facade_lease.logger, "error", _log_then_turn_ends)
    assert lease.refresh_tick() is False
    assert getattr(agent, "_turn_lease_lost_holder", None) is None


def test_a_live_refresher_still_records_the_loss(agent_db):
    agent, _db = agent_db
    lease = DurableTurnLease(agent, _db, SID, "old-executor")
    lease.turn_active = True
    lease._interrupt_turn("Session turn lease lost; stopping to protect the transcript.")
    assert agent._turn_lease_lost_holder == "old-executor"


# --- review rounds 4-5 (Codex): deferred interrupt ownership --------------------------------------

def _run_in_thread(fn, timeout=10.0):
    import threading
    box = {}

    def _target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # surface to the test thread
            box["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "blocked (deadlock)"
    if "error" in box:
        raise box["error"]
    return box.get("result")


def test_the_committing_thread_parks_and_then_delivers_its_own_interrupt(agent_db):
    """Real CompressionCommitFence + real AIAgent.interrupt(): the committing thread (fence owner)
    cannot deliver a hard interrupt while holding the fence; it parks it ON THE FENCE and delivers
    it after its own finish_commit. Previously this waited on itself."""
    from agent.conversation_compression import CompressionCommitFence, _deliver_owner_interrupt
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, _db = agent_db
    fence = CompressionCommitFence()
    agent._active_compression_commit_fence = fence
    seen = {}

    def _commit():
        assert fence.begin_commit()
        on_turn_lease_lost(agent, "old-executor")          # the commit's except handler
        seen["recorded"] = agent._turn_lease_lost_holder
        seen["interrupted_inside"] = bool(getattr(agent, "_interrupt_requested", False))
        fence.finish_commit()
        _deliver_owner_interrupt(agent, fence)              # the commit's outer finally

    _run_in_thread(_commit)
    assert seen == {"recorded": "old-executor", "interrupted_inside": False}
    assert getattr(agent, "_interrupt_requested", False) is True
    assert fence.take_owner_interrupt() is None, "delivered exactly once"


def test_another_thread_never_parks_so_nothing_is_stranded(agent_db):
    """Codex's interleaving: a loss callback on ANOTHER thread while a commit is in flight. It must
    not park (the owner might already have drained); it delivers, waiting out the commit like any
    external hard cancel. Nothing is left for a later turn to receive."""
    import threading
    from agent.conversation_compression import CompressionCommitFence, _deliver_owner_interrupt
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, _db = agent_db
    fence = CompressionCommitFence()
    agent._active_compression_commit_fence = fence
    in_commit, release_commit, callback_done = threading.Event(), threading.Event(), threading.Event()

    def _owner():
        assert fence.begin_commit()
        in_commit.set()
        release_commit.wait(10)
        fence.finish_commit()
        _deliver_owner_interrupt(agent, fence)

    owner = threading.Thread(target=_owner, daemon=True)
    owner.start()
    assert in_commit.wait(10)

    def _callback():
        on_turn_lease_lost(agent, "old-executor")           # e.g. micro-compaction's loss sink
        callback_done.set()

    cb = threading.Thread(target=_callback, daemon=True)
    cb.start()
    assert fence.take_owner_interrupt() is None, "a non-owner never parks on the fence"
    release_commit.set()
    owner.join(10)
    assert callback_done.wait(10), "the non-owner delivered once the commit finished"
    assert getattr(agent, "_interrupt_requested", False) is True
    assert fence.take_owner_interrupt() is None
    assert not hasattr(agent, "_pending_lease_loss_interrupt") or not agent._pending_lease_loss_interrupt


def test_outside_a_commit_the_interrupt_is_immediate(agent_db):
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, _db = agent_db
    on_turn_lease_lost(agent, "old-executor")
    assert getattr(agent, "_interrupt_requested", False) is True
