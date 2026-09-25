"""Payload builders for Codex ownership operations (design rev 9 §4, §6, §7, §8, §10 stage 1).

Each builder reads the registry view and the store snapshots loaded under their locks and returns a
``Payload`` (final registry entries + idempotent store edits + record actions). They never write;
``auth_codex_ops.run_op`` does.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from hermes_cli import auth_codex_rows as rows_mod
from hermes_cli.auth_codex_ops import Payload, retire_entry
from hermes_cli.auth_codex_evidence import Evidence, known_material, row_material
from hermes_cli.auth_codex_records import read_record
from hermes_cli.auth_codex_registry import (
    PROVIDER, SINGLETON, STATE_ACTIVE, STATE_FENCED, RegistryView, is_root_store, new_id,
    pair_fp, root_store_path, token_sha, unrecorded_legacy_rescues, utc_now)
from hermes_cli.auth_credential_io import store_key

logger = logging.getLogger("hermes_cli.auth")

DEVICE_CODE = "device_code"
MANUAL_DEVICE_CODE = "manual:device_code"


@dataclass
class Snapshot:
    key: str
    path: Path
    store: Dict[str, Any]

    @property
    def is_root(self) -> bool:
        return is_root_store(self.key)

    @property
    def profile(self) -> str:
        from hermes_constants import profile_name_for_home
        return profile_name_for_home(self.path.parent) or self.key


def load_snapshots(paths: Iterable[Path]) -> Dict[str, Snapshot]:
    """Witnessed loads of each store (caller holds their locks)."""
    from hermes_cli.auth import _load_auth_store
    snaps: Dict[str, Snapshot] = {}
    for p in paths:
        key = store_key(p)
        if key not in snaps:
            snaps[key] = Snapshot(key, Path(p), _load_auth_store(Path(p)))
    return snaps


def _account_id(access: str) -> Optional[str]:
    try:
        from agent.credential_pool import _codex_principal_identity
        identity = _codex_principal_identity(access)
    except Exception:
        identity = None
    return identity[0] if identity else None


def _chain_entry(owner: str, rows: List[Tuple[str, str]], access: str, *, state: str = STATE_ACTIVE,
                 fence: Optional[Dict[str, Any]] = None, epoch: int = 1) -> Dict[str, Any]:
    return {"provider": PROVIDER, "owner": owner, "epoch": epoch, "state": state, "fence": fence,
            "lifecycle": None, "account_id": _account_id(access),
            "rows": [{"store": s, "row": r} for s, r in rows]}


def _create_record(owner: str, cid: str, epoch: int, access: str, refresh: str, outcome: str) -> Dict[str, Any]:
    return {"action": "create", "owner": owner, "chain_id": cid, "epoch": epoch,
            "fp": pair_fp(access, refresh), "access_sha": token_sha(access),
            "refresh_sha": token_sha(refresh), "outcome": outcome}


def _next_priority(store: Dict[str, Any]) -> int:
    prios = [e.get("priority") for e in rows_mod.pool_entries(store) or () if isinstance(e, dict)]
    return max((p for p in prios if isinstance(p, int)), default=-1) + 1


def _new_pool_id(store: Dict[str, Any]) -> str:
    taken = {e.get("id") for e in rows_mod.pool_entries(store) or () if isinstance(e, dict)}
    while True:
        candidate = uuid.uuid4().hex[:6]
        if candidate not in taken:
            return candidate


def _is_suppressed(store: Dict[str, Any], source: str) -> bool:
    suppressed = store.get("suppressed_sources")
    listed = suppressed.get(PROVIDER) if isinstance(suppressed, dict) else None
    return source in (listed if isinstance(listed, (list, dict)) else ())


def _has_source(store: Dict[str, Any], source: str) -> bool:
    return any(isinstance(e, dict) and e.get("source") == source for e in rows_mod.pool_entries(store) or ())


def _alias_defaults(store: Dict[str, Any], access: str) -> Dict[str, Any]:
    from agent.credential_pool import label_from_token
    from hermes_cli.auth_constants import DEFAULT_CODEX_BASE_URL
    state = rows_mod.singleton_state(store) or {}
    return {"source": DEVICE_CODE, "auth_type": "oauth", "priority": _next_priority(store),
            "label": str(state.get("label") or "").strip() or label_from_token(access, DEVICE_CODE),
            "base_url": DEFAULT_CODEX_BASE_URL, "last_refresh": state.get("last_refresh"),
            "request_count": 0}


def _index_only_evidence() -> Evidence:
    """Bootstrap / reconstruction (no readable registry): the retired-token index alone."""
    from hermes_cli.auth_codex_registry import RegistryView as _View
    evidence = known_material(_View("ok", {"version": 1, "ops": {}, "chains": {}}))
    return evidence


def _add(payload: Payload, key: str, edit: Dict[str, Any]) -> None:
    payload.edits.setdefault(key, []).append(edit)


# ── RULE-M4 bootstrap / adoption of unregistered rows / RULE-M6 fenced reconstruction ─────────

def plan_registration(view: RegistryView, snaps: Dict[str, Snapshot], *, only_unregistered: bool,
                      fence_all: Optional[str] = None, kind: str = "bootstrap") -> Payload:
    """Register Codex rows as chains.

    Groups ONLY by identical current token material (copied row ids are not lineage). A group whose
    tokens also appear in another group, or that spans several non-root stores without a root row,
    is ambiguous: one fenced chain per row. Owner = root when the group has a root row, else its one
    store. Singleton aliases are materialized here (RULE-K1) unless the source is suppressed (K2).
    Legacy rescue files fence their candidate chains in the same decision (§7).
    """
    infos: List[Tuple[str, str, str, str]] = []  # (store, row, access, refresh)
    pending_ids: List[Tuple[str, Dict[str, Any]]] = []
    for snap in snaps.values():
        for entry in rows_mod.pool_entries(snap.store) or ():
            if isinstance(entry, dict) and not entry.get("id") and str(entry.get("refresh_token") or "").strip():
                entry["id"] = _new_pool_id(snap.store)  # snapshot only; APPLY assigns it on disk
                pending_ids.append((snap.key, {"op": "assign_id", "id": entry["id"], "fp": pair_fp(
                    str(entry.get("access_token") or "").strip(), str(entry.get("refresh_token") or "").strip())}))
    for snap in snaps.values():
        for row in rows_mod.codex_rows(snap.store):
            if only_unregistered and view.chain_for_row(snap.key, row):
                continue
            access, refresh = rows_mod.row_tokens(snap.store, row)
            infos.append((snap.key, row, access, refresh))
    groups: Dict[str, List[Tuple[str, str, str, str]]] = defaultdict(list)
    fps_by_access: Dict[str, Set[str]] = defaultdict(set)
    fps_by_refresh: Dict[str, Set[str]] = defaultdict(set)
    for info in infos:
        fp = pair_fp(info[2], info[3])
        groups[fp].append(info)
        if info[2]:
            fps_by_access[info[2]].add(fp)
        fps_by_refresh[info[3]].add(fp)
    # Material any chain ever held (live records + the retired-token index) is never a new
    # authorization; when that evidence is incomplete, nothing is registered active (fail closed).
    evidence = known_material(view) if view.ok else _index_only_evidence()

    payload = Payload(kind=kind, scope_chains="*" if not only_unregistered else [],
                      scope_stores=sorted(snaps), scope_profiles=sorted({s.profile for s in snaps.values()}))
    for key, edit in pending_ids:
        _add(payload, key, edit)
    singleton_chain: Dict[str, str] = {}
    for fp, members in groups.items():
        access, refresh = members[0][2], members[0][3]
        stores = {m[0] for m in members}
        root_rows = [m for m in members if is_root_store(m[0])]
        ambiguous = (
            len(fps_by_refresh[refresh]) > 1 or (access and len(fps_by_access[access]) > 1)
            or (not root_rows and len(stores) > 1) or (not fence_all and evidence.knows(access, refresh)))
        unverified = not ambiguous and not fence_all and not evidence.complete
        units = [[m] for m in members] if ambiguous else [members]
        for unit in units:
            cid = new_id()
            owner = next((m[0] for m in unit if is_root_store(m[0])), unit[0][0])
            fence = None
            state = STATE_ACTIVE
            if ambiguous:
                state, fence = STATE_FENCED, {"reason": "ambiguous-lineage", "epoch": 1}
            elif fence_all:
                state, fence = STATE_FENCED, {"reason": fence_all, "epoch": 1}
            elif unverified:
                state, fence = STATE_FENCED, {"reason": "unverified-history", "epoch": 1,
                                              "evidence": {"gaps": evidence.gaps[:5]}}
            unit_rows = [(m[0], m[1]) for m in unit]
            if not ambiguous:
                for store, row in unit_rows:
                    if row == SINGLETON:
                        singleton_chain[store] = cid
            payload.chains[cid] = _chain_entry(owner, unit_rows, access, state=state, fence=fence)
            payload.records.append(_create_record(owner, cid, 1, access, refresh,
                                                  "rebuilt" if fence_all else kind))
            for store, row in unit_rows:
                _add(payload, store, {"op": "tag_row", "row": row, "chain_id": cid, "fp": fp})
            if not only_unregistered:
                continue
            payload.scope_chains.append(cid)

    for store_key_, cid in singleton_chain.items():
        snap = snaps[store_key_]
        if _has_source(snap.store, DEVICE_CODE) or _is_suppressed(snap.store, DEVICE_CODE):
            continue  # an alias exists, or the user removed it (RULE-K2: no selectable alias)
        access, refresh = rows_mod.row_tokens(snap.store, SINGLETON)
        row = rows_mod.pool_row(_new_pool_id(snap.store))
        payload.chains[cid]["rows"].append({"store": store_key_, "row": row})
        _add(payload, store_key_, {"op": "write_row", "row": row, "chain_id": cid, "access_token": access,
                                   "refresh_token": refresh, "owned": {},
                                   "defaults": _alias_defaults(snap.store, access), "reset_failure": False})

    _fence_legacy_rescues(view, snaps, payload)
    return payload


def rescue_candidates(view_chains: Dict[str, Dict[str, Any]], snaps: Dict[str, Snapshot],
                      store: str) -> Set[str]:
    """Chains a store owns OR borrows (a profile without its own rows reads root's)."""
    root = store_key(root_store_path())
    snap = snaps.get(store)
    own_singleton = bool(snap and rows_mod.row_tokens(snap.store, SINGLETON)[1])
    own_pool = bool(snap and rows_mod.pool_entries(snap.store))
    found: Set[str] = set()
    for cid, entry in view_chains.items():
        for r in entry.get("rows", ()):
            if r["store"] == store:
                found.add(cid)
            elif store != root and r["store"] == root and (
                    (r["row"] == SINGLETON and not own_singleton) or (r["row"] != SINGLETON and not own_pool)):
                found.add(cid)
    return found


def _fence_legacy_rescues(view: RegistryView, snaps: Dict[str, Snapshot], payload: Payload) -> None:
    for store, sha, rescue in unrecorded_legacy_rescues(view):
        chains = sorted(rescue_candidates({**view.chains, **payload.chains}, snaps, store))
        for cid in chains:
            entry = payload.chains.get(cid) or view.chain(cid)
            if entry and entry.get("state") == STATE_ACTIVE:
                payload.chains[cid] = {**entry, "state": STATE_FENCED, "fence": {
                    "reason": "legacy-rescue", "epoch": entry["epoch"],
                    "evidence": {"rescue_store": store, "rescue_file": rescue, "rescue_sha": sha}}}
        payload.legacy_rescues[rescue] = {"sha": sha, "store": store, "chains": chains, "at": utc_now()}


# ── RULE-A2 fresh sign-in ────────────────────────────────────────────────────────────────────

def _chain_row_material(entry: Dict[str, Any], snaps: Dict[str, Snapshot]) -> List[Dict[str, Any]]:
    """Token hashes of every row a chain holds now (kept in the retired-token index with its
    record's history, RULE-A3)."""
    out = []
    for r in entry.get("rows", ()):
        snap = snaps.get(r["store"])
        if snap is not None:
            access, refresh = rows_mod.row_tokens(snap.store, r["row"])
            if access or refresh:
                out.append(row_material(access, refresh))
    return out


def _release_rows(view: RegistryView, payload: Payload, target: str, released: List[str],
                  snaps: Dict[str, Snapshot]) -> None:
    """Take *released* rows out of their old chains; an old chain owned by the target store (or
    left without rows) is retired and its record archived (RULE-A3)."""
    released_set = {(target, r) for r in released}
    old: Dict[str, Dict[str, Any]] = {}
    for row in released:
        found = view.chain_for_row(target, row)
        if found:
            old[found[0]] = found[1]
    for cid, entry in old.items():
        remaining = [r for r in entry["rows"] if (r["store"], r["row"]) not in released_set]
        if entry["owner"] == target or not remaining:
            payload.chains[cid] = retire_entry(entry, remaining)
            payload.records.append({"action": "archive", "owner": entry["owner"], "chain_id": cid,
                                    "reason": "retired", "rows": _chain_row_material(entry, snaps)})
        else:
            payload.chains[cid] = {**entry, "rows": remaining, "epoch": entry["epoch"] + 1}
            payload.records.append({"action": "set_epoch", "owner": entry["owner"], "chain_id": cid,
                                    "epoch": entry["epoch"] + 1})
        payload.scope_chains.append(cid)
        if entry["owner"] not in payload.scope_stores:
            payload.scope_stores.append(entry["owner"])


def plan_sign_in(view: RegistryView, snaps: Dict[str, Snapshot], target: str, access: str, refresh: str, *,
                 kind: str, last_refresh: Optional[str], label: Optional[str] = None,
                 source: Optional[str] = None, base_url: Optional[str] = None, set_active: bool = True,
                 activate_if_unset: bool = False, outcome: str = "sign-in") -> Payload:
    """A fresh authorization in *target* (always profile-owned by that store, RULE-A2)."""
    from hermes_cli.auth_constants import DEFAULT_CODEX_BASE_URL
    snap = snaps[target]
    store = snap.store
    cid = new_id()
    payload = Payload(kind=f"sign-in:{kind}", scope_chains=[cid], scope_stores=[target],
                      scope_profiles=[snap.profile])
    rows: List[str] = []
    if kind == "singleton":
        prev_access, _ = rows_mod.row_tokens(store, SINGLETON)
        owned = {"auth_mode": "chatgpt", "last_refresh": last_refresh}
        if label and str(label).strip():
            owned["label"] = str(label).strip()
        rows.append(SINGLETON)
        _add(payload, target, {"op": "write_row", "row": SINGLETON, "chain_id": cid, "access_token": access,
                               "refresh_token": refresh, "owned": owned, "defaults": {}, "reset_failure": True})
        # The pool rows upstream's reauth sync keeps (#33000, #39236): the device_code alias, and a
        # manual:device_code row that was a copy of the previous singleton.
        for entry in rows_mod.pool_entries(store) or ():
            if not (isinstance(entry, dict) and entry.get("id")):
                continue
            is_alias = entry.get("source") == DEVICE_CODE or (
                entry.get("source") == MANUAL_DEVICE_CODE and prev_access
                and entry.get("access_token") == prev_access)
            if is_alias:
                row = rows_mod.pool_row(str(entry["id"]))
                rows.append(row)
                _add(payload, target, {"op": "write_row", "row": row, "chain_id": cid, "access_token": access,
                                       "refresh_token": refresh, "owned": {"last_refresh": last_refresh},
                                       "defaults": {}, "reset_failure": True})
        if not _has_source(store, DEVICE_CODE):
            row = rows_mod.pool_row(_new_pool_id(store))
            rows.append(row)
            defaults = _alias_defaults(store, access)
            defaults.update(label=(str(label).strip() if label and str(label).strip() else defaults["label"]),
                            last_refresh=last_refresh)
            _add(payload, target, {"op": "write_row", "row": row, "chain_id": cid, "access_token": access,
                                   "refresh_token": refresh, "owned": {}, "defaults": defaults,
                                   "reset_failure": False})
        _add(payload, target, {"op": "unsuppress", "sources": [DEVICE_CODE]})
        if set_active:
            _add(payload, target, {"op": "set_active_provider", "value": PROVIDER})
    else:
        row = rows_mod.pool_row(_new_pool_id(store))
        rows.append(row)
        defaults = {"source": source or MANUAL_DEVICE_CODE, "auth_type": "oauth",
                    "priority": _next_priority(store), "label": label or source or MANUAL_DEVICE_CODE,
                    "base_url": base_url or DEFAULT_CODEX_BASE_URL, "last_refresh": last_refresh,
                    "request_count": 0}
        _add(payload, target, {"op": "write_row", "row": row, "chain_id": cid, "access_token": access,
                               "refresh_token": refresh, "owned": {}, "defaults": defaults,
                               "reset_failure": False})
        suppressed = store.get("suppressed_sources")
        listed = suppressed.get(PROVIDER) if isinstance(suppressed, dict) else None
        if listed:
            _add(payload, target, {"op": "unsuppress", "sources": list(listed)})
        if activate_if_unset:
            _add(payload, target, {"op": "set_active_provider", "value": PROVIDER, "only_if_unset": True})
    _release_rows(view, payload, target, [r for r in rows if view.chain_for_row(target, r)], snaps)
    payload.chains[cid] = _chain_entry(target, [(target, r) for r in rows], access)
    payload.records.append(_create_record(target, cid, 1, access, refresh, outcome))
    payload.result = {"chain_id": cid, "rows": rows}
    return payload


# ── Removal (auth remove / logout) and the retry override ──────────────────────────────────────

def plan_removal(view: RegistryView, snaps: Dict[str, Snapshot], target: str, rows: List[str], *,
                 suppress: Iterable[str] = (), deactivate: bool = False) -> Payload:
    snap = snaps[target]
    payload = Payload(kind="remove", scope_chains=[], scope_stores=[target], scope_profiles=[snap.profile])
    for row in rows:
        if rows_mod.get_row(snap.store, row) is None:
            continue
        _add(payload, target, {"op": "remove_row", "row": row, "chain_id": rows_mod.row_chain(snap.store, row),
                               "fp": rows_mod.row_fp(snap.store, row)})
    _release_rows(view, payload, target, [r for r in rows if view.chain_for_row(target, r)], snaps)
    for cid, entry in list(payload.chains.items()):
        owner_rows = [r for r in entry["rows"] if r["store"] == entry["owner"]]
        if entry.get("state") != "retired" and not owner_rows:
            payload.chains[cid] = retire_entry(view.chain(cid), entry["rows"])
            payload.records = [a for a in payload.records if a.get("chain_id") != cid]
            payload.records.append({"action": "archive", "owner": entry["owner"], "chain_id": cid,
                                    "reason": "removed", "rows": _chain_row_material(view.chain(cid), snaps)})
    if suppress:
        _add(payload, target, {"op": "suppress", "sources": list(suppress)})
    if deactivate:
        active = snap.store.get("active_provider")
        if active == PROVIDER:
            _add(payload, target, {"op": "set_active_provider", "value": None})
    return payload


def plan_override(view: RegistryView, snaps: Dict[str, Snapshot], chain_id: str) -> Payload:
    """RULE-O1: reactivate a chain fenced as indeterminate for exactly one retry."""
    from hermes_cli.auth_codex_registry import CodexAuthorityRefused, store_path_of
    entry = view.chain(chain_id)
    fence = (entry or {}).get("fence") or {}
    if not entry or entry.get("state") != STATE_FENCED or fence.get("reason") != "indeterminate":
        raise CodexAuthorityRefused(
            "--retry-refresh applies only to a Codex login paused after an unconfirmed refresh.")
    record = read_record(store_path_of(entry["owner"]), chain_id)
    attempt = (record.data.get("attempt") or {}) if record.status == "ok" else {}
    if attempt.get("state") != "indeterminate" or not attempt.get("attempt_id"):
        raise CodexAuthorityRefused("The refresh record for this login is not in the unconfirmed state.")
    grant = new_id()
    epoch = entry["epoch"] + 1
    payload = Payload(kind="override", scope_chains=[chain_id], scope_stores=[entry["owner"]])
    payload.chains[chain_id] = {**entry, "state": STATE_ACTIVE, "fence": None, "epoch": epoch}
    payload.records.append({"action": "override", "owner": entry["owner"], "chain_id": chain_id,
                            "epoch": epoch, "for_attempt": attempt["attempt_id"], "grant_id": grant})
    payload.result = {"grant_id": grant, "for_attempt": attempt["attempt_id"]}
    return payload
