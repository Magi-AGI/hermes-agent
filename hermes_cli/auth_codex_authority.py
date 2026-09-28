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
* ``migrate(rebuild=True)`` — RULE-M6 reconstruction of an UNREADABLE registry, every chain fenced.

Profile rename / delete and transfers are ownership ops in ``auth_codex_profile_ops`` (stage 2).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    with store_locks(paths):  # a consistent snapshot of every store to plan from
        view = read_registry()
        payload = plan_registration(view, load_snapshots(paths), only_unregistered=False,
                                    fence_all=fence_all, kind="rebuild" if fence_all else "bootstrap")
    # PAYLOAD / DECISION / COMMIT need only the migration lock; APPLY takes each store's lock in
    # turn and its edits are conditional (RULE-M7). No Codex token can move meanwhile: refresh is
    # never admitted without a registry or while an op touches the chain.
    run_op(payload, view)
    logger.info("Codex authority registry %s: %d chain(s)", "rebuilt" if fence_all else "created",
                len(payload.chains))
    return {"chains": len(payload.chains)}


def _fence_new_legacy_rescues() -> List[str]:
    view = read_registry()
    paths = lockable_stores(all_store_paths())
    found = unrecorded_legacy_rescues(view)
    if not found:
        return []
    with store_locks(paths):
        snaps = load_snapshots(paths)
        candidates = {rescue: rescue_candidates(view.chains, snaps, store) for store, _sha, rescue in found}
        return record_legacy_rescues(found, candidates)


def _codex_material_present(paths: Iterable[Path]) -> bool:
    """Any Codex row or legacy rescue file anywhere (an ABSENT registry with neither is trivially
    consistent: nothing to govern, so nothing is written until the first Codex sign-in)."""
    from hermes_cli.auth import _load_auth_store
    from hermes_cli.auth_codex_registry import RescueScanIncomplete, rescue_locations
    try:
        if rescue_locations():
            return True
    except RescueScanIncomplete:
        return True  # cannot tell: let the bootstrap (which fails closed) surface it
    for path in paths:
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
    from hermes_cli.auth_codex_rescue import settle_legacy_rescues
    recover_ops()
    if read_registry().status == "absent":
        if not _codex_material_present(all_store_paths()):
            return "idle"
        _bootstrap()
        settle_legacy_rescues()
        return "bootstrapped"
    _fence_new_legacy_rescues()
    settle_legacy_rescues()
    return "done"


# A request that finds the registry absent / an unfinished op waits at most this long to ACQUIRE
# the migration lock (one attempt) — i.e. for another process that is recovering to finish — and
# otherwise gets the transient "updating your Codex login" error. It bounds the lock wait only: a
# request that gets the lock runs the dispatch pass itself (bootstrap / roll-forward) to completion.
REQUEST_DISPATCH_BUDGET_SECONDS = 5.0


def authority_dispatch(*, read_only: bool = False, budget_seconds: float = 0.0) -> str:
    """One pass (§6); ``budget_seconds`` bounds the migration-lock acquisition, not the pass.
    Returns ``skipped`` (read-only), ``busy`` (lock not acquired within the budget, or this thread
    holds a store lock), ``unreadable``, ``idle``, ``bootstrapped``, ``done`` or ``failed``."""
    if read_only:
        return "skipped"
    from hermes_cli.auth_credential_io import path_memo
    with try_migration_lock(budget_seconds) as acquired:
        if not acquired:
            return "busy"
        try:
            with path_memo():
                return dispatch_locked()
        except Exception:
            logger.warning("Codex authority dispatch failed", exc_info=True)
            return "failed"


def dispatch_needed() -> bool:
    """Unfinished ops to roll forward (incl. an ``unrecoverable`` one whose payload may read again),
    or payloads left by an op that never reached its decision. A ``lifecycle-stuck`` op (and its
    payload) is not work for automatic dispatch (RULE-P3)."""
    from hermes_cli.auth_codex_ops import unreferenced_payloads
    view = read_registry()
    return (view.status == "absent"
            or any(op.get("status") in ("running", "unrecoverable") for op in view.ops.values())
            or bool(unreferenced_payloads(view)))


def startup_dispatch() -> str:
    """CLI startup hook for runtime/mutating modes: only does work when there is some to do."""
    try:
        view = read_registry()
        paths = all_store_paths()
        if view.status == "absent":
            if not _codex_material_present(paths) and not ops_dir().is_dir():
                return "idle"
        elif not dispatch_needed() and not unrecorded_legacy_rescues(view):
            from hermes_cli.auth_codex_rescue import settlement_needed
            if not settlement_needed(view):
                return "idle"
    except Exception:
        # Whether there is work could not be established (a denied listing, an unreadable file):
        # never "idle" — the dispatch pass runs and reports what it can't do (review r6).
        logger.warning("Codex authority startup check failed; running the dispatcher", exc_info=True)
    return authority_dispatch()


# ── Ownership entry points ───────────────────────────────────────────────────────────────────

def _ready_view() -> RegistryView:
    """Registry ready for an ownership op (restore fenced, bootstrapped, unfinished ops rolled
    forward)."""
    recover_ops()
    view = read_registry()
    if view.status == "absent":
        _bootstrap()
        view = read_registry()
    return view


def lockable_stores(paths: Iterable[Path]) -> List[Path]:
    """*paths* minus the stores of profiles reserved by an op in a moving phase (a stuck rename's
    source): locking one would be refused, and no op may touch them anyway (RULE-G4 refuses any
    op whose scope intersects that reservation), so an ownership op elsewhere proceeds (r8-1)."""
    from hermes_cli.auth_codex_lifecycle import blocking_op, profile_of_store
    return [p for p in paths if blocking_op(profile_of_store(Path(p)), store_key(p)) is None]


def _refuse_moving_targets(targets: Iterable[Path]) -> None:
    """The op's own target store (the active one, a transfer destination) reserved by a moving op:
    refused with that op's message (the store-lock helper's own refusal, raised before any lock)."""
    from hermes_cli.auth_codex_lifecycle import refuse_blocked, blocking_op, profile_of_store
    for target in targets:
        profile = profile_of_store(target)
        found = blocking_op(profile, store_key(target))
        if found is not None:
            raise refuse_blocked(profile or str(target.parent), found)


def _run_with_all_stores(build, extra_paths: Iterable[Path] = ()) -> Any:
    """Migration lock, registry ready, ALL store locks (ownership ops are rare), then build + run."""
    if thread_holds_store_lock():
        raise RuntimeError("Codex ownership operations must not run inside an auth-store lock")
    from hermes_cli.auth_credential_io import path_memo
    with migration_lock(), path_memo():
        view = read_registry()
        if view.status == "unreadable":
            view = None
        else:
            view = _ready_view()
        paths = all_store_paths()
        targets = [Path(p) for p in (active_store_path(), *extra_paths)]
        for extra in targets:
            if store_key(extra) not in {store_key(p) for p in paths}:
                paths.append(extra)
        _refuse_moving_targets(targets)
        paths = lockable_stores(paths)
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
            _rebuild_fenced([s.path for s in snaps.values()])
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


def _override_target(view: RegistryView, snaps, target: str, chain: Optional[str]) -> str:
    """The chain ``--retry-refresh`` applies to: ``chain`` (id or unique prefix) when given, else the
    one login paused after an unconfirmed refresh that this store owns, holds rows of, or borrows
    (a profile without its own Codex rows reads root's)."""
    def paused(entry: Dict[str, Any]) -> bool:
        return entry.get("state") == "fenced" and (entry.get("fence") or {}).get("reason") == "indeterminate"

    if chain:
        matches = [cid for cid in view.chains if cid.startswith(chain.strip())]
        if len(matches) != 1:
            raise CodexAuthorityRefused(
                f"No single Codex login matches --chain {chain!r}; `hermes auth recovery list` shows them.")
        return matches[0]
    candidates = sorted(cid for cid in rescue_candidates(view.chains, snaps, target) if paused(view.chains[cid]))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise CodexAuthorityRefused(
            "No Codex login of this profile is paused after an unconfirmed refresh; "
            "`hermes auth recovery list` shows every login.")
    listed = ", ".join(c[:12] for c in candidates)
    raise CodexAuthorityRefused(
        f"Several Codex logins are paused after an unconfirmed refresh ({listed}); choose one with "
        "`--chain <id>`.")


def grant_retry_override(chain: Optional[str] = None) -> Dict[str, Any]:
    """RULE-O1 (``hermes auth codex --retry-refresh [--chain <id>]``). The target is resolved and
    the override decided under the migration lock + every store lock; it applies to the chain's
    OWNER record wherever that is."""
    target = store_key(active_store_path())

    def build(view, snaps):
        if view is None:
            raise CodexRegistryUnreadable()
        payload = plan_override(view, snaps, _override_target(view, snaps, target, chain))
        run_op(payload, view)
        return payload.result
    return _run_with_all_stores(build)


def _rebuild_fenced(stores: Iterable[Path]) -> None:
    """RULE-M6 (caller holds the migration lock; the registry is UNREADABLE). The damaged file is
    copied, the old generation's records are folded into the retired-token index, and the fenced
    reconstruction's DECISION then atomically replaces the damaged registry: it is never ABSENT in
    between (an absent registry would be bootstrapped with active chains)."""
    from hermes_cli.auth_codex_evidence import retain_orphan_records
    preserve_unreadable_registry()
    retain_orphan_records(list(stores))
    _bootstrap(fence_all="registry-rebuilt")


def migrate(*, rebuild: bool = False) -> Dict[str, Any]:
    """``hermes auth migrate``: bootstrap / roll forward, register rows that are not yet, and retire
    chains whose owner store no longer exists. ``rebuild=True`` (``--rebuild``) reconstructs an
    UNREADABLE registry with EVERY chain fenced (RULE-M6): matching token copies prove duplication,
    not usability, so only a sign-in (RULE-A2) or the retry override reactivates one. A sign-in over
    a damaged registry does the same reconstruction on its own; ``--rebuild`` exists for an install
    that must change profiles (rename, delete, restore) without signing in to Codex."""
    def build(view, snaps):
        if rebuild:
            if view is not None:
                raise CodexAuthorityRefused(
                    "The Codex login registry is readable; there is nothing to rebuild (`hermes auth migrate` "
                    "registers any login that is missing from it).")
            _rebuild_fenced([s.path for s in snaps.values()])
            return {"rebuilt": True, "chains": len(read_registry().chains)}
        if view is None:
            raise CodexRegistryUnreadable()
        from hermes_cli.auth_codex_rescue import settle_legacy_rescues
        report = {"legacy_fenced": _fence_new_legacy_rescues()}
        report["legacy_settled"] = settle_legacy_rescues()
        view = read_registry()
        payload = plan_registration(view, snaps, only_unregistered=True, kind="adopt")
        if payload.chains:
            run_op(payload, view)
        report["registered"] = len(payload.chains)
        report["retired_orphans"] = _retire_orphaned_chains()
        report["chains"] = len(read_registry().chains)
        return report
    return _run_with_all_stores(build)


def _retire_orphaned_chains() -> List[str]:
    """A chain whose OWNER store's directory is gone (the profile was removed outside Hermes) can
    never be refreshed again: it is retired as an ownership op (RULE-A3). Its record went with the
    directory, so the retired-token index marks its evidence incomplete (imports then fail closed).
    Only a directory that is genuinely ABSENT counts; one that cannot be inspected is kept. Chains
    reserved by an unfinished op are left to that op."""
    import errno
    import os
    from hermes_cli.auth_codex_ops import Payload, conflicting_op, retire_entry
    view = read_registry()
    orphans = []
    for cid, entry in view.chains.items():
        if entry.get("state") == STATE_RETIRED or conflicting_op(view, [cid], [entry["owner"]], []):
            continue
        try:
            os.stat(Path(entry["owner"]).parent)
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno in (errno.ENOENT, errno.ENOTDIR):
                orphans.append(cid)
    if not orphans:
        return []
    payload = Payload(kind="retire-orphaned", scope_chains=sorted(orphans),
                      scope_stores=sorted({view.chains[c]["owner"] for c in orphans}))
    for cid in orphans:
        entry = view.chains[cid]
        payload.chains[cid] = retire_entry(entry, [r for r in entry["rows"] if r["store"] != entry["owner"]])
        payload.records.append({"action": "archive", "owner": entry["owner"], "chain_id": cid,
                                "reason": "owner-missing", "rows": []})
    run_op(payload, view)
    logger.warning("Codex: retired %d login(s) whose profile directory no longer exists", len(orphans))
    return orphans


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
