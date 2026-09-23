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


# --- review round 4 (Codex): no self-deadlock inside a compression commit ---------------------------

def test_a_loss_detected_inside_a_compression_commit_does_not_deadlock(agent_db):
    """Real CompressionCommitFence + real interrupt(): a refusal detected INSIDE the commit (fence
    lock held by this thread) records the loss at once and parks the hard interrupt; the fence
    owner delivers it after finish_commit. Delivering it inside waited on this very fence."""
    import threading
    from agent.conversation_compression import CompressionCommitFence
    from agent.turn_facade_lease import deliver_pending_lease_loss_interrupt, on_turn_lease_lost
    agent, _db = agent_db
    fence = CompressionCommitFence()
    agent._active_compression_commit_fence = fence
    assert fence.begin_commit()
    done = threading.Event()

    def _inside_commit():
        on_turn_lease_lost(agent, "old-executor")   # what the commit's except handler does
        done.set()

    worker = threading.Thread(target=_inside_commit, daemon=True)
    worker.start()
    finished = done.wait(10.0)
    try:
        assert finished, "on_turn_lease_lost blocked on the commit fence it was called under"
        assert agent._turn_lease_lost_holder == "old-executor", "the loss is recorded immediately"
        assert agent._pending_lease_loss_interrupt, "the hard interrupt is parked, not dropped"
    finally:
        fence.finish_commit()
    deliver_pending_lease_loss_interrupt(agent)
    assert getattr(agent, "_interrupt_requested", False) is True
    assert agent._pending_lease_loss_interrupt is None


def test_outside_a_commit_the_interrupt_is_immediate(agent_db):
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, _db = agent_db
    on_turn_lease_lost(agent, "old-executor")
    assert getattr(agent, "_interrupt_requested", False) is True
    assert not getattr(agent, "_pending_lease_loss_interrupt", None)
