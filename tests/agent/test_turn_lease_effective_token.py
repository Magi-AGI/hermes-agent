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


# --- review rounds 4-6 (Codex): a lease loss inside a compression commit never deadlocks -----------

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


def _commit_bracket(agent, fence, body):
    """What both production brackets do: begin, body, finish, then deliver a parked interrupt."""
    from agent.conversation_compression import _deliver_owner_interrupt
    assert fence.begin_commit()
    try:
        return body()
    finally:
        fence.finish_commit()
        _deliver_owner_interrupt(agent, fence)


def test_the_committing_thread_parks_its_interrupt_and_delivers_after_finish(agent_db):
    """Real CompressionCommitFence + real AIAgent.interrupt(): inside its own commit the thread records
    the loss at once and parks the interrupt on its own fence (found thread-locally); it is delivered
    right after finish_commit. (Pre-cc24078df1 this waited on itself.)"""
    from agent.conversation_compression import CompressionCommitFence
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, _db = agent_db
    fence = CompressionCommitFence()
    agent._active_compression_commit_fence = fence
    seen = {}

    def _body():
        on_turn_lease_lost(agent, "old-executor")
        seen["recorded"] = agent._turn_lease_lost_holder
        seen["interrupted_inside"] = bool(getattr(agent, "_interrupt_requested", False))

    _run_in_thread(lambda: _commit_bracket(agent, fence, _body))
    assert seen == {"recorded": "old-executor", "interrupted_inside": False}
    assert agent._interrupt_requested is True
    assert fence.take_owner_interrupt() is None, "delivered exactly once"


def test_the_shared_fence_slot_is_irrelevant_to_the_owner(agent_db):
    """Codex's A/B registration interleaving: whatever the agent's shared slot names (an overlapping
    attempt's B, or the restored A), the committing thread finds ITS OWN fence thread-locally."""
    from agent.conversation_compression import CompressionCommitFence
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, _db = agent_db
    fence_a, fence_b = CompressionCommitFence(), CompressionCommitFence()

    def _body():
        agent._active_compression_commit_fence = fence_b
        on_turn_lease_lost(agent, "old-executor")
        agent._active_compression_commit_fence = fence_a
        return fence_a._owner_interrupt, fence_b._owner_interrupt

    parked_a, parked_b = _run_in_thread(lambda: _commit_bracket(agent, fence_a, _body))
    assert parked_a and parked_b is None
    assert agent._interrupt_requested is True and not fence_b.is_cancelled


@pytest.mark.parametrize("external", ["user_stop", "lease_refresher"])
def test_an_external_cancel_already_waiting_does_not_deadlock_the_owner(agent_db, external, monkeypatch):
    """Codex round 6: an external hard cancel holds _pending_redirect_lock while it waits on the
    owner's fence; the owner then detects lease loss. Calling interrupt() inside the commit would
    need that lock (lock inversion). The owner parks instead; both complete."""
    import threading
    from agent.conversation_compression import CompressionCommitFence
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, db = agent_db
    fence = CompressionCommitFence()
    agent._active_compression_commit_fence = fence
    in_commit, external_waiting = threading.Event(), threading.Event()
    done = {}
    # Deterministic (Codex nit): signal from the external cancel's ACTUAL fence wait, i.e. with
    # _pending_redirect_lock held and about to block on this fence, instead of sleeping.
    real_cancel = CompressionCommitFence.cancel_before_commit

    def _cancel_entry(self, *a, **k):
        if self is fence:
            external_waiting.set()
        return real_cancel(self, *a, **k)

    monkeypatch.setattr(CompressionCommitFence, "cancel_before_commit", _cancel_entry)

    def _owner():
        def _body():
            in_commit.set()
            assert external_waiting.wait(10), "the external cancel never reached its fence wait"
            on_turn_lease_lost(agent, "old-executor")
        _commit_bracket(agent, fence, _body)
        done["owner"] = True

    def _external():
        assert in_commit.wait(10)
        if external == "user_stop":
            agent.interrupt("stop", hard_cancel=True)
        else:
            lease = DurableTurnLease(agent, db, SID, "old-executor")
            lease.turn_active = True
            lease._interrupt_turn("Session turn lease lost; stopping to protect the transcript.")
        done["external"] = True

    ext = threading.Thread(target=_external, daemon=True)
    own = threading.Thread(target=_owner, daemon=True)
    ext.start()
    own.start()
    own.join(15)
    ext.join(15)
    assert done == {"owner": True, "external": True}, "deadlocked"
    assert agent._interrupt_requested is True
    assert agent._turn_lease_lost_holder == "old-executor"


def test_another_thread_still_waits_out_an_in_flight_commit(agent_db):
    """Upstream behaviour for an external hard cancel is unchanged: a non-owner waits for the commit
    to finish, then publishes."""
    import threading
    from agent.conversation_compression import CompressionCommitFence
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

    owner = threading.Thread(target=_owner, daemon=True)
    owner.start()
    assert in_commit.wait(10)
    cb = threading.Thread(target=lambda: (on_turn_lease_lost(agent, "old-executor"), callback_done.set()),
                          daemon=True)
    cb.start()
    assert not callback_done.wait(0.5), "a non-owner waits out the in-flight commit"
    release_commit.set()
    owner.join(10)
    assert callback_done.wait(10)
    assert getattr(agent, "_interrupt_requested", False) is True


def test_outside_a_commit_the_interrupt_is_immediate(agent_db):
    from agent.turn_facade_lease import on_turn_lease_lost
    agent, _db = agent_db
    on_turn_lease_lost(agent, "old-executor")
    assert getattr(agent, "_interrupt_requested", False) is True
