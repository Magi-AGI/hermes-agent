"""RULE-S1/S2: the owner-local reconcile table, row by row (design rev 9 §3).

Only owner-local values are compared — the record and the PAIR fingerprint the rows hold — so an
unchanged refresh token (successor refresh == predecessor refresh) still tells "rows updated" from
"rows still hold the old pair" by the access token (RULE-F1).
"""

import pytest

from hermes_cli.auth_codex_records import RecordView, reconcile
from hermes_cli.auth_codex_registry import pair_fp

OLD = pair_fp("access-0", "refresh-0")
NEW_SAME_REFRESH = pair_fp("access-1", "refresh-0")
OTHER = pair_fp("access-x", "refresh-x")


def _view(attempt=None, epoch=3, current=OLD):
    return RecordView("ok", {"chain_id": "c", "epoch": epoch, "current_fp": current, "attempt": attempt,
                             "history": []}, "sha")


def _pending(**extra):
    return {"attempt_id": "a1", "state": "pending", "predecessor_fp": OLD, "successor_fp": NEW_SAME_REFRESH,
            **extra}


@pytest.mark.parametrize("view, rows_fp, expected", [
    (_view(), OLD, ("normal", None)),
    (_view(), OTHER, ("fence", "unexpected-token")),
    (_view(_pending()), NEW_SAME_REFRESH, ("complete", None)),
    (_view(_pending()), OLD, ("adopt", None)),            # same refresh token, different access
    (_view(_pending()), OTHER, ("fence", "unexpected-token")),
    (_view({"attempt_id": "a1", "state": "intent", "predecessor_fp": OLD}), OLD, ("fence", "indeterminate")),
    (_view({"attempt_id": "a1", "state": "indeterminate", "predecessor_fp": OLD}), OLD, ("fence", "indeterminate")),
    (_view({"attempt_id": "a1", "state": "indeterminate", "predecessor_fp": OLD,
            "override": {"for_attempt": "a1", "grant_id": "g"}}), OLD, ("override", None)),
    (_view({"attempt_id": "a1", "state": "indeterminate", "predecessor_fp": OLD,
            "override": {"for_attempt": "a0", "grant_id": "g"}}), OLD, ("fence", "indeterminate")),
    (RecordView("unreadable"), OLD, ("fence", "unreadable-record")),
    (_view(epoch=2), OLD, ("fence", "unreadable-record")),  # S1: an older ownership's record
])
def test_reconcile_table(view, rows_fp, expected):
    decision = reconcile(view, 3, rows_fp)
    assert (decision.action, decision.reason) == expected
