"""Phase 3 — compaction queue observability (spec §7).

Two rules shape every assertion here:

* **A disabled queue records NOTHING.** The default path never runs the queue, so
  a metric implying it did would be a signal an operator could act on — worse than
  no metric at all.
* **DENIED and coordinator_error must never be conflated.** DENIED is a
  *successful* observation that the queue is full. ``failopen{coordinator_error}``
  means the queue is silently NOT queueing — from the outside that is
  indistinguishable from a healthy idle queue, so the counter is the only way an
  operator ever finds out. Merging them would let a broken queue hide behind
  "looks busy".
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import compaction_coordinator as cc
from agent import compaction_metrics as cm
from agent import conversation_compression as cv
from agent.compaction_coordinator import try_acquire_compaction_slot
from agent.conversation_compression import compress_context


class _Sentinel(Exception):
    """Stops compress() before the rotation machinery — not under test here."""


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "hermes-root"
    r.mkdir()
    monkeypatch.setenv("HERMES_COMPACTION_HOME", str(r))
    monkeypatch.setattr(cc, "_logged_path", None, raising=False)
    cm.reset()
    yield r
    cm.reset()


def _agent(*, enabled=True, max_concurrent=1, ttl=300.0, max_wait=300.0,
           notify_after=3600.0, context_length=100_000, sid="s1", platform="cli"):
    a = SimpleNamespace()
    a.api_mode = "chat"
    a._compression_feasibility_checked = True
    a.compression_in_place = False
    a.session_id = sid
    a.model = "test/model"
    a.platform = platform
    a._session_db = None
    a._memory_manager = None
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
    try:
        return compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=approx_tokens)
    except _Sentinel:
        return None


# ── Disabled path records nothing ───────────────────────────────────────────


class TestDisabledRecordsNothing:
    def test_no_queue_metrics_and_no_db(self, root):
        agent = _agent(enabled=False)
        _run(agent)
        assert cm.snapshot() == {}, (
            "a disabled queue recorded metrics — that implies the queue ran"
        )
        assert not (root / "compaction.db").exists()

    def test_disabled_does_not_record_failopen(self, root):
        """'disabled' is NOT a fail-open: nothing was bypassed, the feature is off."""
        _run(_agent(enabled=False))
        assert cm.get_counter(cm.QUEUE_FAILOPEN_TOTAL, reason="coordinator_error") == 0
        assert cm.get_counter(cm.QUEUE_FAILOPEN_TOTAL, reason="hardwall") == 0
        assert cm.get_counter(cm.QUEUE_FAILOPEN_TOTAL, reason="waitcap") == 0


# ── ACQUIRED ────────────────────────────────────────────────────────────────


class TestAcquired:
    def test_records_slot_gauges_from_the_committed_transaction(self, root):
        _run(_agent(enabled=True, max_concurrent=2))
        assert cm.get_gauge(cm.QUEUE_SLOTS_IN_USE) == 1.0
        assert cm.get_gauge(cm.QUEUE_SLOTS_MAX) == 2.0

    def test_records_zero_wait_when_it_never_queued(self, root):
        _run(_agent(enabled=True, platform="cli"))
        obs = cm.get_observation(cm.QUEUE_WAIT_SECONDS, source="cli")
        assert obs is not None
        assert obs["count"] == 1
        assert obs["last"] == 0.0

    def test_acquired_is_not_a_denial_or_a_failopen(self, root):
        _run(_agent(enabled=True))
        assert cm.get_counter(cm.QUEUE_DENIED_TOTAL, source="cli") == 0
        assert cm.snapshot().get(cm.QUEUE_FAILOPEN_TOTAL, {}) == {}

    def test_reclaimed_expired_is_counted(self, root):
        # A dead holder whose lease has expired.
        dead = try_acquire_compaction_slot("dead", max_concurrent=1, ttl_seconds=1.0)
        assert dead.acquired
        cm.reset()

        agent = _agent(enabled=True, sid="live")
        with patch.object(cv.time, "time", return_value=time.time() + 5.0):
            _run(agent)

        assert cm.get_counter(cm.QUEUE_RECLAIMED_EXPIRED_TOTAL) == 1

    def test_no_reclaim_recorded_when_nothing_expired(self, root):
        _run(_agent(enabled=True))
        assert cm.get_counter(cm.QUEUE_RECLAIMED_EXPIRED_TOTAL) == 0


# ── DENIED ──────────────────────────────────────────────────────────────────


class TestDenied:
    @pytest.fixture()
    def denied(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        cm.reset()
        agent = _agent(enabled=True, sid="mine", platform="telegram")
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        return agent

    def test_increments_denied_total_with_source(self, denied):
        assert cm.get_counter(cm.QUEUE_DENIED_TOTAL, source="telegram") == 1

    def test_records_slot_load_from_the_denied_result(self, denied):
        assert cm.get_gauge(cm.QUEUE_SLOTS_IN_USE) == 1.0
        assert cm.get_gauge(cm.QUEUE_SLOTS_MAX) == 1.0

    def test_records_wait_seconds_by_source(self, denied):
        obs = cm.get_observation(cm.QUEUE_WAIT_SECONDS, source="telegram")
        assert obs is not None and obs["count"] == 1
        assert obs["last"] >= 0.0

    def test_denied_is_NOT_a_failopen(self, denied):
        """The distinction that keeps a broken queue from hiding behind 'busy'."""
        assert cm.snapshot().get(cm.QUEUE_FAILOPEN_TOTAL, {}) == {}

    def test_repeated_denials_accumulate_and_wait_grows(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        cm.reset()
        agent = _agent(enabled=True, sid="mine")

        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)
        time.sleep(0.05)
        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)

        assert cm.get_counter(cm.QUEUE_DENIED_TOTAL, source="cli") == 2
        obs = cm.get_observation(cm.QUEUE_WAIT_SECONDS, source="cli")
        assert obs["count"] == 2
        assert obs["max"] >= 0.04, "the second denial should record a longer wait"


# ── Fail-open reasons ───────────────────────────────────────────────────────


class TestFailOpenReasons:
    def test_hardwall_bypass(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        cm.reset()
        agent = _agent(enabled=True, sid="mine", context_length=100_000)

        with patch.object(cc, "try_acquire_compaction_slot") as acquire:
            _run(agent, approx_tokens=95_000)          # 95% -> hard wall
            acquire.assert_not_called()                 # coordinator not consulted

        assert cm.get_counter(cm.QUEUE_FAILOPEN_TOTAL, reason="hardwall") == 1
        agent.context_compressor.compress.assert_called_once()

    def test_waitcap_bypass_records_the_actual_wait(self, root):
        try_acquire_compaction_slot("other", max_concurrent=1)
        agent = _agent(enabled=True, sid="mine", max_wait=60.0)

        compress_context(agent, list(MSGS), "SYSTEM", approx_tokens=1000)  # DENIED
        cm.reset()
        agent._compaction_queue_first_pending_at_by_session["mine"] = time.time() - 90.0

        _run(agent)

        assert cm.get_counter(cm.QUEUE_FAILOPEN_TOTAL, reason="waitcap") == 1
        obs = cm.get_observation(cm.QUEUE_WAIT_SECONDS, source="cli")
        assert obs is not None
        assert obs["last"] >= 89.0, "waitcap must record the REAL pending duration"
        agent.context_compressor.compress.assert_called_once()

    def test_coordinator_error_is_alertable(self, root):
        agent = _agent(enabled=True)
        with patch.object(cc, "_connect", side_effect=Exception("disk I/O error")):
            _run(agent)
        assert cm.get_counter(cm.QUEUE_FAILOPEN_TOTAL, reason="coordinator_error") == 1
        agent.context_compressor.compress.assert_called_once()  # still fail-open

    def test_caller_side_AttributeError_is_metered_as_coordinator_error(self, root):
        agent = _agent(enabled=True)
        with patch.object(cc, "try_acquire_compaction_slot",
                          side_effect=AttributeError("module skew")):
            _run(agent)
        assert cm.get_counter(cm.QUEUE_FAILOPEN_TOTAL, reason="coordinator_error") == 1

    def test_unknown_reason_is_not_silently_dropped(self):
        cm.reset()
        cm.record_queue_failopen("something-new")
        assert cm.get_counter(cm.QUEUE_FAILOPEN_TOTAL, reason="coordinator_error") == 1
        cm.reset()


# ── Metrics module mechanics ────────────────────────────────────────────────


class TestMetricsModule:
    def test_reset_clears_queue_metrics_too(self):
        cm.reset()
        cm.record_queue_denied("cli")
        cm.record_queue_slot_load(1, 2)
        cm.record_queue_wait_seconds(3.0, "cli")
        cm.record_queue_failopen(cm.REASON_HARDWALL)
        cm.record_queue_reclaimed_expired(2)
        assert cm.snapshot() != {}

        cm.reset()
        assert cm.snapshot() == {}
        assert cm.get_gauge(cm.QUEUE_SLOTS_IN_USE) is None
        assert cm.get_observation(cm.QUEUE_WAIT_SECONDS, source="cli") is None

    def test_snapshot_includes_all_three_kinds(self):
        cm.reset()
        cm.record_queue_denied("cli")
        cm.record_queue_slot_load(1, 1)
        cm.record_queue_wait_seconds(1.5, "cli")
        snap = cm.snapshot()
        assert cm.QUEUE_DENIED_TOTAL in snap          # counter
        assert cm.QUEUE_SLOTS_IN_USE in snap          # gauge
        assert cm.QUEUE_WAIT_SECONDS in snap          # observation
        cm.reset()

    def test_route_guard_counters_still_work(self):
        """Backward compatibility: Part 1's counters are untouched."""
        cm.reset()
        cm.record_route_rejected("anthropic", "metered_auth_mode")
        assert cm.get_counter(
            cm.ROUTE_REJECTED_TOTAL, provider="anthropic", reason="metered_auth_mode",
        ) == 1
        cm.reset()

    def test_observation_tracks_count_sum_min_max_last(self):
        cm.reset()
        for v in (1.0, 5.0, 3.0):
            cm.record_queue_wait_seconds(v, "cli")
        obs = cm.get_observation(cm.QUEUE_WAIT_SECONDS, source="cli")
        assert obs["count"] == 3
        assert obs["sum"] == 9.0
        assert obs["min"] == 1.0
        assert obs["max"] == 5.0
        assert obs["last"] == 3.0
        cm.reset()

    def test_reclaimed_expired_ignores_zero_and_none(self):
        cm.reset()
        cm.record_queue_reclaimed_expired(0)
        cm.record_queue_reclaimed_expired(None)
        assert cm.get_counter(cm.QUEUE_RECLAIMED_EXPIRED_TOTAL) == 0
        cm.reset()
