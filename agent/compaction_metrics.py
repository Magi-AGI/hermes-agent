"""Process-local counters for the compaction route guard (spec §7).

Hermes has **no metrics service** — there is no statsd/Prometheus/OTel sink in
the repo, and this module deliberately does NOT introduce one. It is the
smallest hook that satisfies the spec's observability requirement: labelled,
monotonic, in-process counters that

  * the existing INFO/WARN logs continue to sit alongside (logs are unchanged —
    these counters are additive), and
  * diagnostics / tests can read as a snapshot.

Counters are per-process and reset on restart. That is sufficient for their
stated purpose: telling an operator that a guard event *happened at all*
(``{provider=anthropic, reason=metered_auth_mode}`` firing at least once means
the §5.2 dual-route API-key fallback was hit in production), and giving tests a
non-log assertion surface. If a real metrics sink is ever added, the two
``record_*`` functions below are the single place to forward from.

Counters (spec §7):

* ``compaction.route_rejected_total{provider, reason}`` — the guard REFUSED a
  constructed/effective route. ``reason`` ∈ ``not_allowlisted`` |
  ``metered_auth_mode`` | ``endpoint_mismatch``. Ends that route.
* ``compaction.credential_candidate_skipped_total{provider, source, reason}`` —
  a credential candidate was SKIPPED during the scan-past walk (§5.3.1) while
  still looking for a subscription credential. ``reason`` = ``metered_shape``.
  Deliberately distinct from a rejection: a skip means the search *continued*,
  so a non-zero skip count alongside a SUCCESSFUL compaction is benign — it just
  tells the user their ``ANTHROPIC_TOKEN`` is metered-shaped and inert for
  compaction.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Tuple

ROUTE_REJECTED_TOTAL = "compaction.route_rejected_total"
CREDENTIAL_CANDIDATE_SKIPPED_TOTAL = "compaction.credential_candidate_skipped_total"

# Normalised reason labels — keep in sync with the guard's call sites.
REASON_NOT_ALLOWLISTED = "not_allowlisted"
REASON_METERED_AUTH_MODE = "metered_auth_mode"
REASON_ENDPOINT_MISMATCH = "endpoint_mismatch"
REASON_METERED_SHAPE = "metered_shape"

_VALID_REJECT_REASONS = frozenset(
    {REASON_NOT_ALLOWLISTED, REASON_METERED_AUTH_MODE, REASON_ENDPOINT_MISMATCH}
)

# ── Compaction QUEUE metrics (spec §7) ──────────────────────────────────────
#
# These describe the cross-session queue, and are distinct from the route-guard
# counters above (which describe the PRIVACY guard). Nothing here fires when the
# queue is disabled — a disabled queue must be provably inert, and a metric
# claiming the queue "ran" would be a lie an operator could act on.

QUEUE_SLOTS_IN_USE = "compaction.queue.slots_in_use"      # gauge
QUEUE_SLOTS_MAX = "compaction.queue.slots_max"            # gauge
QUEUE_WAIT_SECONDS = "compaction.queue.wait_seconds"      # observation, by source
QUEUE_DENIED_TOTAL = "compaction.queue.denied_total"      # counter
QUEUE_FAILOPEN_TOTAL = "compaction.queue.failopen_total"  # counter, by reason
QUEUE_RECLAIMED_EXPIRED_TOTAL = "compaction.queue.reclaimed_expired_total"

# Fail-open reasons. `coordinator_error` MUST be alertable: it means the queue is
# silently NOT queueing — every session bypasses and compacts unbounded, which
# looks identical to a healthy idle queue from the outside. That is the single
# most important signal in this module.
REASON_HARDWALL = "hardwall"
REASON_WAITCAP = "waitcap"
REASON_COORDINATOR_ERROR = "coordinator_error"

_VALID_FAILOPEN_REASONS = frozenset(
    {REASON_HARDWALL, REASON_WAITCAP, REASON_COORDINATOR_ERROR}
)

# name -> {label-tuple: count}
_counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], int]] = {}
# name -> {label-tuple: last value}  (point-in-time, e.g. slots in use)
_gauges: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
# name -> {label-tuple: {count,sum,min,max,last}}  (e.g. wait seconds)
_observations: Dict[str, Dict[Tuple[Tuple[str, str], ...], Dict[str, float]]] = {}
_lock = threading.Lock()


def _key(labels: Optional[Dict[str, str]] = None) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted((labels or {}).items()))


def _increment(name: str, labels: Dict[str, str], by: int = 1) -> None:
    key = _key(labels)
    with _lock:
        _counters.setdefault(name, {})
        _counters[name][key] = _counters[name].get(key, 0) + int(by)


def _set_gauge(name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
    key = _key(labels)
    with _lock:
        _gauges.setdefault(name, {})
        _gauges[name][key] = value


def _observe(name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
    key = _key(labels)
    value = float(value)
    with _lock:
        _observations.setdefault(name, {})
        cur = _observations[name].get(key)
        if cur is None:
            _observations[name][key] = {
                "count": 1, "sum": value, "min": value, "max": value, "last": value,
            }
        else:
            cur["count"] += 1
            cur["sum"] += value
            cur["min"] = min(cur["min"], value)
            cur["max"] = max(cur["max"], value)
            cur["last"] = value


def record_route_rejected(provider: str, reason: str) -> None:
    """Count a refused compaction route. Must be alertable in any future sink."""
    if reason not in _VALID_REJECT_REASONS:
        # Never drop the event just because a call site drifted — attribute it to
        # the closest bucket rather than silently losing a privacy signal.
        reason = REASON_NOT_ALLOWLISTED
    _increment(
        ROUTE_REJECTED_TOTAL,
        {"provider": provider or "unknown", "reason": reason},
    )


def record_credential_candidate_skipped(
    provider: str, source: str, reason: str = REASON_METERED_SHAPE,
) -> None:
    """Count a credential candidate skipped during the scan-past walk (§5.3.1)."""
    _increment(
        CREDENTIAL_CANDIDATE_SKIPPED_TOTAL,
        {
            "provider": provider or "unknown",
            "source": source or "unknown",
            "reason": reason or REASON_METERED_SHAPE,
        },
    )


def record_queue_slot_load(
    slots_in_use: Optional[int], max_concurrent: Optional[int],
) -> None:
    """Record the queue depth OBSERVED BY A REAL TRANSACTION.

    Fed from the ``SlotResult`` of an acquire (ACQUIRED or DENIED) rather than by
    polling the DB, so a disabled queue never touches the coordinator just to fill
    a gauge — and so the gauge always reflects a genuine, committed observation.
    """
    if slots_in_use is not None:
        _set_gauge(QUEUE_SLOTS_IN_USE, float(slots_in_use))
    if max_concurrent is not None:
        _set_gauge(QUEUE_SLOTS_MAX, float(max_concurrent))


def record_queue_wait_seconds(seconds: float, source: str = "") -> None:
    """Observe how long a session waited before it got in (or gave up).

    Labelled by ``source`` because the source split is exactly what would later
    justify — or refute — a priority scheduler (spec §4.6/§9.5). Without it, a
    starving interactive session is indistinguishable from a patient background one.
    """
    _observe(QUEUE_WAIT_SECONDS, max(0.0, float(seconds)), {"source": source or "unknown"})


def record_queue_denied(source: str = "") -> None:
    """Count a genuine DENIED — a SUCCESSFUL observation that the queue is full.

    Never incremented for a coordinator error: that is a fail-open bypass, not a
    denial, and conflating them would hide a broken queue behind "looks busy".
    """
    _increment(QUEUE_DENIED_TOTAL, {"source": source or "unknown"})


def record_queue_failopen(reason: str) -> None:
    """Count a bypass of the queue's bound.

    ``coordinator_error`` must be ALERTABLE — it means the queue is silently not
    queueing. ``hardwall``/``waitcap`` are deliberate, healthy escape hatches.
    """
    if reason not in _VALID_FAILOPEN_REASONS:
        reason = REASON_COORDINATOR_ERROR  # never silently drop a fail-open signal
    _increment(QUEUE_FAILOPEN_TOTAL, {"reason": reason})


def record_queue_reclaimed_expired(count: Optional[int]) -> None:
    """Count slots reclaimed from crashed/expired holders. No-op for 0/None."""
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        return
    if n > 0:
        _increment(QUEUE_RECLAIMED_EXPIRED_TOTAL, {}, by=n)


def get_counter(name: str, **labels: str) -> int:
    """Return the current value for one labelled counter (0 if never incremented)."""
    with _lock:
        return _counters.get(name, {}).get(_key(labels), 0)


def get_gauge(name: str, **labels: str) -> Optional[float]:
    """Return the last recorded gauge value, or None if never recorded."""
    with _lock:
        return _gauges.get(name, {}).get(_key(labels))


def get_observation(name: str, **labels: str) -> Optional[Dict[str, float]]:
    """Return {count,sum,min,max,last} for an observation series, or None."""
    with _lock:
        series = _observations.get(name, {}).get(_key(labels))
        return dict(series) if series is not None else None


def snapshot() -> Dict[str, Dict]:
    """Deep copy of every metric — counters, gauges and observations.

    Flat ``name -> {label-tuple: value}`` so it stays compatible with the existing
    route-guard assertions (which read counters by name, and assert emptiness when
    nothing has been recorded). Metric names are unique across the three kinds, so
    merging them cannot collide.
    """
    with _lock:
        out: Dict[str, Dict] = {}
        for name, series in _counters.items():
            out[name] = dict(series)
        for name, series in _gauges.items():
            out[name] = dict(series)
        for name, series in _observations.items():
            out[name] = {k: dict(v) for k, v in series.items()}
        return out


def reset() -> None:
    """Clear ALL metrics. Test-support only; never called on the runtime path."""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _observations.clear()
