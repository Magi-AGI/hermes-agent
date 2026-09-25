"""The Codex refresh transaction (design rev 9 §5), shared by every refresh entry point:

(P1) singleton refresh in ``resolve_codex_runtime_credentials``, (P2) the quota-probe refresh,
(P3) the credential-pool refresh. :func:`refresh_row` is the TOP-LEVEL entry: call it outside every
auth-store lock and pool mutex. It runs

1. lock-free admission (RULE-G1): registry present, no unfinished op on the chain, chain active,
   refresh redirected to the OWNER store; ABSENT registry / unfinished op -> dispatcher, once;
2. the store locks (owner; the caller's borrowing store too), budget ``max(lock timeout, refresh
   timeout + 5)``;
3. revalidation of the registry under the locks (changed -> start over once);
4. the chain-record reconcile (RULE-S1/S2);
5. the decision: a peer already moved the PAIR -> return the current rows, no network (RULE-F1);
6. intent (COMMITTED) -> the POST -> pending -> the rows -> the completion (RULE-F2). A pre-send
   failure (closed allowlist, decision 4) clears the intent; ``invalid_grant`` /
   ``refresh_token_reused`` / 401 fences ``rejected``; ANY other outcome after the request may have
   been sent (including HTTP 400/429/5xx and a 200 without a refresh token, RULE-V1) fences
   ``indeterminate`` — never an automatic replay.

A fence is an ownership decision: the transaction raises :class:`NeedsAuthorityRepair`, which
unwinds every lock scope (RULE-L3); :func:`refresh_row` applies it under the migration lock
(RULE-R1: only if still current) and raises the user-facing error.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from hermes_cli import auth_codex_rows as rows_mod
from hermes_cli.auth_codex_records import (
    ATTEMPT_INDETERMINATE, ATTEMPT_PENDING, ATTEMPT_REJECTED, completed, history_entry, new_attempt,
    read_record, reconcile, write_record)
from hermes_cli.auth_codex_registry import (
    OP_UNRECOVERABLE, STATE_FENCED, STATE_RETIRED, CodexAuthorityBusy, CodexRecoveryFenced,
    CodexRefreshIndeterminate, CodexRegistryUnreadable, CodexRotationPersistFailed, NeedsAuthorityRepair,
    RepairRequest, new_id, pair_fp, read_registry, store_locks, store_path_of, thread_holds_store_lock,
    unrecorded_legacy_rescues)
from hermes_cli.auth_constants import AUTH_LOCK_TIMEOUT_SECONDS, AuthError, httpx
from hermes_cli.auth_credential_io import CredentialWriteUncertain, WriteOutcome, store_key

logger = logging.getLogger("hermes_cli.auth")

# Decision 4: the ONLY failures that prove the refresh token never left this process.
_PRE_SEND_TYPES: Tuple[type, ...] = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol, httpx.ProxyError,
    httpx.PoolTimeout, socket.gaierror, ConnectionRefusedError)
_PRE_SEND_CODES = frozenset({"codex_auth_missing_refresh_token"})
_REJECTED_CODES = frozenset({"invalid_grant", "refresh_token_reused"})


@dataclass(frozen=True)
class RefreshResult:
    access_token: str
    refresh_token: str
    last_refresh: Optional[str] = None
    refreshed: bool = False  # this call spent the refresh token (network)
    peer: bool = False       # another caller had already moved the pair
    chain_id: Optional[str] = None


class _Restart(Exception):
    pass


def classify_refresh_failure(exc: BaseException) -> str:
    """``pre-send`` | ``rejected`` | ``indeterminate`` (closed allowlist, decision 4)."""
    if isinstance(exc, AuthError):
        if exc.code in _PRE_SEND_CODES:
            return "pre-send"
        if exc.code in _REJECTED_CODES or getattr(exc, "status_code", None) == 401:
            return "rejected"
        return "indeterminate"
    if isinstance(exc, _PRE_SEND_TYPES):
        return "pre-send"
    return "indeterminate"


def _fenced_error(cid: str, entry: dict) -> AuthError:
    reason = (entry.get("fence") or {}).get("reason") or "fenced"
    if entry.get("state") == STATE_RETIRED:
        return CodexRecoveryFenced("retired")
    if reason == "indeterminate":
        return CodexRefreshIndeterminate(chain_id=cid)
    return CodexRecoveryFenced(reason)


def _unrecoverable_message(op_id: str) -> str:
    return ("Hermes paused Codex token refresh to protect your login (an interrupted login update "
            f"could not be completed). Run `hermes auth recovery resolve {op_id[:8]}`, then sign in "
            "again with `hermes auth codex`.")


def _dispatch_once(round_: int) -> None:
    from hermes_cli.auth_codex_authority import REQUEST_DISPATCH_BUDGET_SECONDS, authority_dispatch
    if round_ == 0 and authority_dispatch(budget_seconds=REQUEST_DISPATCH_BUDGET_SECONDS) not in ("busy", "failed"):
        raise _Restart()
    raise CodexAuthorityBusy()


def _write_attempt(owner: Path, record: dict, attempt: Optional[dict], *, extra_history=None) -> Optional[str]:
    data = {**record, "attempt": attempt}
    if extra_history:
        data["history"] = [*record.get("history", []), extra_history]
    result = write_record(owner, data)
    return result.sha if result.outcome is WriteOutcome.COMMITTED else None


def _transaction(row_store: Path, row: str, started: Tuple[str, str], *, force: bool,
                 needs_refresh: Callable[[str], bool], timeout: float, refresh_timeout: Optional[float],
                 refresh_fn: Callable, round_: int) -> RefreshResult:
    from hermes_cli import auth as auth_mod

    # 1. lock-free admission (RULE-G1); an unfinished restore is fenced by the dispatcher first
    from hermes_cli.auth_codex_restore import restore_barrier_pending
    if restore_barrier_pending():
        _dispatch_once(round_)
    view = read_registry()
    if view.status == "absent":
        _dispatch_once(round_)
    if view.status == "unreadable":
        raise CodexRegistryUnreadable()
    row_key = store_key(row_store)
    found = view.chain_for_row(row_key, row)
    if found is None:
        raise CodexRecoveryFenced(
            "unregistered", "This Codex credential is not registered with Hermes's login registry yet; "
            "run `hermes auth migrate`, or sign in again.")
    cid, entry = found
    touching = view.ops_touching(cid)
    if touching and all(op.get("status") == OP_UNRECOVERABLE for _op_id, op in touching):
        # Kept until its payload reads again (the dispatcher retries it) or it is resolved.
        raise CodexRecoveryFenced("op-unrecoverable", _unrecoverable_message(touching[0][0]))
    if touching or entry.get("lifecycle") or unrecorded_legacy_rescues(view):  # every alias path (§7)
        _dispatch_once(round_)
    if entry.get("state") in (STATE_FENCED, STATE_RETIRED):
        raise _fenced_error(cid, entry)
    owner = store_path_of(entry["owner"])

    # 2. store locks (owner; plus the borrowing caller's store), L1 order
    with store_locks({owner, Path(row_store)}, timeout_seconds=timeout):
        # 3. revalidate
        again = read_registry()
        entry2 = again.chain(cid) if again.ok else None
        if (entry2 is None or entry2.get("epoch") != entry.get("epoch") or entry2.get("state") != entry.get("state")
                or entry2.get("owner") != entry.get("owner") or again.ops_touching(cid)):
            if round_ == 0:
                raise _Restart()
            raise CodexAuthorityBusy()
        epoch = entry2["epoch"]
        chain_rows = [r["row"] for r in entry2["rows"] if r["store"] == entry2["owner"]]
        store = auth_mod._load_auth_store(owner)
        fps = {rows_mod.row_fp(store, r) for r in chain_rows}
        rows_fp = next(iter(fps)) if len(fps) == 1 else None

        # 4. reconcile (RULE-S1/S2)
        record_view = read_record(owner, cid)
        decision = reconcile(record_view, epoch, rows_fp)
        record = dict(record_view.data)
        attempt = record.get("attempt") or {}
        if decision.action == "fence":
            observed = record_view.sha
            if record_view.status == "ok" and attempt.get("state") == "intent":
                # An intent found at start: the outcome is unknown. Make the fence sticky locally.
                observed = _write_attempt(owner, record, {**attempt, "state": ATTEMPT_INDETERMINATE}) or observed
            raise NeedsAuthorityRepair(
                RepairRequest(cid, epoch, decision.reason, entry2["owner"], observed, attempt.get("attempt_id")),
                CodexRefreshIndeterminate(chain_id=cid) if decision.reason == "indeterminate"
                else CodexRecoveryFenced(decision.reason))
        if decision.action == "adopt":
            succ = attempt["successor"]
            for r in chain_rows:
                rows_mod.set_row_tokens(store, r, succ["access_token"], succ["refresh_token"], succ.get("last_refresh"))
            auth_mod._save_auth_store(store, target_path=owner)
            decision_outcome = "adopted-after-crash"
        else:
            decision_outcome = "refreshed"
        if decision.action in ("adopt", "complete"):
            record = completed(record, attempt, decision_outcome)
            _write_attempt(owner, record, None)
            store = auth_mod._load_auth_store(owner)
        current = rows_mod.row_tokens(store, chain_rows[0])
        last_refresh = (rows_mod.get_row(store, chain_rows[0]) or {}).get("last_refresh")

        # 5. decide
        if pair_fp(*current) != pair_fp(*started):
            return RefreshResult(*current, last_refresh=last_refresh, peer=True, chain_id=cid)
        if not force and not needs_refresh(current[0]):
            return RefreshResult(*current, last_refresh=last_refresh, chain_id=cid)

        # 6. intent -> POST -> pending -> rows -> completion
        override = (attempt.get("override") or {}).get("grant_id") if decision.action == "override" else None
        attempt_id = new_id()
        intent = new_attempt(attempt_id, current, override_used=override)
        if _write_attempt(owner, record, intent) is None:
            check = read_record(owner, cid)
            if (check.data.get("attempt") or {}).get("attempt_id") != attempt_id:
                raise CodexRotationPersistFailed("the refresh intent could not be recorded; nothing was sent")
        try:
            if refresh_timeout is None:  # the pool/probe call shape (upstream used the default timeout)
                refreshed = refresh_fn(current[0], current[1])
            else:
                refreshed = refresh_fn(current[0], current[1], timeout_seconds=refresh_timeout)
        except BaseException as exc:
            kind = classify_refresh_failure(exc)
            if kind == "pre-send":
                _write_attempt(owner, record, None, extra_history=history_entry(
                    cid, "pre-send-failure", current, attempt_id=attempt_id,
                    evidence={"error": type(exc).__name__}))
                raise
            state = ATTEMPT_REJECTED if kind == "rejected" else ATTEMPT_INDETERMINATE
            marked = {**intent, "state": state, "evidence": {
                "error": type(exc).__name__, "code": getattr(exc, "code", None),
                "status": getattr(exc, "status_code", None)}}
            observed = _write_attempt(owner, record, marked)
            if not isinstance(exc, Exception):
                raise  # interrupt / exit: the verdict is recorded, the interrupt still propagates
            error = (exc if kind == "rejected" and isinstance(exc, AuthError)
                     else CodexRefreshIndeterminate(chain_id=cid))
            if kind == "rejected" and isinstance(error, AuthError):
                error.relogin_required = True
            raise NeedsAuthorityRepair(
                RepairRequest(cid, epoch, "rejected" if kind == "rejected" else "indeterminate",
                              entry2["owner"], observed, attempt_id), error) from exc
        succ = (str(refreshed["access_token"]).strip(), str(refreshed["refresh_token"]).strip())
        succ_refresh_at = refreshed.get("last_refresh")
        pending = {**intent, "state": ATTEMPT_PENDING,
                   "successor": {"access_token": succ[0], "refresh_token": succ[1], "last_refresh": succ_refresh_at},
                   "successor_fp": pair_fp(*succ)}
        if _write_attempt(owner, record, pending) is None:
            check = read_record(owner, cid)
            if (check.data.get("attempt") or {}).get("state") != ATTEMPT_PENDING:
                # The successor exists only in memory and the record still says "intent": the next
                # transaction could not tell what happened, so record the verdict now.
                raise NeedsAuthorityRepair(
                    RepairRequest(cid, epoch, "indeterminate", entry2["owner"], check.sha, attempt_id,
                                  {"reason": "successor could not be recorded"}),
                    CodexRefreshIndeterminate(chain_id=cid))
        store = auth_mod._load_auth_store(owner)
        for r in chain_rows:
            rows_mod.set_row_tokens(store, r, succ[0], succ[1], succ_refresh_at)
        try:
            auth_mod._save_auth_store(store, target_path=owner)
        except CredentialWriteUncertain:
            if rows_mod.row_fp(auth_mod._load_auth_store(owner), chain_rows[0]) != pair_fp(*succ):
                raise CodexRotationPersistFailed("auth.json may not have been updated")
        except Exception as exc:
            raise CodexRotationPersistFailed(str(exc)) from exc
        _write_attempt(owner, completed({**record, "attempt": pending}, pending, "refreshed"), None)
        return RefreshResult(*succ, last_refresh=succ_refresh_at, refreshed=True, chain_id=cid)


def _apply_repair_safely(need: NeedsAuthorityRepair) -> None:
    """RULE-L3 handler body. When this thread still holds a store lock (a caller up the stack), the
    registry write cannot be taken here; the chain record already carries the verdict, so the next
    transaction requests the same fence."""
    if thread_holds_store_lock():
        logger.warning("Codex fence for chain %s deferred: called inside an auth-store lock", need.request.chain_id)
        return
    from hermes_cli.auth_codex_ops import apply_repair
    try:
        apply_repair(need.request)
    except Exception:
        logger.warning("Codex fence for chain %s could not be recorded yet", need.request.chain_id, exc_info=True)


def refresh_row(row_store: Path, row: str, started: Tuple[str, str], *, force: bool,
                needs_refresh: Callable[[str], bool], timeout_seconds: float,
                pass_timeout: bool = True, refresh_fn: Optional[Callable] = None) -> RefreshResult:
    """Top-level entry (P1/P2/P3): outside every store lock and pool mutex.

    *refresh_fn* is the token-endpoint call; each entry point passes the seam it always used
    (P1/P3: ``hermes_cli.auth.refresh_codex_oauth_pure``, P2: ``auth_codex``'s own global)."""
    if refresh_fn is None:
        def refresh_fn(*args, **kwargs):
            from hermes_cli import auth as auth_mod
            return auth_mod.refresh_codex_oauth_pure(*args, **kwargs)
    budget = max(float(AUTH_LOCK_TIMEOUT_SECONDS), float(timeout_seconds) + 5.0)
    for round_ in range(2):
        try:
            return _transaction(Path(row_store), row, started, force=force, needs_refresh=needs_refresh,
                                timeout=budget, refresh_timeout=float(timeout_seconds) if pass_timeout else None,
                                refresh_fn=refresh_fn, round_=round_)
        except _Restart:
            continue
        except NeedsAuthorityRepair as need:
            _apply_repair_safely(need)
            raise need.error from None
    raise CodexAuthorityBusy()
