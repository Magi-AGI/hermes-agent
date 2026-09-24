"""Codex ownership operations: PAYLOAD -> DECISION -> APPLY -> COMMIT (design rev 9 §4).

Every ownership change runs under the migration lock + the store locks of its scope (RULE-L1):

1. PAYLOAD  ``<root>/auth.ops/<op_id>.json`` (strict writer, COMMITTED required): everything recovery
            needs — new credentials, the exact row edits, record actions, the final registry entries.
2. DECISION one registry write adding ``ops[op_id]`` and the FINAL chain entries. This single atomic
            write IS the ownership decision.
3. APPLY    make every store and chain record match the payload; idempotent, conditional (RULE-M7).
4. COMMIT   one registry write removing ``ops[op_id]``; the payload is archived (never pruned).

RULE-M1 recovery (any later migration-lock holder): a payload with no ``ops`` entry never decided ->
archived as ``abandoned``; an ``ops`` entry with its payload -> rolled FORWARD; an ``ops`` entry whose
payload is missing/corrupt -> every chain it touches is fenced ``op-unrecoverable`` (never guessed).

Fences requested by refresh transactions (RULE-R1) and legacy-rescue fences are single registry
writes (no store or record change), applied only while the request still matches.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from hermes_cli.auth_codex_records import (
    ATTEMPT_INDETERMINATE, archive_record, read_record, record_path, write_record)
from hermes_cli.auth_codex_registry import (
    OP_RUNNING, OP_UNRECOVERABLE, STATE_ACTIVE, STATE_FENCED, STATE_RETIRED, CodexAuthorityRefused,
    RegistryView, RepairRequest, empty_registry, migration_lock, migration_lock_held, new_id, ops_dir,
    read_registry, store_locks, store_path_of, utc_now, validate_registry, write_registry)
from hermes_cli.auth_credential_io import (
    encode_json, read_witnessed_bytes, require_committed, sha256_hex, write_credential_file)

logger = logging.getLogger("hermes_cli.auth")


@dataclass
class Payload:
    """What an ownership op decides. ``chains`` are FINAL registry entries for the touched chains."""

    kind: str
    scope_chains: Any                      # list of chain ids, or "*"
    scope_stores: List[str]
    scope_profiles: List[str] = field(default_factory=list)
    chains: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    legacy_rescues: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    edits: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    records: List[Dict[str, Any]] = field(default_factory=list)
    result: Dict[str, Any] = field(default_factory=dict)   # returned to the caller, not persisted
    op_id: str = field(default_factory=new_id)

    def to_json(self) -> Dict[str, Any]:
        return {"op_id": self.op_id, "kind": self.kind, "created_at": utc_now(),
                "scope": {"chains": self.scope_chains, "stores": self.scope_stores,
                          "profiles": self.scope_profiles},
                "chains": self.chains, "legacy_rescues": self.legacy_rescues,
                "edits": self.edits, "records": self.records}


def _payload_path(op_id: str) -> Path:
    return ops_dir() / f"{op_id}.json"


def _archive_payload(path: Path, label: str) -> None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = ops_dir() / "archive" / f"{path.stem}.{label}.{stamp}.json"
    require_committed(write_credential_file(target, raw), f"archive of ownership payload {path.name}")
    path.unlink()


# ── G4 reservations ───────────────────────────────────────────────────────────────────────────

def conflicting_op(view: RegistryView, chains: Any, stores: Iterable[str], profiles: Iterable[str],
                   continuing: Optional[str] = None) -> Optional[str]:
    """RULE-G4/G5: the first unfinished op (other than *continuing*) whose reservation intersects."""
    chains_set = set() if chains == "*" else set(chains or ())
    stores_set, profiles_set = set(stores), set(profiles)
    for op_id, op in view.ops.items():
        if op_id == continuing:
            continue
        op_chains = op.get("chains")
        if chains == "*" or op_chains == "*" or chains_set & set(op_chains or ()):
            return op_id
        if stores_set & set(op.get("stores") or ()) or profiles_set & set(op.get("profiles") or ()):
            return op_id
    return None


# ── DECISION / COMMIT ─────────────────────────────────────────────────────────────────────────

def _decided_registry(current: Dict[str, Any], payload: Payload, payload_sha: str) -> Dict[str, Any]:
    data = json.loads(json.dumps(current)) if current else empty_registry()
    data.setdefault("legacy_rescues", {})
    data["chains"].update(payload.chains)
    data["legacy_rescues"].update(payload.legacy_rescues)
    data["ops"][payload.op_id] = {
        "kind": payload.kind, "chains": payload.scope_chains, "stores": payload.scope_stores,
        "profiles": payload.scope_profiles, "payload_path": _payload_path(payload.op_id).name,
        "payload_sha": payload_sha, "phase": "decided", "direction": "forward", "status": OP_RUNNING}
    return data


def run_op(payload: Payload, view: RegistryView, *, continuing: Optional[str] = None) -> Payload:
    """PAYLOAD -> DECISION -> APPLY -> COMMIT. Caller holds the migration lock and the store locks
    of ``payload.scope_stores``; *view* is the registry read under the migration lock."""
    if not migration_lock_held():
        raise RuntimeError("ownership ops run only under the migration lock")
    blocking = conflicting_op(view, payload.scope_chains, payload.scope_stores, payload.scope_profiles,
                              continuing)
    if blocking is not None:
        raise CodexAuthorityRefused(
            f"Another Codex login update ({view.ops[blocking].get('kind')}, op {blocking}) is unfinished; "
            f"run `hermes auth migrate` to complete it, then try again.")
    body = payload.to_json()
    decided = _decided_registry(view.data if view.ok else {}, payload, "")
    validate_registry(decided)  # RULE-G2 checked BEFORE anything is written
    raw = encode_json(body)
    require_committed(write_credential_file(_payload_path(payload.op_id), raw),
                      f"ownership payload {payload.op_id}")
    write_registry(_decided_registry(view.data if view.ok else {}, payload, sha256_hex(raw)))
    apply_payload(body)
    commit_op(payload.op_id)
    return payload


def commit_op(op_id: str) -> None:
    view = read_registry()
    if view.ok and op_id in view.ops:
        data = dict(view.data)
        data["ops"] = {k: v for k, v in view.ops.items() if k != op_id}
        write_registry(data)
    _archive_payload(_payload_path(op_id), "committed")


# ── APPLY (idempotent, conditional) ───────────────────────────────────────────────────────────

def _apply_store_edits(path: Path, edits: List[Dict[str, Any]]) -> None:
    from hermes_cli import auth as auth_mod
    from hermes_cli import auth_codex_rows as rows
    store = auth_mod._load_auth_store(path)
    changed = False
    for edit in edits:
        kind = edit["op"]
        if kind == "write_row":
            changed |= rows.write_row(
                store, edit["row"], chain_id=edit["chain_id"], access=edit["access_token"],
                refresh=edit["refresh_token"], owned=edit.get("owned") or {},
                defaults=edit.get("defaults") or {}, reset_failure=bool(edit.get("reset_failure")))
        elif kind == "assign_id":
            changed |= rows.assign_id(store, edit["id"], edit["fp"])
        elif kind == "tag_row":
            changed |= rows.tag_row(store, edit["row"], edit["chain_id"], edit["fp"])
        elif kind == "remove_row":
            changed |= rows.remove_row(store, edit["row"], edit.get("chain_id"), edit.get("fp"))
        elif kind in ("suppress", "unsuppress"):
            changed |= _edit_suppression(store, edit["sources"], add=kind == "suppress")
        elif kind == "set_active_provider":
            current = (store.get("active_provider") or "").strip() if isinstance(store.get("active_provider"), str) else ""
            if not (edit.get("only_if_unset") and current) and store.get("active_provider") != edit.get("value"):
                store["active_provider"] = edit.get("value")
                changed = True
        else:  # pragma: no cover - payloads are written by this module
            raise ValueError(f"unknown store edit {kind}")
    if changed:
        auth_mod._save_auth_store(store, target_path=path)


def _edit_suppression(store: Dict[str, Any], sources: List[str], *, add: bool) -> bool:
    from hermes_cli.auth import _suppressed_source_list
    from hermes_cli.auth_codex_registry import PROVIDER
    suppressed = store.get("suppressed_sources")
    if not isinstance(suppressed, dict):
        if not add:
            return False
        suppressed = store["suppressed_sources"] = {}
    current = _suppressed_source_list(suppressed, PROVIDER)
    if current is None:
        if not add:
            return False
        current = suppressed[PROVIDER] = []
    changed = False
    for source in sources:
        if add and source not in current:
            current.append(source)
            changed = True
        elif not add and source in current:
            current.remove(source)
            changed = True
    if not add and not current:
        suppressed.pop(PROVIDER, None)
        if not suppressed:
            store.pop("suppressed_sources", None)
    return changed


def _apply_record_action(action: Dict[str, Any]) -> None:
    from hermes_cli.auth_codex_records import history_entry
    owner = store_path_of(action["owner"])
    cid, kind = action["chain_id"], action["action"]
    if kind == "archive":
        archive_record(owner, cid, action.get("reason", "retired"))
        return
    view = read_record(owner, cid)
    if kind == "create":
        if view.status == "ok" and view.data.get("epoch", 0) >= action["epoch"]:
            return
        entry = {"chain_id": cid, "attempt_id": None, "outcome": action["outcome"], "at": utc_now(),
                 "predecessor_fp": action["fp"], "predecessor_access_sha": action.get("access_sha"),
                 "predecessor_refresh_sha": action.get("refresh_sha")}
        if action.get("evidence"):
            entry["evidence"] = action["evidence"]
        record = {"chain_id": cid, "epoch": action["epoch"], "current_fp": action["fp"], "attempt": None,
                  "history": [entry]}
    elif kind == "set_epoch":
        if view.status != "ok" or view.data.get("epoch", 0) >= action["epoch"]:
            return
        record = {**view.data, "epoch": action["epoch"]}
    elif kind == "override":
        if view.status != "ok" or view.data.get("epoch", 0) >= action["epoch"]:
            return
        attempt = view.data.get("attempt") or {}
        if attempt.get("attempt_id") != action["for_attempt"] or attempt.get("state") != ATTEMPT_INDETERMINATE:
            return
        override = {"for_attempt": action["for_attempt"], "grant_id": action["grant_id"],
                    "by": "operator", "at": utc_now()}
        record = {**view.data, "epoch": action["epoch"], "attempt": {**attempt, "override": override},
                  "history": [*view.data.get("history", []), {
                      "chain_id": cid, "attempt_id": action["for_attempt"], "outcome": "override-granted",
                      "at": utc_now(), "evidence": {"grant_id": action["grant_id"],
                                                    "indeterminate_attempt": attempt}}]}
    else:  # pragma: no cover
        raise ValueError(f"unknown record action {kind}")
    require_committed(write_record(owner, record), f"chain record {cid}")


def apply_payload(body: Dict[str, Any]) -> None:
    stores = sorted(set(body["edits"]) | {a["owner"] for a in body["records"]})
    with store_locks([store_path_of(k) for k in stores]):
        for key, edits in body["edits"].items():
            if edits:
                _apply_store_edits(store_path_of(key), edits)
        for action in body["records"]:
            _apply_record_action(action)


# ── RULE-M1 recovery ──────────────────────────────────────────────────────────────────────────

def recover_ops() -> Dict[str, List[str]]:
    """Roll unfinished ops forward; archive undecided payloads. Caller holds the migration lock."""
    report: Dict[str, List[str]] = {"abandoned": [], "rolled_forward": [], "unrecoverable": []}
    view = read_registry()
    if view.status == "unreadable":
        return report  # never classify payloads against a registry we cannot read (RULE-G3)
    directory = ops_dir()
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if not view.ok or path.stem not in view.ops:
                _archive_payload(path, "abandoned")
                report["abandoned"].append(path.stem)
    if not view.ok:
        return report
    for op_id, op in list(view.ops.items()):
        if op.get("status") != OP_RUNNING:
            continue
        path = _payload_path(op_id)
        try:
            raw, sha = read_witnessed_bytes(path)
            body = json.loads(raw.decode("utf-8")) if raw is not None and sha == op.get("payload_sha") else None
        except Exception:
            body = None
        if body is None:
            _fence_unrecoverable(op_id, op)
            report["unrecoverable"].append(op_id)
            continue
        apply_payload(body)
        commit_op(op_id)
        report["rolled_forward"].append(op_id)
    return report


def _fence_unrecoverable(op_id: str, op: Dict[str, Any]) -> None:
    """Payload missing/corrupt: fence every chain the op touches; the op itself cannot finish, so
    its entry is closed (its chains stay fenced; a fresh sign-in is the exit)."""
    view = read_registry()
    data = json.loads(json.dumps(view.data))
    scope = op.get("chains")
    targets = list(data["chains"]) if scope == "*" else [c for c in scope or () if c in data["chains"]]
    for cid in targets:
        entry = data["chains"][cid]
        if entry.get("state") == STATE_ACTIVE:
            entry["state"] = STATE_FENCED
            entry["fence"] = {"reason": "op-unrecoverable", "epoch": entry["epoch"], "evidence": {"op_id": op_id}}
    data["ops"] = {k: v for k, v in data["ops"].items() if k != op_id}
    write_registry(data)
    path = _payload_path(op_id)
    if path.exists():
        _archive_payload(path, OP_UNRECOVERABLE)
    logger.warning("Codex ownership op %s could not be recovered; its chains are fenced", op_id)


# ── Fence decisions (single registry writes) ─────────────────────────────────────────────────

def apply_repair(request: RepairRequest) -> bool:
    """RULE-R1: fence the chain only while epoch, attempt and record sha still match."""
    with migration_lock():
        view = read_registry()
        entry = view.chain(request.chain_id) if view.ok else None
        if entry is None or entry.get("epoch") != request.epoch or entry.get("state") != STATE_ACTIVE:
            return False
        owner = store_path_of(request.owner)
        with store_locks([owner]):
            record = read_record(owner, request.chain_id)
            if record.sha != request.observed_record_sha:
                return False
            if request.attempt_id and (record.data.get("attempt") or {}).get("attempt_id") != request.attempt_id:
                return False
            data = json.loads(json.dumps(view.data))
            fence = {"reason": request.reason, "epoch": request.epoch}
            if request.attempt_id:
                fence["attempt_id"] = request.attempt_id
            if request.evidence:
                fence["evidence"] = request.evidence
            data["chains"][request.chain_id].update(state=STATE_FENCED, fence=fence)
            write_registry(data)
    logger.warning("Codex chain %s fenced (%s)", request.chain_id, request.reason)
    return True


def record_legacy_rescues(found: List[Any], candidates: Dict[str, Set[str]]) -> List[str]:
    """Fence the candidate chains of newly seen legacy rescue files and record the files."""
    view = read_registry()
    data = json.loads(json.dumps(view.data))
    fenced = []
    for store, sha in found:
        chains = sorted(candidates.get(store, set()))
        for cid in chains:
            entry = data["chains"].get(cid)
            if entry and entry.get("state") == STATE_ACTIVE:
                entry["state"] = STATE_FENCED
                entry["fence"] = {"reason": "legacy-rescue", "epoch": entry["epoch"],
                                  "evidence": {"rescue_store": store, "rescue_sha": sha}}
                fenced.append(cid)
        data["legacy_rescues"][store] = {"sha": sha, "chains": chains, "at": utc_now()}
    write_registry(data)
    return fenced


def retire_entry(entry: Dict[str, Any], remaining_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    return {**entry, "state": STATE_RETIRED, "epoch": entry["epoch"] + 1, "rows": remaining_rows,
            "fence": None, "lifecycle": None}
