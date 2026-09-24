"""Owner-local Codex chain records: where each chain's token rotation currently is.

Credential recovery protocol, stage 1 (design rev 9 §3). ``<owner store dir>/auth.recovery/
<chain_id>.json``, read and written ONLY under the owner store lock, with the strict writer::

    {chain_id, epoch, current_fp,
     attempt: null | {attempt_id, state: intent|pending|indeterminate|rejected, predecessor_fp,
                      successor?, successor_fp?, override?, override_used?},
     history: [...]}            # append-only, never pruned (decision 3)

RULE-F1 compares the COMPLETE pair fingerprint; RULE-F2 appends the evidence of every completion
(pair fingerprints + each token's hash) in the same write that clears the attempt, so the history
identifies every token value the chain ever held (import recognition, RULE-A1).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_cli.auth_codex_registry import RECORDS_DIR_NAME, pair_fp, token_sha, utc_now
from hermes_cli.auth_credential_io import (
    WriteResult, encode_json, read_bytes_retrying, read_witnessed_bytes, require_committed, unlink_retrying,
    write_credential_file)

logger = logging.getLogger("hermes_cli.auth")

ATTEMPT_INTENT, ATTEMPT_PENDING = "intent", "pending"
ATTEMPT_INDETERMINATE, ATTEMPT_REJECTED = "indeterminate", "rejected"


def records_dir(owner_store: Path) -> Path:
    return Path(owner_store).parent / RECORDS_DIR_NAME


def record_path(owner_store: Path, chain_id: str) -> Path:
    return records_dir(owner_store) / f"{chain_id}.json"


@dataclass
class RecordView:
    status: str  # "absent" | "unreadable" | "ok"
    data: Dict[str, Any] = field(default_factory=dict)
    sha: Optional[str] = None


def read_record(owner_store: Path, chain_id: str) -> RecordView:
    try:
        raw, sha = read_witnessed_bytes(record_path(owner_store, chain_id))
    except OSError:
        return RecordView("unreadable")
    if raw is None:
        return RecordView("absent")
    try:
        data = json.loads(raw.decode("utf-8"))
        if not (isinstance(data, dict) and data.get("chain_id") == chain_id
                and isinstance(data.get("epoch"), int) and isinstance(data.get("history"), list)):
            raise ValueError("bad record shape")
    except Exception:
        return RecordView("unreadable", sha=sha)
    return RecordView("ok", data, sha)


def write_record(owner_store: Path, data: Dict[str, Any]) -> WriteResult:
    return write_credential_file(record_path(owner_store, data["chain_id"]), encode_json(data))


def archive_record(owner_store: Path, chain_id: str, reason: str) -> Optional[Path]:
    """RULE-A3: retirement archives the record + history (never pruned). Idempotent."""
    src = record_path(owner_store, chain_id)
    try:
        raw = read_bytes_retrying(src)
    except FileNotFoundError:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = records_dir(owner_store) / "archive" / f"{chain_id}.{reason}.{stamp}.json"
    require_committed(write_credential_file(target, raw), f"archive of chain record {chain_id}")
    unlink_retrying(src)
    return target


def token_evidence(prefix: str, access: Any, refresh: Any) -> Dict[str, Any]:
    return {f"{prefix}_fp": pair_fp(access, refresh), f"{prefix}_access_sha": token_sha(access),
            f"{prefix}_refresh_sha": token_sha(refresh)}


def history_entry(chain_id: str, outcome: str, predecessor: Tuple[str, str], *,
                  attempt_id: Optional[str] = None, successor: Optional[Tuple[str, str]] = None,
                  evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"chain_id": chain_id, "attempt_id": attempt_id, "outcome": outcome, "at": utc_now()}
    entry.update(token_evidence("predecessor", *predecessor))
    if successor is not None:
        entry.update(token_evidence("successor", *successor))
    if evidence:
        entry["evidence"] = evidence
    return entry


def new_record(chain_id: str, epoch: int, pair: Tuple[str, str], outcome: str,
               evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"chain_id": chain_id, "epoch": epoch, "current_fp": pair_fp(*pair), "attempt": None,
            "history": [history_entry(chain_id, outcome, pair, evidence=evidence)]}


def known_token_shas(record: Dict[str, Any]) -> Tuple[set, set]:
    """Every access / refresh token hash the chain ever held (history + pending predecessor)."""
    access, refresh = set(), set()
    for entry in record.get("history") or ():
        for prefix in ("predecessor", "successor"):
            if entry.get(f"{prefix}_access_sha"):
                access.add(entry[f"{prefix}_access_sha"])
            if entry.get(f"{prefix}_refresh_sha"):
                refresh.add(entry[f"{prefix}_refresh_sha"])
    return access, refresh


# ── RULE-S1 / S2 reconcile ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Reconcile:
    action: str            # "normal" | "complete" | "adopt" | "fence" | "override"
    reason: Optional[str] = None


def reconcile(view: RecordView, chain_epoch: int, rows_fp: Optional[str]) -> Reconcile:
    """Compare ONLY owner-local values (the record and the rows in the owner store)."""
    if view.status != "ok":
        return Reconcile("fence", "unreadable-record")
    record = view.data
    if record.get("epoch") != chain_epoch:
        return Reconcile("fence", "unreadable-record")  # S1: an older ownership's record
    attempt = record.get("attempt")
    if not attempt:
        return Reconcile("normal") if rows_fp == record.get("current_fp") else Reconcile("fence", "unexpected-token")
    state = attempt.get("state")
    if state == ATTEMPT_PENDING:
        if rows_fp is not None and rows_fp == attempt.get("successor_fp"):
            return Reconcile("complete")
        if rows_fp is not None and rows_fp == attempt.get("predecessor_fp"):
            return Reconcile("adopt")
        return Reconcile("fence", "unexpected-token")
    if state == ATTEMPT_INDETERMINATE:
        override = attempt.get("override")
        if (isinstance(override, dict) and override.get("for_attempt") == attempt.get("attempt_id")
                and rows_fp == record.get("current_fp")):
            return Reconcile("override")
        return Reconcile("fence", "indeterminate")
    if state == ATTEMPT_REJECTED:
        return Reconcile("fence", "rejected")
    return Reconcile("fence", "indeterminate")  # an intent found at start: outcome unknown


def completed(record: Dict[str, Any], attempt: Dict[str, Any], outcome: str) -> Dict[str, Any]:
    """RULE-F2: clear the attempt AND append its evidence in one record value."""
    succ = attempt.get("successor") or {}
    entry: Dict[str, Any] = {
        "chain_id": record["chain_id"], "attempt_id": attempt.get("attempt_id"), "outcome": outcome,
        "at": utc_now(), "predecessor_fp": attempt.get("predecessor_fp"),
        "predecessor_access_sha": attempt.get("predecessor_access_sha"),
        "predecessor_refresh_sha": attempt.get("predecessor_refresh_sha")}
    entry.update(token_evidence("successor", succ.get("access_token", ""), succ.get("refresh_token", "")))
    if attempt.get("override_used"):
        entry["evidence"] = {"override_used": attempt["override_used"]}
    return {**record, "current_fp": attempt.get("successor_fp"), "attempt": None,
            "history": [*record.get("history", []), entry]}


def new_attempt(attempt_id: str, pair: Tuple[str, str], *, override_used: Optional[str] = None) -> Dict[str, Any]:
    """The intent written before any byte goes to the token endpoint."""
    attempt = {"attempt_id": attempt_id, "state": ATTEMPT_INTENT, "at": utc_now(),
               "predecessor_fp": pair_fp(*pair), "predecessor_access_sha": token_sha(pair[0]),
               "predecessor_refresh_sha": token_sha(pair[1])}
    if override_used:
        attempt["override_used"] = override_used
    return attempt
