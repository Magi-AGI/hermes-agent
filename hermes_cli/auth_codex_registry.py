"""Codex Chain Authority Registry: who may refresh which Codex refresh-token chain.

Credential recovery protocol, stage 1 (design rev 9 §2). Two kinds of state are kept apart:

* OWNERSHIP (this module): ``<root home>/auth.chains.json``, written only with the strict writer and
  only under the migration lock. Changes only at ownership events (bootstrap migration, sign-in,
  retirement, removal, override), each bumping the chain's ownership ``epoch``; a refresh never
  writes it. Unfinished ownership ops (``ops``) are scoped reservations.
* TOKEN STATE (``auth_codex_records``): where each chain's rotation currently is, owner-local.

A chain is identified by ``chain_id``; its ``rows`` are physical credential rows
``{"store": <canonical auth.json>, "row": "singleton" | "pool:<id>"}`` (RULE-G2: a row belongs to
at most one chain). ``owner`` is the one store allowed to refresh it (RULE-G1).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from hermes_cli.auth_constants import AUTH_LOCK_TIMEOUT_SECONDS, AuthError
from hermes_cli.auth_credential_io import (
    encode_json, read_bytes_retrying, read_witnessed_bytes, require_committed, sha256_hex, store_key,
    write_credential_file)

logger = logging.getLogger("hermes_cli.auth")

PROVIDER = "openai-codex"
REGISTRY_VERSION = 1
REGISTRY_NAME = "auth.chains.json"
MIGRATION_LOCK_NAME = "auth.migration.lock"
OPS_DIR_NAME = "auth.ops"
RECORDS_DIR_NAME = "auth.recovery"
LEGACY_RESCUE_NAME = "auth.codex-rotation-rescue.json"
SINGLETON = "singleton"

STATE_ACTIVE, STATE_FENCED, STATE_RETIRED = "active", "fenced", "retired"
OP_RUNNING, OP_UNRECOVERABLE = "running", "unrecoverable"


# ── Errors (design §11) ────────────────────────────────────────────────────────────────────────

def _signin_command() -> str:
    from hermes_constants import profile_cli_selector
    return f"hermes {profile_cli_selector()}auth codex"


_FENCE_REASON_TEXT = {
    "unexpected-token": "the stored token does not match what Hermes last saved",
    "legacy-rescue": "an older Hermes left an unconfirmed token copy",
    "ambiguous-lineage": "the same login was found in several places and Hermes can't tell which copy is current",
    "unreadable-record": "the refresh record for this login is damaged",
    "registry-rebuilt": "the login registry was rebuilt",
    "op-unrecoverable": "an interrupted login update could not be completed",
    "rejected": "OpenAI rejected the saved refresh token",
    "retired": "this login was replaced by a newer sign-in",
    "unregistered": "this credential is not registered yet",
}


class CodexRecoveryFenced(AuthError):
    """Refresh of this Codex chain is paused to protect the login (``codex_recovery_fenced``)."""

    def __init__(self, reason: str, message: Optional[str] = None, *, relogin: bool = True) -> None:
        self.reason = reason
        if message is None:
            detail = _FENCE_REASON_TEXT.get(reason, reason)
            message = (
                f"Hermes paused Codex token refresh to protect your login ({detail}). Sign in again "
                f"with `{_signin_command()}` (a fresh browser or device-code login; Codex CLI import "
                "isn't accepted while paused).")
        super().__init__(message, provider=PROVIDER, code="codex_recovery_fenced", relogin_required=relogin)


class CodexRegistryUnreadable(CodexRecoveryFenced):
    def __init__(self) -> None:
        super().__init__(
            "registry-unreadable",
            "Hermes's Codex authority file is damaged. Sign in again with "
            f"`{_signin_command()}`: the damaged file is kept, every old Codex login stays paused, "
            "and the new sign-in is used from then on.")


class CodexAuthorityBusy(CodexRecoveryFenced):
    """Transient: an ownership update is in progress (never a credential verdict)."""

    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(
            "in-progress",
            message or "Hermes is updating your Codex login; it will be available again in a moment.",
            relogin=False)
        self.retryable = True


class CodexRefreshIndeterminate(AuthError):
    def __init__(self, message: Optional[str] = None) -> None:
        super().__init__(
            message or (
                "Couldn't confirm the Codex token refresh (the connection dropped after the request was "
                "sent). Hermes won't retry automatically. Sign in again with "
                f"`{_signin_command()}` (a fresh browser or device-code login), or run "
                f"`{_signin_command()} --retry-refresh` only if you're sure the refresh never "
                "reached OpenAI."),
            provider=PROVIDER, code="codex_refresh_indeterminate", relogin_required=True)


class CodexRotationPersistFailed(AuthError):
    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "Couldn't save the new Codex token; it will be retried on the next request."
            + (f" ({detail})" if detail else ""),
            provider=PROVIDER, code="codex_rotation_persist_failed", relogin_required=False)


class CodexAuthorityRefused(AuthError):
    """An ownership operation that stage 1 refuses (or that conflicts with an unfinished op)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, provider=PROVIDER, code="codex_authority_refused", relogin_required=False)


class CredentialTokenWriteRefused(RuntimeError):
    """A token/provenance change outside the authority protocol (RULE-T4) or a runtime Codex row
    creation/removal (RULE-K1)."""


@dataclass(frozen=True)
class RepairRequest:
    """RULE-R1: applied only while the chain's epoch, attempt and record sha still match."""

    chain_id: str
    epoch: int
    reason: str
    owner: str
    observed_record_sha: Optional[str]
    attempt_id: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None


class NeedsAuthorityRepair(Exception):
    """Raised inside a refresh transaction that needs an ownership-level change (a fence).

    Unwinds every store-lock scope to the entry point (RULE-L3), which applies the request under
    the migration lock and raises ``error`` to the caller."""

    def __init__(self, request: RepairRequest, error: AuthError) -> None:
        super().__init__(f"{request.chain_id}: {request.reason}")
        self.request = request
        self.error = error


# ── Paths and identity ─────────────────────────────────────────────────────────────────────────

def root_home() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def root_store_path() -> Path:
    return root_home() / "auth.json"


def active_store_path() -> Path:
    from hermes_cli.auth import _auth_file_path
    return _auth_file_path()


def registry_path() -> Path:
    return root_home() / REGISTRY_NAME


def ops_dir() -> Path:
    return root_home() / OPS_DIR_NAME


def is_root_store(key: str) -> bool:
    return key == store_key(root_store_path())


def store_path_of(key: str) -> Path:
    """The auth.json path a registry store key names. Keys are case-folded canonical paths; the
    live path (original case, which the deleted-profile guard compares) is preferred."""
    for candidate in (active_store_path(), root_store_path()):  # the hot path: no enumeration
        if store_key(candidate) == key:
            return candidate
    for candidate in all_store_paths():
        if store_key(candidate) == key:
            return candidate
    return Path(key)


def all_store_paths() -> List[Path]:
    """Root, the active store, and every live named profile's auth.json that exists (read-only
    enumeration)."""
    paths = [root_store_path(), active_store_path()]
    try:
        from hermes_cli.profiles import _iter_named_profile_dirs
        paths.extend(d / "auth.json" for d in _iter_named_profile_dirs() if (d / "auth.json").exists())
    except Exception:
        logger.debug("codex authority: profile enumeration failed", exc_info=True)
    seen, unique = set(), []
    for p in paths:
        k = store_key(p)
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique


def pair_fp(access_token: Any, refresh_token: Any) -> str:
    """RULE-F1: fingerprint of the COMPLETE pair (never the refresh token alone)."""
    return sha256_hex(f"{access_token or ''}".encode("utf-8") + b"\x00" + f"{refresh_token or ''}".encode("utf-8"))


def token_sha(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return sha256_hex(text.encode("utf-8")) if text else None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


# ── Registry view ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RegistryView:
    status: str  # "absent" | "unreadable" | "ok"
    data: Dict[str, Any] = field(default_factory=dict)
    sha: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def chains(self) -> Dict[str, Dict[str, Any]]:
        return self.data.get("chains", {}) if self.ok else {}

    @property
    def ops(self) -> Dict[str, Dict[str, Any]]:
        return self.data.get("ops", {}) if self.ok else {}

    def chain(self, chain_id: str) -> Optional[Dict[str, Any]]:
        return self.chains.get(chain_id)

    def chain_for_row(self, store: str, row: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        for cid, entry in self.chains.items():
            for r in entry.get("rows", ()):
                if r.get("store") == store and r.get("row") == row:
                    return cid, entry
        return None

    def ops_touching(self, chain_id: str, stores: Iterable[str] = ()) -> List[Tuple[str, Dict[str, Any]]]:
        stores = set(stores)
        hits = []
        for op_id, op in self.ops.items():
            chains = op.get("chains")
            if chains == "*" or chain_id in (chains or ()) or stores & set(op.get("stores") or ()):
                hits.append((op_id, op))
        return hits


def empty_registry() -> Dict[str, Any]:
    return {"version": REGISTRY_VERSION, "ops": {}, "chains": {}, "legacy_rescues": {}}


def validate_registry(data: Any) -> None:
    """Structural validation + RULE-G2 (each physical row in at most one chain)."""
    if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION:
        raise ValueError("registry: bad version")
    if not isinstance(data.get("ops"), dict) or not isinstance(data.get("chains"), dict):
        raise ValueError("registry: bad sections")
    if not isinstance(data.get("legacy_rescues", {}), dict):
        raise ValueError("registry: bad legacy_rescues")
    seen: Dict[Tuple[str, str], str] = {}
    for cid, entry in data["chains"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"registry: chain {cid} is not an object")
        if entry.get("state") not in (STATE_ACTIVE, STATE_FENCED, STATE_RETIRED):
            raise ValueError(f"registry: chain {cid} has a bad state")
        if not isinstance(entry.get("epoch"), int) or not isinstance(entry.get("owner"), str):
            raise ValueError(f"registry: chain {cid} lacks epoch/owner")
        rows = entry.get("rows")
        if not isinstance(rows, list):
            raise ValueError(f"registry: chain {cid} rows")
        for r in rows:
            key = (r.get("store"), r.get("row")) if isinstance(r, dict) else (None, None)
            if not all(isinstance(k, str) for k in key):
                raise ValueError(f"registry: chain {cid} has a malformed row")
            if key in seen:
                raise ValueError(f"registry: row {key} is in chains {seen[key]} and {cid} (RULE-G2)")
            seen[key] = cid


def read_registry() -> RegistryView:
    """Plain (lock-free) read. ABSENT and UNREADABLE are different (RULE-G3)."""
    path = registry_path()
    try:
        raw, sha = read_witnessed_bytes(path)
    except OSError as exc:
        return RegistryView("unreadable", error=str(exc))
    if raw is None:
        return RegistryView("absent")
    try:
        data = json.loads(raw.decode("utf-8"))
        validate_registry(data)
    except Exception as exc:
        return RegistryView("unreadable", sha=sha, error=str(exc))
    data.setdefault("legacy_rescues", {})
    return RegistryView("ok", data, sha)


def preserve_unreadable_registry() -> Optional[Path]:
    """RULE-G3: keep a timestamped copy of an unreadable registry (once per distinct content)."""
    path = registry_path()
    try:
        raw = read_bytes_retrying(path)
    except OSError:
        return None
    for existing in path.parent.glob(f"{REGISTRY_NAME}.corrupt.*"):
        try:
            if read_bytes_retrying(existing) == raw:
                return existing
        except OSError:
            continue
    target = path.with_name(f"{REGISTRY_NAME}.corrupt.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}")
    require_committed(write_credential_file(target, raw), f"copy of the damaged registry {target}")
    logger.warning("Codex authority registry %s is unreadable; preserved as %s", path, target)
    return target


# ── Locks ──────────────────────────────────────────────────────────────────────────────────────

_migration_holder = threading.local()


def migration_lock_held() -> bool:
    return getattr(_migration_holder, "depth", 0) > 0


def thread_holds_store_lock() -> bool:
    """True when this thread is inside any auth-store lock (then it may not take the migration
    lock: RULE-L1 orders the migration lock first)."""
    from hermes_cli import auth as auth_mod
    with auth_mod._auth_target_lock_holders_guard:
        holders = list(auth_mod._auth_target_lock_holders.values())
    return any(getattr(h, "depth", 0) > 0 for h in holders)


@contextmanager
def migration_lock(timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """The single ownership-change lock (``<root>/auth.migration.lock``), taken before any store
    lock (RULE-L1). Reentrant per thread."""
    from hermes_cli.auth import _file_lock
    if thread_holds_store_lock() and not migration_lock_held():
        raise RuntimeError("lock order: the migration lock is never taken inside a store lock")
    with _file_lock(root_home() / MIGRATION_LOCK_NAME, _migration_holder, timeout_seconds,
                    "Timed out waiting for the Codex authority migration lock"):
        yield


@contextmanager
def try_migration_lock(budget_seconds: float = 0.0) -> Iterator[bool]:
    """One bounded attempt (a short budget, never the full lock timeout); yields False when another
    process still holds it (or this thread holds a store lock, which forbids taking it)."""
    from hermes_cli.auth import _kernel_lock
    if migration_lock_held():
        with migration_lock():
            yield True
        return
    if thread_holds_store_lock():
        yield False
        return
    lock_path = root_home() / MIGRATION_LOCK_NAME
    try:
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(lock_path.parent)
        if os.name == "nt" and (not lock_path.exists() or lock_path.stat().st_size == 0):
            try:
                lock_path.write_text(" ", encoding="utf-8")
            except OSError:
                pass
        from hermes_cli.auth import _open_lock_file
        handle = _open_lock_file(lock_path, "r+" if os.name == "nt" else "a+",
                                 time.monotonic() + max(0.2, budget_seconds))
    except OSError:
        yield False
        return
    try:
        deadline = time.monotonic() + max(0.0, budget_seconds)
        while True:
            try:
                _kernel_lock(handle, True)
                break
            except (BlockingIOError, OSError, PermissionError):
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.05)
        _migration_holder.depth = 1
        try:
            yield True
        finally:
            _migration_holder.depth = 0
            try:
                _kernel_lock(handle, False)
            except OSError:
                pass
    finally:
        handle.close()


@contextmanager
def store_locks(paths: Iterable[Path], timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Distinct canonical stores in RULE-L1 order: profile stores sorted, root exactly once, last."""
    from hermes_cli.auth import _auth_store_lock
    by_key: Dict[str, Path] = {}
    for p in paths:
        by_key.setdefault(store_key(p), Path(p))
    root = store_key(root_store_path())
    ordered = [by_key[k] for k in sorted(k for k in by_key if k != root)]
    if root in by_key:
        ordered.append(by_key[root])
    with ExitStack() as stack:
        for p in ordered:
            stack.enter_context(_auth_store_lock(timeout_seconds, target_path=p))
        yield


def write_registry(data: Dict[str, Any]) -> str:
    """Write the registry (strict writer, COMMITTED required). Only under the migration lock."""
    if not migration_lock_held():
        raise RuntimeError("the Codex authority registry is written only under the migration lock")
    validate_registry(data)
    result = write_credential_file(registry_path(), encode_json(data))
    require_committed(result, "the Codex authority registry")
    return result.sha


def legacy_rescue_path(store: Path) -> Path:
    return Path(store).parent / LEGACY_RESCUE_NAME


def unrecorded_legacy_rescues(view: RegistryView, stores: Iterable[Path]) -> List[Tuple[str, str]]:
    """``(store key, sha)`` of rescue files (written by the withdrawn rev-0 code) not yet fenced."""
    known = view.data.get("legacy_rescues", {}) if view.ok else {}
    found = []
    for store in stores:
        rescue = legacy_rescue_path(store)
        try:
            _raw, sha = read_witnessed_bytes(rescue)
        except OSError:
            sha = "unreadable"
        if sha is None:
            continue
        key = store_key(store)
        if (known.get(key) or {}).get("sha") != sha:
            found.append((key, sha))
    return found


def now_monotonic() -> float:
    return time.monotonic()
