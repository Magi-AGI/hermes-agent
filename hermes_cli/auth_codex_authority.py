"""Codex chain authority: dispatcher and the ownership entry points callers use.

Credential recovery protocol, stage 1 (design rev 9 §6, §8, §10). Every function here takes the
migration lock FIRST and then the store locks (RULE-L1), so none may be called while this thread
holds an auth-store lock; the dispatcher checks and returns ``"busy"`` instead.

* ``authority_dispatch()`` — top-level entry points only: bootstrap migration when the registry is
  ABSENT (RULE-M4), roll unfinished ops forward (RULE-M1), fence chains of newly found legacy
  rescue files (§7). One non-blocking lock attempt; never for read-only callers.
* ``sign_in`` / ``import_pair`` — RULE-A2 / A1.
* ``remove_rows`` — removal / logout as ownership ops (RULE-K1).
* ``grant_retry_override`` — RULE-O1 (``hermes auth codex --retry-refresh``).
* ``refuse_if_registered_store`` — stage-1 refusal of profile rename/delete/restore (RULE-P1).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from hermes_cli import auth_codex_rows as rows_mod
from hermes_cli.auth_codex_ops import recover_ops, record_legacy_rescues, run_op
from hermes_cli.auth_codex_plans import (
    load_snapshots, plan_override, plan_registration, plan_removal, plan_sign_in, rescue_candidates)
from hermes_cli.auth_codex_evidence import known_material
from hermes_cli.auth_codex_records import read_record
from hermes_cli.auth_codex_registry import (
    PROVIDER, SINGLETON, STATE_ACTIVE, STATE_RETIRED, CodexAuthorityRefused, CodexRegistryUnreadable, RegistryView,
    active_store_path, all_store_paths, migration_lock, ops_dir, pair_fp, preserve_unreadable_registry,
    read_registry, store_locks, store_path_of, thread_holds_store_lock, token_sha,
    try_migration_lock, unrecorded_legacy_rescues)
from hermes_cli.auth_credential_io import store_key

logger = logging.getLogger("hermes_cli.auth")


# ── Dispatcher (§6) ───────────────────────────────────────────────────────────────────────────

def _bootstrap(fence_all: Optional[str] = None) -> Dict[str, Any]:
    paths = all_store_paths()
    with store_locks(paths):
        view = read_registry()
        payload = plan_registration(view, load_snapshots(paths), only_unregistered=False,
                                    fence_all=fence_all, kind="rebuild" if fence_all else "bootstrap")
        run_op(payload, view)
    logger.info("Codex authority registry %s: %d chain(s)", "rebuilt" if fence_all else "created",
                len(payload.chains))
    return {"chains": len(payload.chains)}


def _fence_new_legacy_rescues() -> List[str]:
    view = read_registry()
    paths = all_store_paths()
    found = unrecorded_legacy_rescues(view, paths)
    if not found:
        return []
    with store_locks(paths):
        snaps = load_snapshots(paths)
        candidates = {store: rescue_candidates(view.chains, snaps, store) for store, _sha in found}
        return record_legacy_rescues(found, candidates)


def _codex_material_present(paths: Iterable[Path]) -> bool:
    """Any Codex row or legacy rescue file anywhere (an ABSENT registry with neither is trivially
    consistent: nothing to govern, so nothing is written until the first Codex sign-in)."""
    from hermes_cli.auth import _load_auth_store
    from hermes_cli.auth_codex_registry import legacy_rescue_path
    for path in paths:
        if legacy_rescue_path(path).exists():
            return True
        try:
            if path.exists() and rows_mod.codex_rows(_load_auth_store(path)):
                return True
        except OSError:
            return True  # unreadable store: let the bootstrap surface it
    return False


def dispatch_locked() -> str:
    """The dispatcher body; caller holds the migration lock and no store lock."""
    view = read_registry()
    if view.status == "unreadable":
        preserve_unreadable_registry()
        return "unreadable"
    recover_ops()
    if read_registry().status == "absent":
        if not _codex_material_present(all_store_paths()):
            return "idle"
        _bootstrap()
        return "bootstrapped"
    _fence_new_legacy_rescues()
    return "done"


# A request that finds the registry absent / an unfinished op waits this long for the process
# that is recovering (one attempt), then gets the transient "updating your Codex login" error.
REQUEST_DISPATCH_BUDGET_SECONDS = 5.0


def authority_dispatch(*, read_only: bool = False, budget_seconds: float = 0.0) -> str:
    """One bounded pass (§6). Returns ``skipped`` (read-only), ``busy`` (lock taken elsewhere, or
    this thread holds a store lock), ``unreadable``, ``idle``, ``bootstrapped`` or ``done``."""
    if read_only:
        return "skipped"
    with try_migration_lock(budget_seconds) as acquired:
        if not acquired:
            return "busy"
        try:
            return dispatch_locked()
        except Exception:
            logger.warning("Codex authority dispatch failed", exc_info=True)
            return "failed"


def dispatch_needed() -> bool:
    """Unfinished ops to roll forward, or payloads left by an op that never reached its decision."""
    view = read_registry()
    return (view.status == "absent" or any(op.get("status") == "running" for op in view.ops.values())
            or (ops_dir().is_dir() and any(ops_dir().glob("*.json"))))


def startup_dispatch() -> str:
    """CLI startup hook for runtime/mutating modes: only does work when there is some to do."""
    try:
        view = read_registry()
        paths = all_store_paths()
        if view.status == "absent":
            if not _codex_material_present(paths) and not ops_dir().is_dir():
                return "idle"
        elif not dispatch_needed() and not unrecorded_legacy_rescues(view, paths):
            return "idle"
    except Exception:
        return "idle"
    return authority_dispatch()


# ── Ownership entry points ───────────────────────────────────────────────────────────────────

def _ready_view() -> RegistryView:
    """Registry ready for an ownership op (bootstrapped, unfinished ops rolled forward)."""
    recover_ops()
    view = read_registry()
    if view.status == "absent":
        _bootstrap()
        view = read_registry()
    return view


def _run_with_all_stores(build) -> Any:
    """Migration lock, registry ready, ALL store locks (ownership ops are rare), then build + run."""
    if thread_holds_store_lock():
        raise RuntimeError("Codex ownership operations must not run inside an auth-store lock")
    with migration_lock():
        view = read_registry()
        if view.status == "unreadable":
            view = None
        else:
            view = _ready_view()
        paths = all_store_paths()
        active = active_store_path()
        if store_key(active) not in {store_key(p) for p in paths}:
            paths.append(active)
        with store_locks(paths):
            snaps = load_snapshots(paths)
            return build(view, snaps)


def any_chain_fenced(view: Optional[RegistryView]) -> bool:
    return view is None or any(c.get("state") == "fenced" for c in view.chains.values())


def sign_in(access: str, refresh: str, *, kind: str = "singleton", last_refresh: Optional[str] = None,
            label: Optional[str] = None, source: Optional[str] = None, base_url: Optional[str] = None,
            set_active: bool = True, activate_if_unset: bool = False, outcome: str = "sign-in") -> Dict[str, Any]:
    """RULE-A2: a fresh authorization, owned by the ACTIVE store (profile-owned in a profile).

    With an UNREADABLE registry (RULE-G3) the damaged file is preserved and the registry is
    reconstructed with every existing chain fenced (``registry-rebuilt``); the new sign-in is the
    only active chain."""
    from hermes_cli.auth import _utc_now_z
    last_refresh = last_refresh or _utc_now_z()

    def build(view, snaps):
        if view is None:
            # The damaged file is copied, the old generation's records are folded into the
            # retired-token index, and the fenced reconstruction's DECISION then atomically
            # replaces the damaged registry: it is never ABSENT in between (an absent registry
            # would be bootstrapped with active chains).
            from hermes_cli.auth_codex_evidence import retain_orphan_records
            preserve_unreadable_registry()
            retain_orphan_records([s.path for s in snaps.values()])
            _bootstrap(fence_all="registry-rebuilt")
            view = read_registry()
            snaps = load_snapshots([s.path for s in snaps.values()])
        payload = plan_sign_in(view, snaps, store_key(active_store_path()), access, refresh, kind=kind,
                               last_refresh=last_refresh, label=label, source=source, base_url=base_url,
                               set_active=set_active, activate_if_unset=activate_if_unset, outcome=outcome)
        run_op(payload, view)
        return payload.result
    return _run_with_all_stores(build)


def _live_chain_records(view: RegistryView) -> Dict[str, Tuple[set, set, Dict[str, Any]]]:
    """Live (non-retired) chains whose record reads ok, with every token hash it lists. A chain
    whose record does not read is NOT listed here; ``known_material`` reports it as a gap."""
    from hermes_cli.auth_codex_evidence import record_material
    out = {}
    for cid, entry in view.chains.items():
        if entry.get("state") == STATE_RETIRED:
            continue
        rec = read_record(store_path_of(entry["owner"]), cid)
        if rec.status == "ok":
            material = record_material(rec.data)
            out[cid] = (material["access"], material["refresh"], rec.data)
    return out


_UNSET = object()


def import_pair(access: str, refresh: str, *, reason: str, last_refresh: Optional[str] = None,
                set_active: bool = True, expected_singleton_access: Any = _UNSET) -> Optional[Tuple[str, str]]:
    """RULE-A1: identifying a chain is not accepting its material. Returns the pair to use, or None
    when the import is refused. Never writes predecessor material; never creates a second
    authority for a known chain."""
    access, refresh = str(access or "").strip(), str(refresh or "").strip()
    a_sha, r_sha, fp = token_sha(access), token_sha(refresh), pair_fp(access, refresh)

    def build(view, snaps):
        if view is None:
            logger.warning("Codex import refused (%s): the authority registry is damaged", reason)
            return None
        if expected_singleton_access is not _UNSET:
            target = snaps[store_key(active_store_path())]
            current_access = rows_mod.row_tokens(target.store, SINGLETON)[0] or None
            if current_access != expected_singleton_access:
                logger.info("Codex import skipped (%s): the credential was re-authenticated meanwhile.", reason)
                return None
        for cid, (a_set, r_set, record) in _live_chain_records(view).items():
            entry = view.chain(cid)
            attempt = record.get("attempt") or {}
            succ = attempt.get("successor") or {}
            if attempt.get("state") == "pending" and succ.get("access_token") == access:
                logger.warning("Codex import refused (%s): it is a pending successor (reconciled by the owner)", reason)
                return None
            if record.get("current_fp") != fp and a_sha not in a_set and r_sha not in r_set:
                continue
            if entry.get("state") != STATE_ACTIVE:
                logger.warning("Codex import refused (%s): that login is paused or retired", reason)
                return None
            owner = snaps.get(entry["owner"])
            current = next((rows_mod.row_tokens(owner.store, r["row"]) for r in entry["rows"]
                            if owner and r["store"] == entry["owner"]), None)
            if current is None:
                return None
            if pair_fp(*current) != record.get("current_fp"):
                return None
            logger.info("Codex import (%s) matched a known login; using its current tokens", reason)
            return current
        evidence = known_material(view)
        if evidence.retired(access, refresh):
            logger.warning("Codex import refused (%s): that token belongs to a Codex login Hermes already "
                           "replaced or removed; sign in again instead", reason)
            return None
        if not evidence.complete:
            logger.warning("Codex import refused (%s): Hermes can't check the token against its login history "
                           "(%s); sign in again instead", reason, "; ".join(evidence.gaps[:3]))
            return None
        if any_chain_fenced(view):
            logger.warning("Codex import refused (%s): a Codex login is paused; sign in again instead", reason)
            return None
        payload = plan_sign_in(view, snaps, store_key(active_store_path()), access, refresh, kind="singleton",
                               last_refresh=last_refresh, set_active=set_active, outcome=f"import:{reason}")
        run_op(payload, view)
        return (access, refresh)
    return _run_with_all_stores(build)


def remove_rows(rows: List[str], *, suppress: Iterable[str] = (), deactivate: bool = False,
                store: Optional[Path] = None) -> Dict[str, Any]:
    target = store_key(store or active_store_path())

    def build(view, snaps):
        if view is None:
            raise CodexRegistryUnreadable()
        payload = plan_removal(view, snaps, target, rows, suppress=suppress, deactivate=deactivate)
        run_op(payload, view)
        return {"removed": [e["row"] for e in payload.edits.get(target, []) if e["op"] == "remove_row"]}
    return _run_with_all_stores(build)


def codex_row_keys(store: Dict[str, Any]) -> List[str]:
    """Every Codex row of a store (singleton + every pool entry), for logout."""
    out = [SINGLETON] if rows_mod.singleton_state(store) is not None else []
    out += [rows_mod.pool_row(str(e["id"])) for e in rows_mod.pool_entries(store) or ()
            if isinstance(e, dict) and e.get("id")]
    return out


def logout_active_store() -> bool:
    from hermes_cli.auth import _load_auth_store
    store = _load_auth_store()
    keys = codex_row_keys(store)
    if not keys and store.get("active_provider") != PROVIDER:
        return False
    remove_rows(keys, deactivate=True)
    return True


def grant_retry_override(row: str = SINGLETON) -> Dict[str, Any]:
    target = store_key(active_store_path())

    def build(view, snaps):
        if view is None:
            raise CodexRegistryUnreadable()
        found = view.chain_for_row(target, row)
        if not found:
            root = store_key(store_path_of(target))
            raise CodexAuthorityRefused(f"No registered Codex login for {row} in {root}.")
        payload = plan_override(view, snaps, found[0])
        run_op(payload, view)
        return payload.result
    return _run_with_all_stores(build)


def migrate() -> Dict[str, Any]:
    """``hermes auth migrate``: bootstrap / roll forward, then register rows that are not yet."""
    def build(view, snaps):
        if view is None:
            raise CodexRegistryUnreadable()
        report = {"legacy_fenced": _fence_new_legacy_rescues()}
        view = read_registry()
        payload = plan_registration(view, snaps, only_unregistered=True, kind="adopt")
        if payload.chains:
            run_op(payload, view)
        report["registered"] = len(payload.chains)
        report["chains"] = len(read_registry().chains)
        return report
    return _run_with_all_stores(build)


def refuse_if_registered_store(store: Path, action: str) -> None:
    """RULE-P1 (stage 1): profile rename/delete/credential-bearing restore of a store that owns a
    chain or holds registered rows is refused."""
    view = read_registry()
    if view.status == "unreadable":
        raise CodexAuthorityRefused(
            f"The Codex authority file is damaged; {action} isn't possible until you sign in to Codex again.")
    key = store_key(store)
    for entry in view.chains.values():
        if entry.get("state") == "retired":
            continue  # a retired login's leftover rows hold nothing refreshable
        if entry.get("owner") == key or any(r.get("store") == key for r in entry.get("rows", ())):
            raise CodexAuthorityRefused(
                f"This profile holds your Codex login; {action} isn't supported for it yet.")


@contextmanager
def owner_store_exclusion(store: Path, action: str) -> Iterator[None]:
    """RULE-P1 for a profile move/delete/restore: the check runs under the migration lock, which is
    then HELD through the caller's filesystem operation. Every sign-in / import / bootstrap takes
    that lock first, so none can register a login in *store* between the check and the move."""
    with migration_lock():
        refuse_if_registered_store(store, action)
        yield


def report() -> Dict[str, Any]:
    """Read-only summary for ``hermes auth recovery list`` (no locks, no writes)."""
    view = read_registry()
    out: Dict[str, Any] = {"registry": view.status, "chains": [], "ops": list(view.ops)}
    for cid, entry in view.chains.items():
        out["chains"].append({"chain_id": cid, "state": entry.get("state"),
                              "reason": (entry.get("fence") or {}).get("reason"), "owner": entry.get("owner"),
                              "rows": [r["row"] for r in entry.get("rows", ())]})
    return out


_reported_unregistered: set = set()


def report_unregistered_rows() -> None:
    """RULE-K1: a Codex row missing from the registry is reported (once per process), never
    created, adopted or pruned at runtime. ``hermes auth migrate`` registers it."""
    try:
        from hermes_cli.auth import _load_auth_store
        view = read_registry()
        if not view.ok:
            return
        key = store_key(active_store_path())
        for row in rows_mod.codex_rows(_load_auth_store()):
            if view.chain_for_row(key, row) is None and (key, row) not in _reported_unregistered:
                _reported_unregistered.add((key, row))
                logger.warning("openai-codex credential %s in %s is not registered; run `hermes auth migrate`",
                               row, key)
    except Exception:
        logger.debug("Codex unregistered-row report failed", exc_info=True)
