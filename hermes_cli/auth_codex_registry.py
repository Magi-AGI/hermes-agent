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

import contextvars
import errno
import hashlib
import json
import logging
import os
import stat
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
OP_RUNNING, OP_UNRECOVERABLE, OP_STUCK = "running", "unrecoverable", "lifecycle-stuck"


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
    "unverified-history": "Hermes could not check this credential against its login history",
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


class CodexStoreLockBusy(CodexAuthorityBusy):
    """Transient: the refresh could not acquire its store lock (another process held it). Nothing
    was sent; the credential's status and cooldown are untouched; a later attempt retries."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("Another Hermes process is updating this Codex login; it will be retried"
                         + (f" ({detail})." if detail else "."))


class CodexRefreshIndeterminate(AuthError):
    def __init__(self, message: Optional[str] = None, *, chain_id: Optional[str] = None) -> None:
        selector = f" --chain {chain_id[:12]}" if chain_id else ""
        super().__init__(
            message or (
                "Couldn't confirm the Codex token refresh (the connection dropped after the request was "
                "sent). Hermes won't retry automatically. Sign in again with "
                f"`{_signin_command()}` (a fresh browser or device-code login), or run "
                f"`{_signin_command()} --retry-refresh{selector}` only if you're sure the refresh never "
                "reached OpenAI."),
            provider=PROVIDER, code="codex_refresh_indeterminate", relogin_required=True)
        self.chain_id = chain_id


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

# The authority a protocol pass acts for: None = this process's own root (HERMES_HOME); set by
# authority_scope() for an operation on ANOTHER home (an explicit quick-snapshot restore).
_authority: "contextvars.ContextVar[Optional[Tuple[Path, Path]]]" = contextvars.ContextVar(
    "codex_authority", default=None)


def authority_root_for(home: Path) -> Path:
    """The Hermes root that owns *home* — the SAME resolver the runtime applies to HERMES_HOME
    (``hermes_constants.hermes_root_for_home``), so a restore into *home* and a later startup
    with ``HERMES_HOME=home`` always agree on the authority."""
    from hermes_constants import _expand_hermes_home, hermes_root_for_home
    return hermes_root_for_home(_expand_hermes_home(str(home)))


@contextmanager
def authority_scope(home: Path) -> Iterator[Path]:
    """Run protocol code (registry, locks, store enumeration, restore merges) for the authority that
    owns *home* instead of the process's own root. Yields that root."""
    from hermes_constants import _expand_hermes_home
    root = authority_root_for(home)
    token = _authority.set((root, _expand_hermes_home(str(home)) / "auth.json"))
    try:
        yield root
    finally:
        _authority.reset(token)


def root_home() -> Path:
    scoped = _authority.get()
    if scoped is not None:
        return scoped[0]
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def root_store_path() -> Path:
    return root_home() / "auth.json"


def active_store_path() -> Path:
    scoped = _authority.get()
    if scoped is not None:
        return scoped[1]
    from hermes_cli.auth import _auth_file_path
    return _auth_file_path()


def active_store_location() -> Path:
    """:func:`active_store_path` for path COMPARISON only (restore classification: the file is never
    opened through it), so it carries no test seat belt."""
    scoped = _authority.get()
    if scoped is not None:
        return scoped[1]
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "auth.json"


def profiles_root() -> Path:
    if _authority.get() is not None:
        return root_home() / "profiles"
    from hermes_cli.profiles import _get_profiles_root
    return _get_profiles_root()


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
    enumeration; computed once per protocol pass when a ``path_memo`` scope is active)."""
    from hermes_cli.auth_credential_io import memo
    cache = memo()
    if cache is not None and "__all_store_paths__" in cache:
        return list(cache["__all_store_paths__"])
    paths = [root_store_path(), active_store_path()]
    try:
        if _authority.get() is not None:  # another home's authority: its own profiles/ directory
            directory = profiles_root()
            profile_dirs = sorted(p for p in directory.iterdir() if p.is_dir() and not p.name.startswith(".")) \
                if directory.is_dir() else []
        else:
            from hermes_cli.profiles import _iter_named_profile_dirs
            profile_dirs = _iter_named_profile_dirs()
        paths.extend(d / "auth.json" for d in profile_dirs if (d / "auth.json").exists())
    except Exception:
        logger.debug("codex authority: profile enumeration failed", exc_info=True)
    seen, unique = set(), []
    for p in paths:
        k = store_key(p)
        if k not in seen:
            seen.add(k)
            unique.append(p)
    if cache is not None:
        cache["__all_store_paths__"] = list(unique)
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
    """True when this thread is inside any auth-store lock or profile lifecycle lock (then it may not
    take the migration lock: RULE-L1 orders the migration lock first)."""
    from hermes_cli import auth as auth_mod
    with auth_mod._auth_target_lock_holders_guard:
        holders = list(auth_mod._auth_target_lock_holders.values())
    if any(getattr(h, "depth", 0) > 0 for h in holders):
        return True
    from hermes_cli import auth_codex_lifecycle as lifecycle  # lifecycle locks also follow the migration lock
    with lifecycle._holders_guard:
        holders = list(lifecycle._holders.values())
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
    from hermes_cli.auth_codex_lifecycle import lifecycle_locks, profile_of_store
    with ExitStack() as stack:
        # RULE-L1: EVERY lifecycle lock of the plan (sorted) before ANY store lock. Taking each
        # store's lifecycle lock just before that store (interleaved) let a refresh hold a custom
        # home's store lock while waiting for a profile's lifecycle lock that a rename held while
        # waiting for that store (stage-2 review r1-4). Each store lock below re-enters its own.
        stack.enter_context(lifecycle_locks([profile_of_store(p) for p in ordered], timeout_seconds))
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


class RescueScanIncomplete(OSError):
    """A rescue location could not be CHECKED (an access / I/O error, not a missing path). The
    evidence may exist, so nothing may treat it as absent: refresh admission refuses (§7)."""


def _missing(exc: OSError) -> bool:
    return isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno in (errno.ENOENT, errno.ENOTDIR)


def _lstat_or_none(path: Path, what: str):
    """``os.lstat`` result, None when the path genuinely does not exist; any other failure raises
    :class:`RescueScanIncomplete` (fail closed)."""
    try:
        return os.lstat(path)
    except OSError as exc:
        if _missing(exc):
            return None
        raise RescueScanIncomplete(exc.errno, f"cannot check {what} {path}: {exc}") from exc


def rescue_locations() -> List[Tuple[Path, Path]]:
    """``(rescue file, store path)`` at EVERY path a store is reached by, enumerated BEFORE store
    aliases are collapsed (§7): root, the active store, and every directory under profiles/ —
    with or without its own auth.json (a borrowing profile's rescue belongs to root's grant) —
    beside the path as given AND beside its canonical file. A rescue written beside a profile whose
    auth.json links to root's is found here although that profile is the same store as root.
    Only EXISTING rescue files are returned, and only they are canonicalized: the scan runs on every
    refresh admission, so a location costs one ``lstat`` (plus one for a symlinked auth.json).
    Every directory under ``profiles/`` counts (identity markers are not required: a rescue beside a
    marker-less directory is still evidence).

    FAIL CLOSED: only a genuinely missing path (ENOENT / ENOTDIR) is absent. A rescue file whose
    metadata cannot be read is returned (its content check then records it ``unreadable`` and
    fences); a store whose link status, or a profiles directory whose listing, cannot be read
    raises :class:`RescueScanIncomplete`."""
    from hermes_cli.auth_credential_io import canonical_path
    stores = [root_store_path(), active_store_path()]
    directory = profiles_root()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.startswith(".") and entry.is_dir():
                    stores.append(Path(entry.path) / "auth.json")
    except OSError as exc:
        if not _missing(exc):
            raise RescueScanIncomplete(exc.errno, f"cannot list {directory}: {exc}") from exc
    seen, out = set(), []
    for store in stores:
        candidates = [legacy_rescue_path(store)]
        st = _lstat_or_none(store, "credential store")
        if st is not None and stat.S_ISLNK(st.st_mode):  # a file-level alias: its rescue may sit beside the target
            candidates.append(legacy_rescue_path(canonical_path(store)))
        for rescue in candidates:
            try:
                if _lstat_or_none(rescue, "rescue file") is None:
                    continue
            except RescueScanIncomplete:
                pass  # present but unreadable: it is evidence (recorded ``unreadable`` below)
            key = store_key(rescue)
            if key not in seen:
                seen.add(key)
                out.append((rescue, Path(store)))
    return out


def unrecorded_legacy_rescues(view: RegistryView,
                              locations: Optional[Iterable[Tuple[Path, Path]]] = None) -> List[Tuple[str, str, str]]:
    """``(store key, sha, rescue key)`` of rescue files (written by the withdrawn rev-0 code) not
    yet fenced. The store key maps each rescue to the CANONICAL store it belongs to; the rescue key
    (the file's own canonical identity) is what the registry records."""
    known = view.data.get("legacy_rescues", {}) if view.ok else {}
    found = []
    for rescue, store in (rescue_locations() if locations is None else locations):
        try:
            _raw, sha = read_witnessed_bytes(rescue)
        except OSError:
            sha = "unreadable"
        if sha is None:
            continue
        rescue_key = store_key(rescue)
        if (known.get(rescue_key) or {}).get("sha") != sha:
            found.append((store_key(store), sha, rescue_key))
    return found


def now_monotonic() -> float:
    return time.monotonic()
