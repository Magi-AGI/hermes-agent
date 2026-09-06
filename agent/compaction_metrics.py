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
from typing import Dict, Tuple

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

# name -> {label-tuple: count}
_counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], int]] = {}
_lock = threading.Lock()


def _increment(name: str, labels: Dict[str, str]) -> None:
    key = tuple(sorted(labels.items()))
    with _lock:
        _counters.setdefault(name, {})
        _counters[name][key] = _counters[name].get(key, 0) + 1


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


def get_counter(name: str, **labels: str) -> int:
    """Return the current value for one labelled counter (0 if never incremented)."""
    key = tuple(sorted(labels.items()))
    with _lock:
        return _counters.get(name, {}).get(key, 0)


def snapshot() -> Dict[str, Dict[Tuple[Tuple[str, str], ...], int]]:
    """Return a deep copy of all counters — for diagnostics and tests."""
    with _lock:
        return {name: dict(series) for name, series in _counters.items()}


def reset() -> None:
    """Clear all counters. Test-support only; never called on the runtime path."""
    with _lock:
        _counters.clear()
