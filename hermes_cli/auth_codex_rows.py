"""Codex credential rows inside one auth.json store, and the metadata-only save rules.

A row is ``"singleton"`` (``providers.openai-codex``, tokens under ``tokens``) or ``"pool:<id>"``
(an entry of ``credential_pool.openai-codex``). Rows carry the ``chain_id`` of the chain that owns
them (written only by ownership ops, RULE-M7).

Metadata saves (design §9): an ordinary save may change only the editable metadata (RULE-T3);
token/identity and provenance fields always come from DISK (RULE-T4), and a save merges into a
disk row only while that row still belongs to the same chain — and, for status/error fields, still
holds the same token pair (RULE-T5), so a late failure from a replaced login cannot mark the
replacement dead.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_cli.auth_codex_registry import (
    PROVIDER, SINGLETON, CredentialTokenWriteRefused, pair_fp)

logger = logging.getLogger("hermes_cli.auth")

# RULE-T3 editable metadata. STATUS_FIELDS additionally require the same pair fingerprint (T5).
STATUS_FIELDS = (
    "last_status", "last_status_at", "last_error_code", "last_error_reason", "last_error_message",
    "last_error_reset_at", "status_cleared_at", "model_cooldowns", "failure_reason")
USER_METADATA_FIELDS = ("label", "priority", "request_count", "last_used_at")
EDITABLE_FIELDS = USER_METADATA_FIELDS + STATUS_FIELDS
# RULE-T2 provenance: changes only through authorization (sign-in / import).
PROVENANCE_FIELDS = ("source", "auth_type", "auth_mode", "base_url")
# RULE-M8: a row reassigned to a new chain drops the previous credential's failure state.
FAILURE_RESET_FIELDS = (
    "last_status", "last_status_at", "last_error_code", "last_error_reason", "last_error_message",
    "last_error_reset_at", "model_cooldowns", "failure_reason")


def pool_row(entry_id: str) -> str:
    return f"pool:{entry_id}"


def pool_id(row: str) -> Optional[str]:
    return row[len("pool:"):] if row.startswith("pool:") else None


def _section(store: Dict[str, Any], key: str, create: bool) -> Optional[Dict[str, Any]]:
    section = store.get(key)
    if not isinstance(section, dict):
        if not create:
            return None
        section = store[key] = {}
    return section


def pool_entries(store: Dict[str, Any], create: bool = False) -> Optional[List[Any]]:
    pool = _section(store, "credential_pool", create)
    if pool is None:
        return None
    entries = pool.get(PROVIDER)
    if not isinstance(entries, list):
        if not create:
            return None
        entries = pool[PROVIDER] = []
    return entries


def singleton_state(store: Dict[str, Any], create: bool = False) -> Optional[Dict[str, Any]]:
    providers = _section(store, "providers", create)
    if providers is None:
        return None
    state = providers.get(PROVIDER)
    if not isinstance(state, dict):
        if not create:
            return None
        state = providers[PROVIDER] = {}
    return state


def get_row(store: Dict[str, Any], row: str) -> Optional[Dict[str, Any]]:
    if row == SINGLETON:
        return singleton_state(store)
    pid = pool_id(row)
    for entry in pool_entries(store) or ():
        if isinstance(entry, dict) and entry.get("id") == pid:
            return entry
    return None


def row_tokens(store: Dict[str, Any], row: str) -> Tuple[str, str]:
    data = get_row(store, row) or {}
    source = data.get("tokens") if row == SINGLETON else data
    source = source if isinstance(source, dict) else {}
    return str(source.get("access_token") or "").strip(), str(source.get("refresh_token") or "").strip()


def row_fp(store: Dict[str, Any], row: str) -> Optional[str]:
    access, refresh = row_tokens(store, row)
    return pair_fp(access, refresh) if (access or refresh) else None


def row_chain(store: Dict[str, Any], row: str) -> Optional[str]:
    data = get_row(store, row)
    value = data.get("chain_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def codex_rows(store: Dict[str, Any]) -> List[str]:
    """Rows holding refreshable material (a refresh token): the ones the authority governs."""
    rows = []
    if row_tokens(store, SINGLETON)[1]:
        rows.append(SINGLETON)
    for entry in pool_entries(store) or ():
        if isinstance(entry, dict) and entry.get("id") and str(entry.get("refresh_token") or "").strip():
            rows.append(pool_row(str(entry["id"])))
    return rows


def set_row_tokens(store: Dict[str, Any], row: str, access: str, refresh: str,
                   last_refresh: Optional[str]) -> None:
    """Token write for a refresh transaction (rows of ONE chain in the owner store)."""
    data = get_row(store, row)
    if data is None:
        raise KeyError(row)
    if row == SINGLETON:
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        data["tokens"] = {**tokens, "access_token": access, "refresh_token": refresh}
    else:
        data["access_token"], data["refresh_token"] = access, refresh
    if last_refresh:
        data["last_refresh"] = last_refresh


def reset_failure_state(data: Dict[str, Any], now: Optional[float] = None) -> None:
    for name in FAILURE_RESET_FIELDS:
        if name == "failure_reason":
            data.pop(name, None)
        else:
            data[name] = None
    data["status_cleared_at"] = now if now is not None else time.time()


def write_row(store: Dict[str, Any], row: str, *, chain_id: str, access: str, refresh: str,
              owned: Dict[str, Any], defaults: Dict[str, Any], reset_failure: bool) -> bool:
    """RULE-M7 conditional APPLY of one row. Returns False when the row already carries the target
    chain and fingerprint (already applied: every later observation is kept)."""
    existing = get_row(store, row)
    target_fp = pair_fp(access, refresh)
    if existing is not None and existing.get("chain_id") == chain_id and row_fp(store, row) == target_fp:
        return False
    if existing is None:
        if row == SINGLETON:
            existing = singleton_state(store, create=True)
        else:
            existing = {"id": pool_id(row)}
            pool_entries(store, create=True).append(existing)
        for key, value in defaults.items():
            existing.setdefault(key, value)
    elif reset_failure and existing.get("chain_id") != chain_id:
        reset_failure_state(existing)
    for key, value in owned.items():
        existing[key] = value
    existing["chain_id"] = chain_id
    if row == SINGLETON:
        tokens = existing.get("tokens") if isinstance(existing.get("tokens"), dict) else {}
        existing["tokens"] = {**tokens, "access_token": access, "refresh_token": refresh}
    else:
        existing["access_token"], existing["refresh_token"] = access, refresh
    return True


def assign_id(store: Dict[str, Any], entry_id: str, expected_fp: str) -> bool:
    """Bootstrap APPLY: give an id-less Codex pool row (hand-written / pre-id format) the id the
    payload chose, so it can be registered. Only the first id-less row with that material."""
    for entry in pool_entries(store) or ():
        if isinstance(entry, dict) and not entry.get("id") and _entry_fp(entry) == expected_fp:
            entry["id"] = entry_id
            return True
    return False


def tag_row(store: Dict[str, Any], row: str, chain_id: str, expected_fp: str) -> bool:
    """Bootstrap APPLY: record the chain on an existing row whose material is still what the
    payload saw. Never touches tokens."""
    data = get_row(store, row)
    if data is None or row_fp(store, row) != expected_fp or data.get("chain_id") == chain_id:
        return False
    data["chain_id"] = chain_id
    return True


def remove_row(store: Dict[str, Any], row: str, expected_chain: Optional[str], expected_fp: Optional[str]) -> bool:
    """Removal APPLY: drop a row only while it still is what the payload decided to remove."""
    data = get_row(store, row)
    if data is None:
        return False
    if data.get("chain_id") != expected_chain and row_fp(store, row) != expected_fp:
        return False
    if row == SINGLETON:
        providers = store.get("providers")
        if isinstance(providers, dict):
            providers.pop(PROVIDER, None)
        return True
    entries = pool_entries(store) or []
    entries[:] = [e for e in entries if not (isinstance(e, dict) and e.get("id") == pool_id(row))]
    if not entries:
        pool = store.get("credential_pool")
        if isinstance(pool, dict):
            pool.pop(PROVIDER, None)
    return True


# ── Metadata-only saves (RULE-T4 / T5) ───────────────────────────────────────────────────────

def _entry_fp(entry: Dict[str, Any]) -> str:
    return pair_fp(str(entry.get("access_token") or "").strip(), str(entry.get("refresh_token") or "").strip())


def _is_governed(entry: Any) -> bool:
    return isinstance(entry, dict) and bool(str(entry.get("refresh_token") or "").strip())


def merge_metadata_row(disk: Dict[str, Any], incoming: Dict[str, Any], *, status_cleared: bool,
                       merge_cooldowns) -> Dict[str, Any]:
    """The disk row with only the caller's editable metadata applied (T4), gated by T5."""
    same_pair = _entry_fp(incoming) == _entry_fp(disk)
    # A row loaded before bootstrap registered it carries no chain id yet: same material = same row.
    same_chain = incoming.get("chain_id") == disk.get("chain_id") or (
        incoming.get("chain_id") is None and same_pair)
    if not same_chain:
        logger.debug("codex pool save: row %s now belongs to another chain; dropping the stale save",
                     disk.get("id"))
        return dict(disk)
    merged = dict(disk)
    for name in USER_METADATA_FIELDS:
        if name in incoming:
            merged[name] = incoming[name]
    if not same_pair:
        logger.debug("codex pool save: row %s holds a different token pair; status fields kept from disk",
                     disk.get("id"))
        return merged
    status_view = {name: incoming.get(name) for name in STATUS_FIELDS}
    status_view.update(access_token=disk.get("access_token"), refresh_token=disk.get("refresh_token"))
    resolved = merge_cooldowns(status_view, None if status_cleared else disk)
    for name in STATUS_FIELDS:
        value = resolved.get(name)
        if value is None and (name == "failure_reason" or (name not in incoming and name not in disk)):
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


def merge_codex_pool_rows(
    disk_list: List[Any], incoming: List[Any], *, removed_ids: Iterable[str],
    status_cleared_ids: Iterable[str], merge_cooldowns,
) -> List[Any]:
    """Codex branch of ``write_credential_pool`` / root write-back: metadata only.

    A governed row (one with refresh material) is never created here: an in-memory row that is not
    on disk (removed by another process's ownership op, or never registered) is dropped from the
    save. Removing a governed row here is refused (``CredentialTokenWriteRefused``): removal is an
    ownership operation (RULE-K1). Rows without refresh material (API-key rows) keep upstream's
    semantics."""
    removed = {rid for rid in removed_ids if rid}
    cleared = {cid for cid in status_cleared_ids if cid}
    disk_by_id = {e.get("id"): e for e in disk_list if isinstance(e, dict) and e.get("id")}
    incoming_ids = {e.get("id") for e in incoming if isinstance(e, dict) and e.get("id")}
    for rid in removed:
        if _is_governed(disk_by_id.get(rid)):
            raise CredentialTokenWriteRefused(
                f"openai-codex row {rid} can only be removed through `hermes auth remove`")
    result: List[Any] = []
    for entry in incoming:
        eid = entry.get("id") if isinstance(entry, dict) else None
        disk = disk_by_id.get(eid) if eid else None
        if disk is None:
            if _is_governed(entry):
                logger.debug("codex pool save: row %s is not on disk; not recreated (ownership ops only)", eid)
                continue
            result.append(entry)
        elif _is_governed(disk):
            result.append(merge_metadata_row(disk, entry, status_cleared=eid in cleared,
                                             merge_cooldowns=merge_cooldowns))
        else:
            result.append(entry)
    for eid, disk in disk_by_id.items():
        if eid not in incoming_ids and eid not in removed:
            result.append(disk)
    return result
