"""Phase 2 — wiring the root-scoped compaction queue into compress_context().

The queue is a THROUGHPUT THROTTLE, never a blocker. Everything here defends two
properties that are easy to break and expensive to get wrong:

1. **enabled=false (the default) changes nothing.** The coordinator is not even
   imported. The InstallDir is the live backend, so a default-off feature must be
   provably inert.
2. **DENIED is a TRUE no-op.** Not "compact a bit less" — *nothing*. No
   ``on_pre_compress`` (that tells an external memory provider context is about to
   be discarded, which would be a real side effect on a session that did nothing),
   no ``compress()``, no rotation, and crucially no ``COMPACTION_STATUS`` — the
   session is merely *waiting*, and announcing "Compacting context" on every
   re-check would be a lie.

``compress_context`` is duck-typed on ``agent: Any``, so these tests drive it with
a stub agent. That is deliberate: it makes the ORDERING assertions exact (lock →
slot → status → on_pre_compress → compress → release) and it does not depend on a
working OpenAI SDK install.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import compaction_coordinator as cc
from agent import conversation_compression as cv
from agent.compaction_coordinator import (
    SlotOutcome,
    get_compaction_slot_load,
    try_acquire_compaction_slot,
)
from agent.conversation_compression import COMPACTION_STATUS_MARKER, compress_context


class _Sentinel(Exception):
    """Raised from the stubbed compress() to stop before the rotation machinery.

    We only need to observe what happened UP TO compaction; the rotation/DB path
    is exercised by the existing suites.
    """


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """Point the coordinator at a temp root — never the developer's real one."""
    r = tmp_path / "hermes-root"
    r.mkdir()
    monkeypatch.setenv("HERMES_COMPACTION_HOME", str(r))
    monkeypatch.setattr(cc, "_logged_path", None, raising=False)
    return r


def _agent(*, enabled=False, max_concurrent=1, ttl=300.0, max_wait=300.0,
           notify_after=60.0, context_length=100_000, sid="sess-1"):
    """A stub AIAgent carrying exactly what compress_context touches."""
    a = SimpleNamespace()
    a.api_mode = "chat"
    a._compression_feasibility_checked = True
    a.compression_in_place = False
    a.session_id = sid
    a.model = "test/model"
    a.platform = "cli"

    a._session_db = None          # no per-session lock DB → lock is a no-op
    a._memory_manager = MagicMock()
    a._emit_status = MagicMock()
    a._emit_warning = MagicMock()
    a._cached_system_prompt = "SYSTEM"
    a._build_system_prompt = MagicMock(return_value="SYSTEM")

    a.context_compressor = MagicMock()
    a.context_compressor.context_length = context_length
    a.context_compressor.compress.side_effect = _Sentinel

    a.compaction_queue_enabled = enabled
    a.compaction_queue_max_concurrent = max_concurrent
    a.compaction_slot_ttl_seconds = ttl
    a.compaction_queue_max_wait_seconds = max_wait
    a.compaction_queue_notify_after_seconds = notify_after
    return a


MSGS = [{"role": "user", "content": f"m{i}"} for i in range(10)]


def _run(agent, approx_tokens=1000):
    """Drive compress_context; swallow the sentinel so we can inspect the agent."""
    try:
        return compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=approx_tokens)
    except _Sentinel:
        return None


def _statuses(agent):
    return [c.args[0] for c in agent._emit_status.call_args_list if c.args]


# ── 1. Default (disabled) is provably inert ─────────────────────────────────


class TestDisabledByDefault:
    def test_disabled_never_touches_the_coordinator(self, root):
        agent = _agent(enabled=False)
        with patch.object(cc, "try_acquire_compaction_slot") as acquire:
            _run(agent)
        acquire.assert_not_called()
        assert not (root / "compaction.db").exists(), (
            "a disabled queue must not even create the coordinator DB"
        )

    def test_disabled_still_compacts_and_emits_status_once(self, root):
        agent = _agent(enabled=False)
        _run(agent)
        agent._memory_manager.on_pre_compress.assert_called_once()
        agent.context_compressor.compress.assert_called_once()
        assert _statuses(agent) == [cv.COMPACTION_STATUS]

    def test_missing_queue_attrs_entirely_are_safe(self, root):
        """An agent from before Phase 1 (no queue fields) must still compact."""
        agent = _agent(enabled=False)
        for f in ("compaction_queue_enabled", "compaction_queue_max_concurrent",
                  "compaction_slot_ttl_seconds", "compaction_queue_max_wait_seconds",
                  "compaction_queue_notify_after_seconds"):
            delattr(agent, f)
        _run(agent)
        agent.context_compressor.compress.assert_called_once()


# ── 2. ACQUIRED — ordering, status, release ─────────────────────────────────


class TestAcquired:
    def test_slot_is_acquired_BEFORE_on_pre_compress(self, root):
        """The ordering that makes DENIED a true no-op.

        on_pre_compress is the first pre-compaction SIDE EFFECT (it tells an
        external memory provider that context is about to be discarded), so the
        queue must be consulted before it — not before compress().
        """
        order = []
        agent = _agent(enabled=True)
        agent._memory_manager.on_pre_compress.side_effect = lambda *_: order.append("pre_compress")
        agent.context_compressor.compress.side_effect = lambda *a, **k: order.append("compress") or (_ for _ in ()).throw(_Sentinel())

        real_acquire = cc.try_acquire_compaction_slot

        def _spy(*a, **k):
            order.append("acquire")
            return real_acquire(*a, **k)

        with patch.object(cv, "_admit_to_compaction_queue", wraps=cv._admit_to_compaction_queue):
            with patch.object(cc, "try_acquire_compaction_slot", _spy):
                _run(agent)

        assert order == ["acquire", "pre_compress", "compress"]

    def test_status_is_emitted_only_after_acquisition(self, root):
        agent = _agent(enabled=True)
        _run(agent)
        assert _statuses(agent) == [cv.COMPACTION_STATUS]

    def test_slot_is_released_even_when_compress_raises(self, root):
        """The sentinel escapes compress() — the slot must still be freed."""
        agent = _agent(enabled=True)
        _run(agent)  # compress raises _Sentinel inside
        load = get_compaction_slot_load(max_concurrent=1)
        assert load.ok and load.slots_in_use == 0, (
            "a slot leaked past a failed compaction — the next session would be "
            "denied forever until the TTL expired"
        )

    def test_a_second_session_is_denied_while_the_first_holds(self, root):
        held = try_acquire_compaction_slot("other", max_concurrent=1)
        assert held.acquired

        agent = _agent(enabled=True, sid="mine")
        _run(agent)

        agent.context_compressor.compress.assert_not_called()
        agent._memory_manager.on_pre_compress.assert_not_called()


# ── 3. DENIED — a TRUE no-op ────────────────────────────────────────────────


class TestDenied:
    @pytest.fixture()
    def denied(self, root):
        # Fill the single slot from "another session".
        other = try_acquire_compaction_slot("other-session", max_concurrent=1)
        assert other.acquired
        agent = _agent(enabled=True, sid="mine")
        result = compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        return agent, result

    def test_messages_are_returned_unchanged(self, denied):
        agent, (msgs, sp) = denied
        assert msgs == MSGS
        assert sp == "SYSTEM"

    def test_no_compaction_status_on_first_denial(self, denied):
        agent, _ = denied
        assert _statuses(agent) == [], (
            "a denied session announced 'Compacting context' but compacted nothing"
        )

    def test_on_pre_compress_is_NOT_called(self, denied):
        agent, _ = denied
        agent._memory_manager.on_pre_compress.assert_not_called()

    def test_compressor_is_NOT_called(self, denied):
        agent, _ = denied
        agent.context_compressor.compress.assert_not_called()

    def test_denial_does_not_block_or_sleep(self, root):
        """The no-throttle guarantee: the turn loop proceeds immediately."""
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine")

        t0 = time.time()
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        assert time.time() - t0 < 1.0, "DENIED must return immediately, never wait"

    def test_denial_records_pending_and_stays_silent(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", notify_after=60.0)
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)

        pending = agent._compaction_queue_first_pending_at_by_session
        assert "mine" in pending
        assert _statuses(agent) == []

    def test_queued_notice_after_notify_after_carries_NO_compacting_marker(self, root):
        """The gateway maps COMPACTION_STATUS_MARKER to kind="compacting".

        A queued session has not compacted anything, so the notice must not carry
        the marker — otherwise the UI shows "Summarizing…" for a session that is
        merely waiting.
        """
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", notify_after=0.001)

        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)  # arms pending
        time.sleep(0.02)
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)  # notice fires

        notices = _statuses(agent)
        assert notices, "expected a queued notice after notify_after_seconds"
        for n in notices:
            assert COMPACTION_STATUS_MARKER not in n
        assert "queued" in notices[0].lower()

    def test_queued_notice_is_deduplicated(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", notify_after=0.001)
        for _ in range(4):
            compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
            time.sleep(0.01)
        assert len(_statuses(agent)) == 1, "the queued notice must fire once per pending window"


# ── 4. Fail-open: COORDINATOR_ERROR and AttributeError ──────────────────────


class TestFailOpen:
    def test_coordinator_error_compacts_unbounded(self, root):
        agent = _agent(enabled=True)
        with patch.object(cc, "_connect", side_effect=Exception("disk I/O error")):
            _run(agent)
        agent.context_compressor.compress.assert_called_once()
        assert _statuses(agent) == [cv.COMPACTION_STATUS]

    def test_caller_side_AttributeError_fails_open(self, root):
        """Module/version skew — the historical no-progress-spin shape.

        A stale in-memory module missing try_acquire_compaction_slot raises
        AttributeError at the CALL SITE (not sqlite3.Error), so the coordinator's
        own guard never runs. It must not freeze compaction.
        """
        agent = _agent(enabled=True)
        with patch.object(cc, "try_acquire_compaction_slot",
                          side_effect=AttributeError("no attribute")):
            _run(agent)
        agent.context_compressor.compress.assert_called_once()

    def test_import_failure_fails_open(self, root):
        agent = _agent(enabled=True)
        with patch.dict("sys.modules", {"agent.compaction_coordinator": None}):
            _run(agent)
        agent.context_compressor.compress.assert_called_once()

    def test_fail_open_never_leaves_a_session_stuck(self, root):
        """A broken coordinator must degrade to today's behaviour, not DENIED."""
        agent = _agent(enabled=True)
        with patch.object(cc, "try_acquire_compaction_slot",
                          side_effect=RuntimeError("boom")):
            _run(agent)
        agent._memory_manager.on_pre_compress.assert_called_once()


# ── 5. Hard-wall and wait-cap bypasses ──────────────────────────────────────


class TestHardWallBypass:
    """Pressure estimate is ``approx_tokens`` (the caller-provided pre-API request
    estimate that conversation_loop passes to compress_context), compared against
    ``agent.context_compressor.context_length``.

    The hard wall (0.92) MUST stay distinct from compression.threshold (0.50).
    Reusing the threshold as the bypass trigger would make EVERY compaction bypass
    the queue and silently void the feature — the two quantities are separate on
    purpose (spec §4.4.3).
    """

    def test_threshold_pressure_STAYS_QUEUED(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", context_length=100_000)

        # 55% — above the compaction threshold, far below the hard wall.
        msgs, _ = compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=55_000)

        assert msgs == MSGS, "threshold-triggered compaction must stay queued"
        agent.context_compressor.compress.assert_not_called()

    def test_hard_wall_pressure_BYPASSES_the_full_queue(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", context_length=100_000)

        # 95% — at/above the hard wall: the next request may genuinely not fit.
        _run(agent, approx_tokens=95_000)

        agent.context_compressor.compress.assert_called_once()
        assert _statuses(agent) == [cv.COMPACTION_STATUS]

    def test_hard_wall_helper_boundary(self):
        agent = _agent(context_length=100_000)
        assert cv._at_compaction_hard_wall(agent, 91_000) is False
        assert cv._at_compaction_hard_wall(agent, 92_000) is True
        assert cv._at_compaction_hard_wall(agent, None) is False
        assert cv._at_compaction_hard_wall(_agent(context_length=0), 99_999) is False

    def test_hard_wall_is_far_above_the_compaction_threshold(self):
        """Regression guard: if these ever converge, the queue becomes a no-op."""
        from hermes_cli.config import DEFAULT_CONFIG

        threshold = DEFAULT_CONFIG["compression"]["threshold"]
        assert cv._COMPACTION_HARD_WALL_FRACTION > threshold + 0.3


class TestWaitCapBypass:
    def test_pending_past_max_wait_bypasses(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", max_wait=60.0)

        # First attempt: DENIED, arms the wait-cap clock.
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        agent.context_compressor.compress.assert_not_called()

        # Backdate the pending stamp past max_wait.
        agent._compaction_queue_first_pending_at_by_session["mine"] = time.time() - 61.0

        _run(agent)  # next natural re-check: bypass and compact unbounded
        agent.context_compressor.compress.assert_called_once()

    def test_bypass_clears_pending_state(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", max_wait=60.0)
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        agent._compaction_queue_first_pending_at_by_session["mine"] = time.time() - 61.0
        _run(agent)
        assert "mine" not in agent._compaction_queue_first_pending_at_by_session

    def test_acquisition_clears_pending_state(self, root):
        held = try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine")
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)  # DENIED
        assert "mine" in agent._compaction_queue_first_pending_at_by_session

        cc.release_compaction_slot(held.slot_id, held.holder)
        _run(agent)  # now ACQUIRED
        assert "mine" not in agent._compaction_queue_first_pending_at_by_session

    def test_max_wait_is_progress_insurance_not_a_latency_guarantee(self, root):
        """It bypasses on the next natural RE-CHECK after the deadline elapses —
        it does not wake a pending session at exactly max_wait_seconds."""
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", max_wait=0.05)
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        time.sleep(0.06)
        # Nothing happened on its own; only the next call re-checks.
        agent.context_compressor.compress.assert_not_called()
        _run(agent)
        agent.context_compressor.compress.assert_called_once()


# ── 6. Slot refresher ───────────────────────────────────────────────────────


class TestRefresher:
    def test_refresher_starts_only_for_an_acquired_slot(self, root):
        agent = _agent(enabled=True)
        with patch.object(cv, "_CompactionSlotLeaseRefresher") as R:
            _run(agent)
        R.assert_called_once()
        R.return_value.start.assert_called_once()
        R.return_value.start.return_value.stop.assert_called_once()  # released in finally

    def test_refresher_NOT_started_when_denied(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine")
        with patch.object(cv, "_CompactionSlotLeaseRefresher") as R:
            compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        R.assert_not_called()

    def test_refresher_NOT_started_on_fail_open_bypass(self, root):
        agent = _agent(enabled=True)
        with patch.object(cc, "try_acquire_compaction_slot",
                          side_effect=RuntimeError("boom")):
            with patch.object(cv, "_CompactionSlotLeaseRefresher") as R:
                _run(agent)
        R.assert_not_called()

    def test_refresher_NOT_started_when_disabled(self, root):
        agent = _agent(enabled=False)
        with patch.object(cv, "_CompactionSlotLeaseRefresher") as R:
            _run(agent)
        R.assert_not_called()

    def test_refresher_extends_the_lease(self, root):
        held = try_acquire_compaction_slot("s", max_concurrent=1, ttl_seconds=2.0)
        before = get_compaction_slot_load().holders[0]["expires_at"]

        r = cv._CompactionSlotLeaseRefresher(
            held.slot_id, held.holder, ttl_seconds=60.0, refresh_interval_seconds=0.1,
        ).start()
        try:
            time.sleep(0.35)
            after = get_compaction_slot_load().holders[0]["expires_at"]
        finally:
            r.stop()
        assert after > before

    def test_refresher_gives_up_within_one_ttl_on_persistent_error(self, root):
        """A wedged refresher must not hold a slot past its TTL."""
        held = try_acquire_compaction_slot("s", max_concurrent=1)
        r = cv._CompactionSlotLeaseRefresher(
            held.slot_id, held.holder, ttl_seconds=0.5, refresh_interval_seconds=0.1,
        )
        with patch.object(cc, "refresh_compaction_slot",
                          side_effect=Exception("db down")):
            r.start()
            time.sleep(1.2)
            alive = r._thread.is_alive()
        r.stop()
        assert not alive, "refresher must stop after ~1 TTL of consecutive failures"

    def test_refresher_stops_when_ownership_is_lost(self, root):
        held = try_acquire_compaction_slot("s", max_concurrent=1)
        r = cv._CompactionSlotLeaseRefresher(
            held.slot_id, held.holder, ttl_seconds=60.0, refresh_interval_seconds=0.05,
        ).start()
        try:
            cc.release_compaction_slot(held.slot_id, held.holder)  # DENIED from now on
            time.sleep(0.3)
            assert not r._thread.is_alive()
        finally:
            r.stop()


# ── 7. Routing invariance (§5.1) ────────────────────────────────────────────


class TestRoutingInvariance:
    def test_queue_state_does_not_change_compression_routing(self, root):
        """The queue controls WHEN compaction runs, never WHERE it routes."""
        from agent.auxiliary_client import _resolve_task_provider_model

        with patch("hermes_cli.config.load_config", return_value={}):
            disabled = _resolve_task_provider_model("compression")

        for held in (True, False):
            if held:
                try_acquire_compaction_slot("other", max_concurrent=1)
            with patch("hermes_cli.config.load_config", return_value={}):
                enabled = _resolve_task_provider_model("compression")
            assert enabled == disabled, (
                "queue state changed compression provider/model resolution"
            )

    def test_compressor_module_never_imports_the_coordinator(self):
        from pathlib import Path

        src = Path(cc.__file__).parent / "context_compressor.py"
        assert "compaction_coordinator" not in src.read_text(encoding="utf-8")


# ── 8. Concurrency: the bound actually holds ────────────────────────────────


class TestConcurrentCompactionIsBounded:
    """Two sessions compacting concurrently must serialise on the root slot.

    NOTE ON SCOPE: this drives the real ``compress_context`` admission path against
    the real root coordinator DB from two threads. The coordinator opens a fresh
    connection per operation, so this genuinely exercises SQLite BEGIN IMMEDIATE
    contention. The CROSS-PROFILE, CROSS-PROCESS proof (the regression for the
    original Gate 0 bug) lives in test_compaction_coordinator_slots.py, which spawns
    real subprocesses under distinct HERMES_HOME profiles. Full multi-process
    compress_context stress is left to the final gate.
    """

    def test_at_most_one_compaction_runs_at_a_time(self, root):
        in_flight = []
        peak = []
        lock = threading.Lock()

        def _compress(*_a, **_k):
            with lock:
                in_flight.append(1)
                peak.append(len(in_flight))
            time.sleep(0.25)          # hold the slot
            with lock:
                in_flight.pop()
            raise _Sentinel()

        agents = []
        for i in range(3):
            a = _agent(enabled=True, sid=f"s{i}")
            a.context_compressor.compress.side_effect = _compress
            agents.append(a)

        threads = [threading.Thread(target=_run, args=(a,)) for a in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert peak, "no compaction ran at all"
        assert max(peak) == 1, (
            f"max_concurrent=1 violated: {max(peak)} compactions ran at once"
        )
        # The losers deferred cleanly rather than erroring.
        assert sum(a.context_compressor.compress.call_count for a in agents) >= 1

    def test_slots_are_all_released_afterwards(self, root):
        agent = _agent(enabled=True)
        _run(agent)
        assert get_compaction_slot_load().slots_in_use == 0


# ── 9. CALLER-SIDE silence on DENIED (Codex 008b blocker) ───────────────────


class TestCallersDoNotAnnounceCompactionBeforeItHappens:
    """The Phase 2 contract is stronger than "compress_context emits late".

    It is: **on queue DENIED the user must see NO compaction/compressing message
    from ANY caller path** before the queued-notice threshold.

    My first Phase 2 pass only moved COMPACTION_STATUS *inside* compress_context.
    That was insufficient: several branches in conversation_loop.py announced
    "Compacting…" / "— compressing…" BEFORE calling it. On a DENIED attempt nothing
    is compacted, so the user was told about work that never happened — and because
    those branches retry up to the per-turn attempt cap, they'd be told repeatedly.
    """

    CALLER_SRC = None

    def _src(self):
        from pathlib import Path

        import agent.conversation_loop as cl

        return Path(cl.__file__).read_text(encoding="utf-8")

    # User-facing emitters. logger.* is fine — it is not shown to the user.
    EMIT_FNS = {"_emit_status", "_buffer_status", "_safe_print", "_buffer_vprint", "_vprint"}
    # Present-tense claims that compaction IS happening / is about to happen.
    # Deliberately NOT the bare word "compression": "Max compression attempts
    # reached" and "auto-compaction disabled — not compressing" are terminal or
    # negated statements, not claims of imminent work.
    CLAIM_WORDS = ("compacting", "compressing")

    def _claims_compaction(self, call_node):
        import ast

        text = " ".join(
            n.value.lower()
            for n in ast.walk(call_node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        )
        if not any(w in text for w in self.CLAIM_WORDS):
            return False
        # Negated forms ("not compressing", "auto-compaction disabled") are honest.
        if "not compressing" in text or "not compacting" in text or "disabled" in text:
            return False
        return True

    def test_no_caller_claims_compaction_before_compress_context(self):
        """STRUCTURAL regression guard for the ENTIRE enclosing branch.

        My first version of this guard only scanned the 6 statements immediately
        preceding the call — which is precisely why it MISSED the provider-overflow
        ``_buffer_vprint`` sites Codex found: they sit in an if/elif/else several
        statements further up in the same block.

        This version walks every ancestor block of each ``_compress_context`` call
        and inspects ALL preceding sibling statements, recursing into their nested
        branches. A user-facing emitter anywhere on the path to the call that claims
        compaction is happening is a bug, because the queue may DENY and compact
        nothing — and these branches retry, so the false claim repeats.
        """
        import ast

        tree = ast.parse(self._src())

        # Parent links so we can walk up from a call to its enclosing blocks.
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def _emitters_in(node):
            for n in ast.walk(node):
                if (isinstance(n, ast.Call)
                        and getattr(n.func, "attr", None) in self.EMIT_FNS
                        and self._claims_compaction(n)):
                    yield n

        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "_compress_context"
        ]
        assert calls, "expected _compress_context call sites in conversation_loop"

        offenders = []
        for call in calls:
            # Walk up to the enclosing statement, then up through every block.
            node = call
            while node in parents and not isinstance(node, ast.stmt):
                node = parents[node]

            cur = node
            while cur in parents:
                parent = parents[cur]
                body = getattr(parent, "body", None)
                if isinstance(body, list) and cur in body:
                    idx = body.index(cur)
                    for prev in body[:idx]:          # ALL preceding siblings...
                        for emit in _emitters_in(prev):   # ...including nested branches
                            offenders.append(
                                f"line {emit.lineno}: user-facing text claims compaction "
                                f"before _compress_context at line {call.lineno}"
                            )
                cur = parent

        assert not offenders, (
            "caller-side code claims compaction BEFORE calling _compress_context. The "
            "queue may DENY, in which case nothing is compacted and the user is told a "
            "lie — repeatedly, since these branches retry:\n  "
            + "\n  ".join(sorted(set(offenders)))
        )

    def test_context_overflow_pre_call_messages_do_not_claim_compression_before_queue_admission(self):
        """The two provider-overflow branches Codex identified, pinned literally.

        They must state the FACT (we kept the context length) and not predict
        compaction, which the queue may deny.
        """
        src = self._src()

        assert "keeping context_length at {old_ctx:,} tokens and compressing." not in src, (
            "the provider-overflow branches still claim 'and compressing' before "
            "_compress_context — a queue DENIAL makes that a lie"
        )
        # The informative half is retained.
        assert "Provider reported overflow amount only;" in src
        assert "provider did not report a max context length" in src

        # And neither surviving message claims compaction.
        for line in src.splitlines():
            if "keeping context_length at" in line:
                low = line.lower()
                assert "compressing" not in low and "compacting" not in low, line

    def test_post_compaction_statuses_are_guarded_by_an_actual_shrink(self):
        """The 'Compressed X → Y' statuses fire AFTER the call, and only when the
        message list or token count genuinely shrank — so a DENIED no-op (which
        returns the messages unchanged) cannot trigger them."""
        src = self._src()
        assert "if len(messages) < original_len or (new_tokens > 0 and new_tokens < original_tokens * 0.95):" in src

    def test_compress_context_publishes_the_deferral_decision(self, root):
        """Callers can distinguish a real compaction from a deferred no-op."""
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine")
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        assert agent._last_compaction_deferred is True

        agent2 = _agent(enabled=False)
        _run(agent2)
        assert agent2._last_compaction_deferred is False


class TestRealAgentIsSilentOnDenial:
    """Integration-style: a REAL AIAgent, the REAL root coordinator, queue DENIED.

    Requires a working SDK install (AIAgent construction). Skipped rather than
    silently weakened where that is broken.
    """

    def _real_agent(self, tmp_path, sid):
        pytest.importorskip("pydantic_core")
        from hermes_state import SessionDB
        from run_agent import AIAgent

        db = SessionDB(tmp_path / "state.db")
        db.create_session(session_id=sid, source="test", model="test/model")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=db,
                session_id=sid,
                skip_context_files=True,
                skip_memory=True,
            )
        return agent

    def test_real_agent_denied_emits_no_status_at_all(self, root, tmp_path):
        # Another session holds the only slot.
        held = try_acquire_compaction_slot("other-session", max_concurrent=1)
        assert held.acquired

        agent = self._real_agent(tmp_path, "mine")
        agent.compaction_queue_enabled = True
        agent.compaction_queue_max_concurrent = 1
        agent.compaction_queue_notify_after_seconds = 3600  # high: no queued notice
        # The one-shot lazy feasibility probe (aux-provider check) runs on the
        # first compaction of a session and can emit its own provider-config
        # warning. That is not a compaction claim and is not what is under test —
        # pin it as already-checked so this asserts purely on the DENIED path, the
        # way a warmed session behaves on every subsequent re-check.
        agent._compression_feasibility_checked = True
        agent.context_compressor = MagicMock()
        agent.context_compressor.context_length = 100_000
        agent.context_compressor.compress.side_effect = AssertionError("must not compact")

        statuses = []
        agent._emit_status = lambda s, *a, **k: statuses.append(s)

        msgs = list(MSGS)
        out, _sp = compress_context(agent, msgs, "SYSTEM", approx_tokens=1000)

        assert out == msgs, "DENIED must return the messages unchanged"
        assert statuses == [], (
            f"a denied session emitted user-facing status(es): {statuses}"
        )
        assert agent._last_compaction_deferred is True
        agent.context_compressor.compress.assert_not_called()

    def test_real_agent_acquired_emits_exactly_one_truthful_status(self, root, tmp_path):
        agent = self._real_agent(tmp_path, "solo")
        agent.compaction_queue_enabled = True
        agent.compaction_queue_max_concurrent = 1
        agent._compression_feasibility_checked = True   # see above
        agent.context_compressor = MagicMock()
        agent.context_compressor.context_length = 100_000
        agent.context_compressor.compress.side_effect = _Sentinel

        statuses = []
        agent._emit_status = lambda s, *a, **k: statuses.append(s)

        try:
            compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        except _Sentinel:
            pass

        assert statuses.count(cv.COMPACTION_STATUS) == 1, (
            f"expected exactly one truthful compaction status, got: {statuses}"
        )
