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
payload is missing/corrupt -> every chain it touches is fenced ``op-unrecoverable`` (never guessed) and
the entry is KEPT (status ``unrecoverable``, reservation intact) until its payload reads again or
``hermes auth recovery resolve`` releases it.

Fences requested by refresh transactions (RULE-R1) and legacy-rescue fences are single registry
writes (no store or record change), applied only while the request still matches.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from hermes_cli.auth_codex_records import (
    ATTEMPT_INDETERMINATE, archive_record, read_record, write_record)
from hermes_cli.auth_codex_registry import (
    OP_RUNNING, OP_STUCK, OP_UNRECOVERABLE, STATE_ACTIVE, STATE_FENCED, STATE_RETIRED, CodexAuthorityRefused,
    RegistryView, RepairRequest, empty_registry, migration_lock, migration_lock_held, new_id, ops_dir,
    read_registry, store_locks, store_path_of, utc_now, validate_registry, write_registry)
from hermes_cli.auth_credential_io import (
    encode_json, read_bytes_retrying, read_witnessed_bytes, require_committed, sha256_hex, unlink_retrying,
    write_credential_file)

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
    apply_order: List[str] = field(default_factory=list)   # stores applied first, in this order
    extra: Dict[str, Any] = field(default_factory=dict)    # kind-specific recovery data (persisted)

    def to_json(self) -> Dict[str, Any]:
        body = {"op_id": self.op_id, "kind": self.kind, "created_at": utc_now(),
                "scope": {"chains": self.scope_chains, "stores": self.scope_stores,
                          "profiles": self.scope_profiles},
                "chains": self.chains, "legacy_rescues": self.legacy_rescues,
                "edits": self.edits, "records": self.records}
        if self.apply_order:
            body["apply_order"] = self.apply_order
        if self.extra:
            body["extra"] = self.extra
        return body


def _payload_path(op_id: str) -> Path:
    return ops_dir() / f"{op_id}.json"


def _archive_payload(path: Path, label: str) -> None:
    try:
        raw = read_bytes_retrying(path)
    except FileNotFoundError:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = ops_dir() / "archive" / f"{path.stem}.{label}.{stamp}.json"
    require_committed(write_credential_file(target, raw), f"archive of ownership payload {path.name}")
    unlink_retrying(path)


# ── G4 reservations ───────────────────────────────────────────────────────────────────────────

def conflicting_op(view: RegistryView, chains: Any, stores: Iterable[str], profiles: Iterable[str]) -> Optional[str]:
    """RULE-G4/G5: the first unfinished op whose reservation intersects — except the op this context
    holds a validated continuation token for (``auth_codex_lifecycle.continuation``: same op, same
    direction as the entry has NOW)."""
    from hermes_cli.auth_codex_lifecycle import continues, lock_name
    chains_set = set() if chains == "*" else set(chains or ())
    stores_set, profiles_set = set(stores), {lock_name(p) for p in profiles}
    for op_id, op in view.ops.items():
        if continues(op_id, op):
            continue
        op_chains = op.get("chains")
        if chains == "*" or op_chains == "*" or chains_set & set(op_chains or ()):
            return op_id
        if stores_set & set(op.get("stores") or ()) or profiles_set & {lock_name(p) for p in op.get("profiles") or ()}:
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


def refuse_conflicts(view: RegistryView, chains: Any, stores: Iterable[str], profiles: Iterable[str],
                     action: str = "this change") -> None:
    """RULE-G4 fresh admission (continuation per RULE-G5): refused while an unfinished op's
    reservation intersects, with the command that resolves it."""
    blocking = conflicting_op(view, chains, stores, profiles)
    if blocking is not None:
        from hermes_cli.auth_codex_lifecycle import reservation_message
        raise CodexAuthorityRefused(reservation_message(view, blocking, action))


def decide_op(payload: Payload, view: RegistryView) -> Dict[str, Any]:
    """PAYLOAD -> DECISION (the caller runs APPLY and COMMIT). Caller holds the migration lock; *view*
    is the registry read under it. Returns the payload body as written."""
    if not migration_lock_held():
        raise RuntimeError("ownership ops run only under the migration lock")
    refuse_conflicts(view, payload.scope_chains, payload.scope_stores, payload.scope_profiles)
    body = payload.to_json()
    decided = _decided_registry(view.data if view.ok else {}, payload, "")
    validate_registry(decided)  # RULE-G2 checked BEFORE anything is written
    raw = encode_json(body)
    require_committed(write_credential_file(_payload_path(payload.op_id), raw),
                      f"ownership payload {payload.op_id}")
    write_registry(_decided_registry(view.data if view.ok else {}, payload, sha256_hex(raw)))
    return body


def run_op(payload: Payload, view: RegistryView) -> Payload:
    """PAYLOAD -> DECISION -> APPLY -> COMMIT. Caller holds the migration lock and the store locks
    of ``payload.scope_stores``; *view* is the registry read under the migration lock."""
    body = decide_op(payload, view)
    apply_payload(body)
    commit_op(payload.op_id)
    return payload


def commit_op(op_id: str, extra_archives: Iterable[Path] = ()) -> None:
    """COMMIT: one registry write removing ``ops[op_id]`` AND clearing the ``lifecycle`` exclusion it
    set on its chains (their ``state`` / ``fence`` are left exactly as they are, RULE-P2 step 5)."""
    view = read_registry()
    if view.ok and op_id in view.ops:
        data = json.loads(json.dumps(view.data))
        data["ops"].pop(op_id)
        for entry in data["chains"].values():
            if (entry.get("lifecycle") or {}).get("op_id") == op_id:
                entry["lifecycle"] = None
        write_registry(data)
    _archive_payload(_payload_path(op_id), "committed")
    for path in extra_archives:
        _archive_payload(path, "committed")


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
    owner = store_path_of(action["owner"])
    cid, kind = action["chain_id"], action["action"]
    if kind == "drop_moved":
        # RULE-M3: the record moved WITH the chain. The copy left here is archived (not indexed as
        # retired: the chain lives on) — and only once the destination holds the moved record.
        moved = read_record(store_path_of(action["moved_to"]), cid)
        if moved.status == "ok" and moved.data.get("epoch", 0) >= action["epoch"]:
            archive_record(owner, cid, "transferred")
        return
    if kind == "archive":
        # The record is about to leave the live set: its token material goes to the durable
        # retired-token index FIRST, so recognition never depends on the archive (RULE-A1/A3).
        from hermes_cli.auth_codex_evidence import retain_chain
        retain_chain(owner, cid, action.get("rows") or (), action.get("reason", "retired"))
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
    elif kind == "import_moved":
        # RULE-M3: the chain's record (attempt, fence evidence, full history) arrives unchanged but
        # for its epoch; a replay after it landed (or after newer writes there) changes nothing.
        if view.status == "ok" and view.data.get("epoch", 0) >= action["epoch"]:
            return
        record = {**action["record"], "epoch": action["epoch"]}
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
    """APPLY, one store at a time (RULE-L1 order: profile stores sorted, root last): each store's
    row edits and the chain records it owns are written under THAT store's lock only, so lock-
    taking readers of the other stores are not held for the whole op. Every step is conditional
    and idempotent (RULE-M7), so the per-store order is recovery-safe."""
    from hermes_cli.auth_codex_registry import is_root_store
    keys = set(body["edits"]) | {a["owner"] for a in body["records"]}
    first = [k for k in body.get("apply_order") or () if k in keys]
    rest = sorted(keys - set(first), key=lambda k: (is_root_store(k), k))
    for key in first + rest:  # a transfer writes the destination before it empties the source
        edits = body["edits"].get(key) or []
        actions = [a for a in body["records"] if a["owner"] == key]
        if _store_dir_gone(key) and all(e["op"] == "remove_row" for e in edits) and all(
                a["action"] in _REMOVAL_ACTIONS for a in actions):
            # The store's directory is gone (a profile removed meanwhile): its rows went with it and
            # there is nothing to lock. What it retires is still indexed (RULE-A3, fail closed).
            for action in actions:
                _apply_record_action(action)
            continue
        with store_locks([store_path_of(key)]):
            if edits:
                _apply_store_edits(store_path_of(key), edits)
            for action in actions:
                _apply_record_action(action)


_REMOVAL_ACTIONS = frozenset({"archive", "drop_moved"})


def _store_dir_gone(key: str) -> bool:
    """The store's directory is genuinely ABSENT (anything else — present, or not inspectable — is
    not gone: the normal locked path then decides)."""
    import errno
    import os
    try:
        os.stat(Path(key).parent)
    except OSError as exc:
        return isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno in (errno.ENOENT, errno.ENOTDIR)
    return False


# ── RULE-M1 recovery ──────────────────────────────────────────────────────────────────────────

def _read_payload(op_id: str, op: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The op's payload when it reads AND matches the decided sha; None otherwise."""
    try:
        raw, sha = read_witnessed_bytes(_payload_path(op_id))
        return json.loads(raw.decode("utf-8")) if raw is not None and sha == op.get("payload_sha") else None
    except Exception:
        return None


ABANDON_SUFFIX = ".abandon"


def _payload_is_referenced(path: Path, view: RegistryView) -> bool:
    """An op payload file is referenced by its registry entry; a compensation payload
    (``<op>.abandon.json``, RULE-P4) only once DECISION-2 turned that entry to ``abandon``."""
    from hermes_cli.auth_codex_lifecycle import ABANDON, op_direction
    stem = path.stem
    if stem.endswith(ABANDON_SUFFIX):
        op = view.ops.get(stem[:-len(ABANDON_SUFFIX)])
        return op is not None and op_direction(op) == ABANDON
    return stem in view.ops


def unreferenced_payloads(view: RegistryView) -> List[Path]:
    from hermes_cli.auth_credential_io import isdir_strict
    directory = ops_dir()
    if not isdir_strict(directory):  # an unreadable auth.ops/ raises: never "no payloads"
        return []
    return [p for p in sorted(directory.glob("*.json")) if not (view.ok and _payload_is_referenced(p, view))]


def recover_ops() -> Dict[str, List[str]]:
    """Roll unfinished ops forward; archive undecided payloads. Caller holds the migration lock.

    An op whose payload cannot be read is marked ``unrecoverable`` (its chains fenced) but KEEPS
    its entry — and so its reservation (RULE-G4) — and is retried here on every pass: a payload
    that reads again is rolled forward; only ``resolve_unrecoverable`` releases one that never does.
    A ``lifecycle-stuck`` op is skipped (terminal for automatic dispatch, RULE-P3): only
    ``hermes auth recover --op`` resumes or abandons it."""
    report: Dict[str, List[str]] = {"abandoned": [], "rolled_forward": [], "unrecoverable": [], "stuck": []}
    view = read_registry()
    if view.status == "unreadable":
        return report  # never classify payloads against a registry we cannot read (RULE-G3)
    for path in unreferenced_payloads(view):
        # Never decided: a compensation payload whose DECISION-2 never landed leaves the original
        # op exactly as it was (still forward, still reserved); an op payload is abandoned.
        label = "abandoned-compensation" if path.stem.endswith(ABANDON_SUFFIX) else "abandoned"
        _archive_payload(path, label)
        report["abandoned"].append(path.stem)
    if not view.ok:
        return report
    for op_id, op in list(view.ops.items()):
        if op.get("status") == OP_STUCK:
            report["stuck"].append(op_id)
            continue
        if op.get("status") not in (OP_RUNNING, OP_UNRECOVERABLE):
            continue
        outcome = roll_forward_one(op_id, op)
        report.setdefault(outcome, []).append(op_id)
    return report


def roll_forward_one(op_id: str, op: Dict[str, Any]) -> str:
    """RULE-M1 for one unfinished op (caller holds the migration lock): ``rolled_forward``,
    ``unrecoverable``, ``stuck``, ``busy`` (a lifecycle op whose profile locks are held), or a
    lifecycle op's own outcome. A lifecycle op is completed ONLY from the decision in force (its
    compensation after DECISION-2), never from the other payload."""
    from hermes_cli.auth_codex_profile_ops import LIFECYCLE_KINDS, recover_lifecycle_op
    if op.get("kind") in LIFECYCLE_KINDS:
        outcome = recover_lifecycle_op(op_id, op)
        return "rolled_forward" if outcome in ("kept", "retired") else outcome
    body = _read_payload(op_id, op)
    if body is None:
        if op.get("status") != OP_UNRECOVERABLE:
            _fence_unrecoverable(op_id, op)
        return "unrecoverable"
    if op.get("status") == OP_UNRECOVERABLE:
        _reinstate_decision(op_id, body)
        op = read_registry().ops.get(op_id, op)
    apply_payload(body)
    commit_op(op_id)
    return "rolled_forward"


def _fence_unrecoverable(op_id: str, op: Dict[str, Any]) -> None:
    """Payload missing/corrupt: fence every chain the op touches and mark the op ``unrecoverable``.
    The entry and the payload file (if any) are KEPT: the reservation holds and a payload that is
    only temporarily unreadable is rolled forward later (RULE-M1, RULE-G4)."""
    view = read_registry()
    data = json.loads(json.dumps(view.data))
    scope = op.get("chains")
    targets = list(data["chains"]) if scope == "*" else [c for c in scope or () if c in data["chains"]]
    for cid in targets:
        entry = data["chains"][cid]
        if entry.get("state") == STATE_ACTIVE:
            entry["state"] = STATE_FENCED
            entry["fence"] = {"reason": "op-unrecoverable", "epoch": entry["epoch"], "evidence": {"op_id": op_id}}
    data["ops"][op_id] = {**data["ops"][op_id], "status": OP_UNRECOVERABLE, "unrecoverable_at": utc_now()}
    write_registry(data)
    logger.warning("Codex ownership op %s could not be read; its chains are paused until it can be "
                   "completed or `hermes auth recovery resolve %s` releases it", op_id, op_id[:8])


def _reinstate_decision(op_id: str, body: Dict[str, Any]) -> None:
    """The payload of an ``unrecoverable`` op reads again: restore its DECIDED chain entries (the
    op-unrecoverable fences were about the missing payload, not the credentials) and make it
    runnable, so the ordinary roll-forward completes it. Its reservation kept every other
    ownership change off these chains meanwhile."""
    view = read_registry()
    data = json.loads(json.dumps(view.data))
    for cid, decided in (body.get("chains") or {}).items():
        entry = data["chains"].get(cid)
        fence = (entry or {}).get("fence") or {}
        if entry is not None and fence.get("reason") == "op-unrecoverable" and (
                fence.get("evidence") or {}).get("op_id") == op_id:
            data["chains"][cid] = decided
    data["ops"][op_id] = {**data["ops"][op_id], "status": OP_RUNNING}
    data["ops"][op_id].pop("unrecoverable_at", None)
    write_registry(data)
    logger.info("Codex ownership op %s is readable again; completing it", op_id)


def resolve_unrecoverable(op_id: str) -> str:
    """``hermes auth recovery resolve <op>``: the durable resolution of an ``unrecoverable`` op.
    A payload that reads now is rolled forward (``rolled-forward``); otherwise the op is COMMITTED
    as given up (``released``): its entry and reservation are removed, its chains STAY fenced
    (nothing it decided is guessed), and whatever is left of the payload is archived."""
    with migration_lock():
        view = read_registry()
        matches = [k for k in view.ops if k.startswith(op_id)] if op_id else []
        if len(matches) != 1:
            raise CodexAuthorityRefused(f"No single unfinished Codex login update matches {op_id!r}.")
        op_id = matches[0]
        op = view.ops[op_id]
        if op.get("status") != OP_UNRECOVERABLE:
            raise CodexAuthorityRefused(
                f"Codex login update {op_id[:8]} is not unrecoverable; `hermes auth migrate` completes it.")
        from hermes_cli.auth_codex_profile_ops import LIFECYCLE_KINDS, decision_body
        readable = decision_body(op_id, op) if op.get("kind") in LIFECYCLE_KINDS else _read_payload(op_id, op)
        if readable is not None:
            outcome = roll_forward_one(op_id, op)
            if outcome in ("rolled_forward", "abandoned"):  # both are completed terminal outcomes
                return "rolled-forward" if outcome == "rolled_forward" else "abandoned"
            hint = (f"`hermes auth recover --op {op_id[:8]}` finishes it once that is resolved"
                    if outcome in ("stuck", "busy") else f"`hermes auth recovery resolve {op_id[:8]}` retries it")
            raise CodexAuthorityRefused(
                f"Codex login update {op_id[:8]} reads again but could not be completed yet ({outcome}); {hint}.")

        data = json.loads(json.dumps(view.data))
        data["ops"] = {k: v for k, v in data["ops"].items() if k != op_id}
        for entry in data["chains"].values():
            if (entry.get("lifecycle") or {}).get("op_id") == op_id:
                entry["lifecycle"] = None  # released: its chains stay FENCED (op-unrecoverable)
        write_registry(data)
        path = _payload_path(op_id)
        try:
            _archive_payload(path, OP_UNRECOVERABLE)
        except OSError:
            logger.warning("Codex ownership payload %s could not be archived; left in place", path)
        logger.warning("Codex ownership op %s released by the operator; its logins stay paused", op_id)
        return "released"


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
    for store, sha, rescue in found:
        chains = sorted(candidates.get(rescue, set()))
        for cid in chains:
            entry = data["chains"].get(cid)
            if entry and entry.get("state") == STATE_ACTIVE:
                entry["state"] = STATE_FENCED
                entry["fence"] = {"reason": "legacy-rescue", "epoch": entry["epoch"],
                                  "evidence": {"rescue_store": store, "rescue_file": rescue, "rescue_sha": sha}}
                fenced.append(cid)
        data["legacy_rescues"][rescue] = {"sha": sha, "store": store, "chains": chains, "at": utc_now()}
    write_registry(data)
    return fenced


def retire_entry(entry: Dict[str, Any], remaining_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    return {**entry, "state": STATE_RETIRED, "epoch": entry["epoch"] + 1, "rows": remaining_rows,
            "fence": None, "lifecycle": None}
