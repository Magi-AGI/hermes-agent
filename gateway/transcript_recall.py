"""Settling recall (withdrawal) requests over every copy of a conversation.

A recall is first recorded durably (``SessionDB.open_recall_request``); from then on matching retry
copies are held from replay. ``settle_recall`` finishes it: it needs the COMPLETE set of copies waiting
for a retried write (the in-process retry queue, when there is one, and every spool file), gives each
undated candidate copy a durable timestamp so it has an identity replay will reproduce, and hands the
snapshot to ``SessionDB.resolve_recall``, which decides and records the outcome in one fenced
transaction. The same routine serves the live recall, a held live replay, boot recovery and the
gateway housekeeping tick, so no path completes a recall differently or depends on another to run.
"""

from __future__ import annotations

import itertools
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# A compression rotation between resolving the lineage and the resolution's transaction closes the
# tip it named; re-resolve and try again this many times before handing the retry to the caller.
_ROTATION_ATTEMPTS = 5
_STAMP_SEQ = itertools.count()

QueuedFor = Callable[[Sequence[str]], List[Dict[str, Any]]]


def message_platform_id(message: Dict[str, Any]) -> Optional[str]:
    return message.get("platform_message_id") or message.get("message_id") or None


def message_timestamp(message: Dict[str, Any]) -> Optional[float]:
    from hermes_cli.timefmt import coerce_epoch
    value = message.get("timestamp")
    epoch = coerce_epoch(value, field="message timestamp") if value is not None else None
    return None if epoch is None else float(epoch)


def _fresh_stamp() -> float:
    # Distinct per copy: two undated copies are two messages, never one identity.
    return time.time() + next(_STAMP_SEQ) % 1000 * 1e-6


def settle_recall(db, request: Dict[str, Any], *, queued_for: Optional[QueuedFor] = None) -> Tuple[int, str]:
    """Resolve one open request; ``(rows and copies covered, outcome)``, outcome ``resolved``, ``none``,
    ``ambiguous`` or a settled request's status. Raises ``TranscriptSpoolIncompleteError`` when any
    spooled copy cannot be read or stamped, and the SessionDB fence errors while a live turn or a
    compression holds the conversation: the request stays open and its copies held. *queued_for*
    returns the retry queue's user copies of the given sessions (caller holds the drain lock)."""
    from gateway.shutdown_flush import TranscriptSpoolIncompleteError, spooled_transcript_messages
    from hermes_state_errors import CompressionSessionClosedError
    from utils import atomic_json_write

    for attempt in range(_ROTATION_ATTEMPTS):
        lineage = db.recall_lineage(request["session_id"])
        queued = [m for m in (queued_for(lineage) if queued_for else []) if m.get("role") == "user"]
        spooled = [(path, payload, m) for path, payload, m in spooled_transcript_messages(lineage)
                   if m.get("role") == "user"]

        def candidate(message: Dict[str, Any]) -> bool:
            return db.recall_text_matches(request, message)

        for message in queued:
            if candidate(message) and message_timestamp(message) is None:
                message["timestamp"] = _fresh_stamp()  # written with it: the queue holds this dict
        for path, payload, message in spooled:
            if candidate(message) and message_timestamp(message) is None:
                message["timestamp"] = _fresh_stamp()
                try:
                    atomic_json_write(path, payload, mode=0o600, default=str)
                except OSError as exc:
                    raise TranscriptSpoolIncompleteError(f"cannot stamp {path.name}: {exc}") from exc
        copies = queued + [m for _path, _payload, m in spooled]
        snapshot = [(message_platform_id(m), candidate(m), message_timestamp(m)) for m in copies]
        try:
            changed, outcome, identity = db.resolve_recall(
                lineage, request_id=request["request_id"], retry_copies=snapshot)
            break
        except CompressionSessionClosedError:
            if attempt + 1 == _ROTATION_ATTEMPTS:
                raise
    if identity is not None:
        kind, key = identity
        changed += sum(1 for pid, text_matches, stamp in snapshot
                       if (pid == key if kind == "id" else (not pid and text_matches and stamp == key)))
    return changed, outcome


def settle_open_recalls(db, session_id: Optional[str] = None, *,
                        queued_for: Optional[QueuedFor] = None) -> List[Tuple[Dict[str, Any], str]]:
    """Try every open request (all, or *session_id*'s conversation); ``(request, outcome)`` for each one
    settled. A request still fenced or facing an unreadable spool file stays open for the next try."""
    from gateway.shutdown_flush import TranscriptSpoolIncompleteError
    from hermes_state import SessionCompressionInProgressError
    from hermes_state_errors import CompressionSessionClosedError, SessionTurnLeaseLostError
    settled = []
    for request in db.open_recall_requests(session_id):
        try:
            _changed, outcome = settle_recall(db, request, queued_for=queued_for)
        except (SessionTurnLeaseLostError, SessionCompressionInProgressError, CompressionSessionClosedError,
                TranscriptSpoolIncompleteError) as exc:
            logger.info("Recall %s in %s stays open: %s", request["request_id"], request["session_id"], exc)
            continue
        settled.append((request, outcome))
    return settled
