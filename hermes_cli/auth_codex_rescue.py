"""Legacy rescue files: archival and evidence-based unfencing (design rev 9.1 §7, stage 2).

The withdrawn rev-0 code kept a rotated pair it could not persist in
``auth.codex-rotation-rescue.json`` beside a store (``{"entries": {row: {access_token,
refresh_token}}}``). Stage 1 fences every candidate chain (owned OR borrowed by that store) the
moment such a file is seen, and never touches the file. Stage 2 settles each RECORDED file:

1. **Verdict** from independent evidence only: ``stale`` when every rescued pair equals the CURRENT
   owner rows of one of the file's candidate chains (the store already holds what the rescue
   saved: nothing diverged), else ``divergent`` (an unreadable / malformed file included). No
   predecessor is ever synthesized and a rescued pair is never written anywhere.
2. **Unfence** (an ownership op, epoch +1): a chain fenced ``legacy-rescue`` whose every recorded
   rescue is ``stale`` becomes active again; any ``divergent`` rescue keeps it fenced until a fresh
   sign-in.
3. **Archive** the file as ``untrusted`` in ``<root>/auth.ops/archive/`` (protected from restores,
   never read back): the exact bytes read are written with the strict writer (COMMITTED), and the
   original name is removed only if it still holds those bytes. Rescue files beside symlinked alias
   paths are found by ``rescue_locations`` like every other.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_cli import auth_codex_rows as rows_mod
from hermes_cli.auth_codex_registry import (
    STATE_ACTIVE, STATE_FENCED, RegistryView, RescueScanIncomplete, ops_dir, pair_fp, read_registry,
    rescue_locations, store_locks)
from hermes_cli.auth_credential_io import (
    read_witnessed_bytes, require_committed, store_key, unlink_retrying, write_credential_file)

logger = logging.getLogger("hermes_cli.auth")

STALE, DIVERGENT = "stale", "divergent"


def rescued_pairs(raw: Optional[bytes]) -> Optional[List[Tuple[str, str]]]:
    """The pairs a rev-0 rescue file holds, or None when it is not exactly that shape."""
    try:
        data = json.loads(raw.decode("utf-8")) if raw is not None else None
    except Exception:
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        return None
    pairs = []
    for value in entries.values():
        if not isinstance(value, dict):
            return None
        access, refresh = str(value.get("access_token") or "").strip(), str(value.get("refresh_token") or "").strip()
        if not access or not refresh:
            return None
        pairs.append((access, refresh))
    return pairs


def verdict(raw: Optional[bytes], candidates: Set[str], view: RegistryView, snaps: Dict[str, Any]) -> str:
    """``stale`` iff every rescued pair equals the current owner rows of a candidate chain."""
    pairs = rescued_pairs(raw)
    if pairs is None:
        return DIVERGENT
    current = set()
    for cid in candidates:
        entry = view.chain(cid) or {}
        snap = snaps.get(entry.get("owner"))
        if snap is None:
            continue
        fps = {rows_mod.row_fp(snap.store, r["row"]) for r in entry.get("rows", ()) if r["store"] == entry["owner"]}
        if len(fps) == 1:
            current |= fps
    return STALE if all(pair_fp(*p) in current for p in pairs) else DIVERGENT


def _present_recorded(view: RegistryView) -> List[Tuple[Path, str, bytes, str]]:
    """``(file, rescue key, bytes, sha)`` of rescue files on disk whose content the registry has
    recorded (a new or changed file is the fence pass's first)."""
    known = view.data.get("legacy_rescues", {}) if view.ok else {}
    out = []
    try:
        locations = rescue_locations()
    except RescueScanIncomplete:
        return out
    for rescue, _store in locations:
        key = store_key(rescue)
        record = known.get(key)
        try:
            raw, sha = read_witnessed_bytes(rescue)
        except OSError:
            continue  # unreadable: stays in place, its chains stay fenced
        if raw is None or record is None or record.get("sha") != sha:
            continue
        out.append((rescue, key, raw, sha))
    return out


def _unfence_candidates(view: RegistryView, records: Dict[str, Dict[str, Any]]) -> List[str]:
    """Chains fenced ``legacy-rescue`` whose EVERY recorded rescue has the verdict ``stale`` (a
    record without a verdict, e.g. of a file deleted before it was settled, keeps the fence), and
    that no unfinished op reserves."""
    from hermes_cli.auth_codex_ops import conflicting_op
    out = []
    for cid, entry in view.chains.items():
        fence = entry.get("fence") or {}
        if entry.get("state") != STATE_FENCED or fence.get("reason") != "legacy-rescue":
            continue
        listing = [r for r in records.values() if cid in (r.get("chains") or ())]
        if listing and all(r.get("verdict") == STALE for r in listing) and conflicting_op(view, [cid], [], []) is None:
            out.append(cid)
    return out


def settlement_needed(view: RegistryView) -> bool:
    if not view.ok:
        return False
    return bool(_present_recorded(view)) or bool(_unfence_candidates(view, view.data.get("legacy_rescues", {})))


def settle_legacy_rescues() -> Dict[str, List[str]]:
    """Verdicts, evidence-based unfencing and archival of recorded rescue files. Caller holds the
    migration lock and no store lock. Idempotent at every step (a crash anywhere replays)."""
    from hermes_cli.auth_codex_authority import lockable_stores
    from hermes_cli.auth_codex_ops import Payload, run_op
    from hermes_cli.auth_codex_plans import load_snapshots
    from hermes_cli.auth_codex_registry import all_store_paths
    report: Dict[str, List[str]] = {"unfenced": [], "archived": []}
    view = read_registry()
    if not view.ok:
        return report
    present = _present_recorded(view)
    records = json.loads(json.dumps(view.data.get("legacy_rescues", {})))
    payload = Payload(kind="legacy-rescue-settle", scope_chains=[], scope_stores=[])
    pending = [(key, raw) for _rescue, key, raw, _sha in present if "verdict" not in records[key]]
    paths = lockable_stores(all_store_paths())
    with store_locks(paths):
        if pending:
            snaps = load_snapshots(paths)
            for key, raw in pending:
                candidates = set(records[key].get("chains") or ())
                records[key] = payload.legacy_rescues[key] = {
                    **records[key], "verdict": verdict(raw, candidates, view, snaps)}
        for cid in _unfence_candidates(view, records):
            entry = view.chains[cid]
            payload.chains[cid] = {**entry, "state": STATE_ACTIVE, "fence": None, "epoch": entry["epoch"] + 1}
            payload.records.append({"action": "set_epoch", "owner": entry["owner"], "chain_id": cid,
                                    "epoch": entry["epoch"] + 1})
            payload.scope_chains.append(cid)
            report["unfenced"].append(cid)
        if payload.legacy_rescues or payload.chains:
            run_op(payload, view)
    for rescue, key, raw, sha in present:
        archive_rescue(rescue, raw, sha)
        report["archived"].append(key)
    if report["unfenced"]:
        logger.info("Codex: %d login(s) paused for a leftover rescue file resumed (the file matched the "
                    "saved login)", len(report["unfenced"]))
    return report


def archive_rescue(rescue: Path, raw: bytes, sha: str) -> Path:
    """Copy the exact bytes read to the untrusted archive, then remove the original name only while
    it still holds those bytes (never a blind unlink of whatever is there now)."""
    target = ops_dir() / "archive" / f"legacy-rescue.{sha[:24]}.untrusted.json"
    try:
        existing, existing_sha = read_witnessed_bytes(target)
    except OSError:
        existing_sha = None
    if existing_sha != sha:
        require_committed(write_credential_file(target, raw), f"archive of legacy rescue file {rescue}")
    try:
        _now, now_sha = read_witnessed_bytes(rescue)
    except OSError:
        return target
    if now_sha == sha:
        unlink_retrying(rescue)
    return target
