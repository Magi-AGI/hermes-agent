"""Settling recall (withdrawal) requests over every copy of a conversation.

A recall is first recorded durably (``SessionDB.open_recall_request``); from then on matching retry
copies are held from replay. ``settle_recall`` finishes it: it needs the COMPLETE set of copies waiting
for a retried write (the in-process retry queue, when there is one, and every spool file) and hands
their identities to ``SessionDB.resolve_recall``, which decides and records the outcome in one fenced
transaction. A copy that had no timestamp of its own is its own message, identified by a key that
travels with it to its eventual write (``shutdown_flush.COPY_KEY``: the spool file's name, or a key the
resolver gives a queued copy in memory); nothing is written back to a spool file. The same routine serves
the live recall, a held live replay, boot recovery and the gateway housekeeping tick.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# A compression rotation between resolving the lineage and the resolution's transaction closes the
# tip it named; re-resolve and try again this many times before handing the retry to the caller.
_ROTATION_ATTEMPTS = 5
QueuedFor = Callable[[Sequence[str]], List[Dict[str, Any]]]


def message_platform_id(message: Dict[str, Any]) -> Optional[str]:
    return message.get("platform_message_id") or message.get("message_id") or None


def message_timestamp(message: Dict[str, Any]) -> Optional[float]:
    from hermes_cli.timefmt import coerce_epoch
    value = message.get("timestamp")
    epoch = coerce_epoch(value, field="message timestamp") if value is not None else None
    return None if epoch is None else float(epoch)


def settle_recall(db, request: Dict[str, Any], *, queued_for: Optional[QueuedFor] = None) -> Tuple[int, str]:
    """Resolve one open request; ``(rows and copies covered, outcome)``, outcome ``resolved``, ``none``,
    ``ambiguous``, ``gone`` (its conversation was deleted) or a settled request's status. Raises
    ``TranscriptSpoolIncompleteError`` when any spooled copy cannot be read, and the SessionDB fence
    errors while a live turn or a compression holds the conversation: the request stays open and its
    copies held. *queued_for* returns the retry queue's user copies of the given sessions (caller holds
    the drain lock)."""
    from gateway.shutdown_flush import COPY_KEY, spooled_copy_key, spooled_transcript_messages
    from hermes_state_errors import CompressionSessionClosedError

    for attempt in range(_ROTATION_ATTEMPTS):
        lineage = db.recall_lineage(request["session_id"])
        snapshot = []
        for message in (queued_for(lineage) if queued_for else []):
            if message.get("role") != "user":
                continue
            if message_timestamp(message) is None and not message.get(COPY_KEY):
                message[COPY_KEY] = uuid.uuid4().hex  # the queue holds this dict: it is written with the key
            snapshot.append((message_platform_id(message), db.recall_text_matches(request, message),
                             message_timestamp(message), message.get(COPY_KEY)))
        for path, _payload, message in spooled_transcript_messages(lineage):
            if message.get("role") == "user":
                snapshot.append((message_platform_id(message), db.recall_text_matches(request, message),
                                 message_timestamp(message), spooled_copy_key(path, message)))
        try:
            changed, outcome, identity = db.resolve_recall(
                lineage, request_id=request["request_id"], retry_copies=snapshot)
        except CompressionSessionClosedError:
            outcome = "stale"
        if outcome != "stale":
            break
        if attempt + 1 == _ROTATION_ATTEMPTS:
            raise CompressionSessionClosedError(lineage[0])
    if identity is not None:
        kind, key = identity
        changed += sum(1 for pid, text_matches, stamp, copy_key in snapshot
                       if (pid == key if kind == "id" else copy_key == key if kind == "copy"
                           else (not pid and not copy_key and text_matches and stamp == key)))
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
