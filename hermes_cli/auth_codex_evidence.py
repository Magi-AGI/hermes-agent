"""Durable recognition of Codex token material Hermes has ever held (RULE-A1, RULE-A3).

A retired chain's record is archived (RULE-A3) and ``hermes auth recovery clear`` may delete the
archive, so recognition cannot rest on live records alone: a consumed predecessor of a replaced or
logged-out login would then look like a brand-new authorization. The **retired-token index**
``<root>/auth.chains.retired.json`` keeps, per retired chain, every access / refresh token hash and
pair fingerprint its record ever listed. It is written only under the migration lock with the
strict writer, is not an archive (``recovery clear`` never removes it) and is never pruned.

Evidence is FAIL-CLOSED: a live chain whose record is missing or unreadable, a retired chain with
no index entry, an index entry recorded without its record, or an unreadable index all make the
evidence INCOMPLETE — and incomplete evidence never establishes that material is new.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hermes_cli.auth_codex_records import RecordView, read_record, records_dir
from hermes_cli.auth_codex_registry import (
    STATE_RETIRED, RegistryView, migration_lock_held, pair_fp, root_home, store_path_of, token_sha, utc_now)
from hermes_cli.auth_credential_io import (
    encode_json, read_bytes_retrying, read_witnessed_bytes, require_committed, write_credential_file)

logger = logging.getLogger("hermes_cli.auth")

RETIRED_INDEX_NAME = "auth.chains.retired.json"
INDEX_VERSION = 1


def retired_index_path() -> Path:
    return root_home() / RETIRED_INDEX_NAME


@dataclass
class Evidence:
    access: set = field(default_factory=set)
    refresh: set = field(default_factory=set)
    fps: set = field(default_factory=set)
    retired_access: set = field(default_factory=set)
    retired_refresh: set = field(default_factory=set)
    retired_fps: set = field(default_factory=set)
    gaps: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.gaps

    def retired(self, access: str, refresh: str) -> bool:
        return (pair_fp(access, refresh) in self.retired_fps or token_sha(access) in self.retired_access
                or token_sha(refresh) in self.retired_refresh)

    def knows(self, access: str, refresh: str) -> bool:
        return self.retired(access, refresh) or pair_fp(access, refresh) in self.fps or (
            token_sha(access) in self.access or token_sha(refresh) in self.refresh)


def record_material(record: Dict[str, Any]) -> Dict[str, set]:
    """Every token hash / pair fingerprint a chain record lists (history, current, attempt)."""
    out: Dict[str, set] = {"access": set(), "refresh": set(), "fps": set()}

    def add(prefix: str, src: Dict[str, Any]) -> None:
        for kind, key in (("access", f"{prefix}_access_sha"), ("refresh", f"{prefix}_refresh_sha"),
                          ("fps", f"{prefix}_fp")):
            if src.get(key):
                out[kind].add(src[key])

    if record.get("current_fp"):
        out["fps"].add(record["current_fp"])
    for entry in record.get("history") or ():
        if isinstance(entry, dict):
            add("predecessor", entry)
            add("successor", entry)
    attempt = record.get("attempt") or {}
    if isinstance(attempt, dict):
        add("predecessor", attempt)
        succ = attempt.get("successor") or {}
        if isinstance(succ, dict) and succ.get("access_token"):
            out["access"].add(token_sha(succ.get("access_token")))
            out["refresh"].add(token_sha(succ.get("refresh_token")))
            out["fps"].add(pair_fp(succ.get("access_token"), succ.get("refresh_token")))
    for kind in out:
        out[kind].discard(None)
    return out


# ── the index ─────────────────────────────────────────────────────────────────────────────────

def read_index() -> tuple:
    """``(status, data)``: ``absent`` / ``ok`` / ``unreadable``."""
    try:
        raw, _sha = read_witnessed_bytes(retired_index_path())
    except OSError:
        return "unreadable", {}
    if raw is None:
        return "absent", {}
    try:
        data = json.loads(raw.decode("utf-8"))
        if not (isinstance(data, dict) and data.get("version") == INDEX_VERSION
                and isinstance(data.get("chains"), dict)):
            raise ValueError("bad index shape")
    except Exception:
        return "unreadable", {}
    return "ok", data


def _writable_index() -> Dict[str, Any]:
    """The index to merge into. An unreadable one is preserved and replaced by one that says its
    earlier evidence is lost (``complete: false``), so recognition fails closed from then on."""
    status, data = read_index()
    if status == "ok":
        return data
    fresh = {"version": INDEX_VERSION, "complete": True, "chains": {}}
    if status == "unreadable":
        path = retired_index_path()
        try:
            raw = read_bytes_retrying(path)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            require_committed(write_credential_file(path.with_name(f"{path.name}.corrupt.{stamp}"), raw),
                              "copy of the damaged retired-token index")
        except OSError:
            logger.warning("Codex retired-token index %s is unreadable and could not be copied", path)
        fresh["complete"] = False
        fresh["lost_at"] = utc_now()
    return fresh


def retain(entries: Dict[str, Dict[str, Any]]) -> None:
    """Merge ``{chain_id: {"access": set, "refresh": set, "fps": set, "complete": bool, "reason"}}``
    into the index (idempotent; COMMITTED required). Caller holds the migration lock."""
    if not migration_lock_held():
        raise RuntimeError("the retired-token index is written only under the migration lock")
    if not entries:
        return
    data = _writable_index()
    changed = False
    for cid, material in entries.items():
        slot = data["chains"].setdefault(cid, {"access": [], "refresh": [], "fps": [], "complete": True,
                                               "reason": material.get("reason"), "at": utc_now()})
        for kind in ("access", "refresh", "fps"):
            merged = sorted(set(slot.get(kind) or ()) | set(material.get(kind) or ()))
            if merged != slot.get(kind):
                slot[kind] = merged
                changed = True
        if not material.get("complete", True) and slot.get("complete", True):
            slot["complete"] = False
            changed = True
    if changed or not retired_index_path().exists():
        require_committed(write_credential_file(retired_index_path(), encode_json(data)),
                          "the Codex retired-token index")


def material_of(record_view: Optional[RecordView], row_material: Iterable[Dict[str, Any]] = (),
                reason: str = "retired") -> Dict[str, Any]:
    """Index entry for one chain: its record's material (``complete`` only if the record read ok)
    plus the token hashes of the rows it held when retired."""
    out: Dict[str, Any] = {"access": set(), "refresh": set(), "fps": set(), "reason": reason,
                           "complete": record_view is not None and record_view.status == "ok"}
    if out["complete"]:
        for kind, values in record_material(record_view.data).items():
            out[kind] |= values
    for row in row_material:
        for kind in ("access", "refresh", "fps"):
            if row.get(kind):
                out[kind].add(row[kind])
    return out


def row_material(access: str, refresh: str) -> Dict[str, Optional[str]]:
    return {"access": token_sha(access), "refresh": token_sha(refresh), "fps": pair_fp(access, refresh)}


def archived_record(owner_store: Path, chain_id: str) -> Optional[RecordView]:
    """The newest archived copy of a chain record that still parses (RULE-A3 archives)."""
    directory = records_dir(owner_store) / "archive"
    for path in sorted(directory.glob(f"{chain_id}.*.json"), reverse=True) if directory.is_dir() else ():
        try:
            data = json.loads(read_bytes_retrying(path).decode("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("chain_id") == chain_id:
            return RecordView("ok", data)
    return None


def retain_chain(owner_store: Path, chain_id: str, rows: Iterable[Dict[str, Any]] = (),
                 reason: str = "retired") -> None:
    """Index a chain about to be archived (retirement / removal). Idempotent: a replay after the
    record was already archived keeps the entry the first application wrote."""
    view: Optional[RecordView] = read_record(owner_store, chain_id)
    if view.status == "absent":
        status, data = read_index()
        if status == "ok" and chain_id in data["chains"]:
            return
        view = archived_record(owner_store, chain_id) or view
    retain({chain_id: material_of(view, rows, reason)})


def retain_orphan_records(stores: Iterable[Path]) -> None:
    """Registry reconstruction: the damaged registry's chain ids are gone, so every record file
    beside a store (the old generation's live records) is folded into the index first."""
    entries: Dict[str, Dict[str, Any]] = {}
    for store in stores:
        from hermes_cli.auth_credential_io import listdir_strict
        directory = records_dir(store)
        # A records directory that cannot be listed raises: the rebuild must not index nothing.
        for path in [directory / n for n in listdir_strict(directory) if n.endswith(".json")]:
            view = read_record(store, path.stem)
            entries[f"rebuilt:{path.stem}"] = material_of(view, reason="registry-rebuilt")
    retain(entries)


# ── recognition ───────────────────────────────────────────────────────────────────────────────

def known_material(view: RegistryView) -> Evidence:
    """Everything Hermes knows about token material: live chain records + the retired index."""
    ev = Evidence()
    status, data = read_index()
    if status == "unreadable":
        ev.gaps.append("the retired-token index is unreadable")
    elif status == "ok":
        if not data.get("complete", True):
            ev.gaps.append("the retired-token index lost earlier evidence")
        for cid, slot in data["chains"].items():
            ev.retired_access |= set(slot.get("access") or ())
            ev.retired_refresh |= set(slot.get("refresh") or ())
            ev.retired_fps |= set(slot.get("fps") or ())
            if not slot.get("complete", True):
                ev.gaps.append(f"retired chain {cid[:8]} was retired without its record")
    indexed = set(data.get("chains") or ()) if status == "ok" else set()
    if not view.ok:
        ev.gaps.append(f"the authority registry is {view.status}")
        return ev
    for cid, entry in view.chains.items():
        retired = entry.get("state") == STATE_RETIRED
        if retired and cid in indexed:
            continue
        owner = store_path_of(entry["owner"])
        record = read_record(owner, cid)
        if retired and record.status == "absent":
            record = archived_record(owner, cid) or record
        if record.status != "ok":
            ev.gaps.append(f"chain {cid[:8]}: record {record.status}")
            continue
        material = record_material(record.data)
        if retired:
            ev.retired_access |= material["access"]
            ev.retired_refresh |= material["refresh"]
            ev.retired_fps |= material["fps"]
        else:
            ev.access |= material["access"]
            ev.refresh |= material["refresh"]
            ev.fps |= material["fps"]
    return ev
