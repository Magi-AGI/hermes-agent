"""Profile lifecycle locks, store-lock admission and continuation tokens (design rev 9 §4 RULE-G4/G5,
RULE-P2, §5 RULE-L1; credential recovery protocol stage 2).

A directory holding an open ``auth.lock`` cannot be renamed on Windows, so no in-directory lock may
guard a profile move. Each profile therefore has a **lifecycle lock** OUTSIDE its directory,
``<root>/profiles/.locks/<profile>.lock``: an ordinary exclusive, per-thread-reentrant file lock that
every acquisition of that profile's store lock takes IMMEDIATELY BEFORE the store lock (writers and
lock-taking readers alike, through ``auth._auth_store_lock``). A mover takes the migration lock, then
the lifecycle locks of every profile it touches (sorted), then store locks — the one RULE-L1 order —
so a writer waiting for a store lock never holds a lock the mover needs.

After the lifecycle lock is acquired the helper REVALIDATES: the store's directory still exists (a
stale caller never recreates a moved profile), and no unfinished op reserving the profile is in a
moving phase (``quiesced`` / ``moving``) — unless this caller presents that op's validated
**continuation token** (RULE-G5), which recovery / ``hermes auth recover --op`` / abandonment use to
act inside their own reservation. A token is validated under the migration lock: the op entry must
exist and its ``direction`` must equal the one requested, so a stale ``forward`` token after an
abandonment decision, or any token after the entry was removed, is refused.

Which profile a store belongs to is decided by where its lock file lives: the store's CANONICAL
parent directly under the canonical profiles root. A profile whose ``auth.json`` links to root's (or
whose directory is a junction elsewhere) keeps no lock inside its directory; its store lock takes no
lifecycle lock (RULE-L2).
"""

from __future__ import annotations

import contextvars
import os
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

from hermes_cli.auth_constants import AUTH_LOCK_TIMEOUT_SECONDS, ProfileBeingMoved, ProfileStoreMissing
from hermes_cli.auth_credential_io import canonical_path, store_key

LIFECYCLE_LOCK_DIR = ".locks"
# Phases in which an op's source directory may change under a lock or handle taken now.
BLOCKING_PHASES = frozenset({"quiesced", "moving"})
FORWARD, ABANDON = "forward", "abandon"


def _profiles_dir() -> Path:
    from hermes_cli.auth_codex_registry import root_home
    return root_home() / "profiles"


def lock_name(profile: str) -> str:
    return os.path.normcase(profile)


def lifecycle_lock_path(profile: str) -> Path:
    return _profiles_dir() / LIFECYCLE_LOCK_DIR / f"{lock_name(profile)}.lock"


def profile_of_store(auth_path: Path) -> Optional[str]:
    """The profile whose DIRECTORY holds this store's lock file (its canonical parent sits directly
    under the canonical profiles root), or None (root, a custom home, an alias leaving profiles/)."""
    real = canonical_path(auth_path)
    parent = real.parent
    if parent.name.startswith(".") or not parent.name:
        return None
    if os.path.normcase(str(parent.parent)) != os.path.normcase(str(canonical_path(_profiles_dir()))):
        return None
    return lock_name(parent.name)


_holders: Dict[str, threading.local] = {}
_holders_guard = threading.Lock()


def _holder(path: Path) -> threading.local:
    with _holders_guard:
        return _holders.setdefault(os.path.normcase(str(path)), threading.local())


def holds_lifecycle_lock(profile: str) -> bool:
    return getattr(_holder(lifecycle_lock_path(profile)), "depth", 0) > 0


@contextmanager
def lifecycle_locks(profiles: Iterable[str], timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """The lifecycle locks of *profiles* (distinct, sorted: RULE-L1). Reentrant per thread."""
    from hermes_cli.auth import _file_lock
    names = sorted({lock_name(p) for p in profiles if p})
    with ExitStack() as stack:
        for name in names:
            path = lifecycle_lock_path(name)
            stack.enter_context(_file_lock(path, _holder(path), timeout_seconds,
                                           f"Timed out waiting for the lifecycle lock of profile '{name}'"))
        yield


# ── Continuation tokens (RULE-G5) ─────────────────────────────────────────────────────────────

_continuation: "contextvars.ContextVar[Optional[Tuple[str, str]]]" = contextvars.ContextVar(
    "codex_op_continuation", default=None)


def op_direction(op: Dict[str, Any]) -> str:
    return op.get("direction") or FORWARD


@contextmanager
def continuation(op_id: str, direction: str) -> Iterator[Dict[str, Any]]:
    """Act inside op *op_id*'s own reservation. Validated under the migration lock (the caller also
    holds the op's lifecycle locks): the entry must exist with exactly this *direction*. Yields the
    op entry as validated."""
    from hermes_cli.auth_codex_registry import CodexAuthorityRefused, migration_lock_held, read_registry
    if not migration_lock_held():
        raise RuntimeError("a continuation token is validated only under the migration lock")
    view = read_registry()
    op = view.ops.get(op_id) if view.ok else None
    if op is None:
        raise CodexAuthorityRefused(f"Codex login update {op_id[:8]} is not unfinished; nothing to continue.")
    if op_direction(op) != direction:
        raise CodexAuthorityRefused(
            f"Codex login update {op_id[:8]} is now being completed as '{op_direction(op)}', not '{direction}'.")
    token = _continuation.set((op_id, direction))
    try:
        yield op
    finally:
        _continuation.reset(token)


def continues(op_id: str, op: Dict[str, Any]) -> bool:
    """This context holds a valid continuation token for *op* (same id, same direction as now)."""
    token = _continuation.get()
    return token is not None and token[0] == op_id and token[1] == op_direction(op)


# ── Reservations seen by the store-lock helper ────────────────────────────────────────────────

_ops_memo: Dict[str, Tuple[Tuple[int, int, int], Dict[str, Dict[str, Any]]]] = {}
_ops_memo_guard = threading.Lock()


def _unfinished_ops() -> Dict[str, Dict[str, Any]]:
    """The registry's unfinished ops, memoized by the file's identity (the strict writer replaces it,
    so every write changes the key): one ``stat`` per store-lock acquisition in the common case.

    An ABSENT registry has no ops. An UNREADABLE one yields none here too: the reservation check
    protects the MOVE of a directory, and no move (or its recovery) ever runs without a readable
    registry, while refresh admission refuses every Codex chain on its own (RULE-G3)."""
    from hermes_cli.auth_codex_registry import read_registry, registry_path
    path = registry_path()
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_ino, st.st_size, st.st_mtime_ns)
    cache_key = os.path.normcase(str(path))
    with _ops_memo_guard:
        hit = _ops_memo.get(cache_key)
    if hit is not None and hit[0] == key:
        return hit[1]
    view = read_registry()
    ops = dict(view.ops) if view.ok else {}
    with _ops_memo_guard:
        _ops_memo[cache_key] = (key, ops)
    return ops


def blocking_op(profile: Optional[str], key: Optional[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """The unfinished op in a moving phase that reserves *profile* or store *key* (and that this
    context does not continue), else None."""
    for op_id, op in _unfinished_ops().items():
        if op.get("phase") not in BLOCKING_PHASES or continues(op_id, op):
            continue
        profiles = {lock_name(p) for p in op.get("profiles") or () if isinstance(p, str)}
        if (profile and profile in profiles) or (key and key in set(op.get("stores") or ())):
            return op_id, op
    return None


def refuse_blocked(profile: str, found: Tuple[str, Dict[str, Any]]) -> ProfileBeingMoved:
    from hermes_cli.auth_codex_registry import OP_STUCK
    op_id, op = found
    return ProfileBeingMoved(profile, op_id, stuck=op.get("status") == OP_STUCK)


@contextmanager
def store_lock_admission(auth_path: Path, timeout_seconds: float) -> Iterator[None]:
    """Taken by ``_auth_store_lock`` BEFORE the store lock of a profile store: the lifecycle lock,
    then the revalidation (directory exists, not reserved by a moving op)."""
    profile = profile_of_store(auth_path)
    if profile is None:
        yield
        return
    with lifecycle_locks([profile], timeout_seconds):
        if not os.path.isdir(canonical_path(auth_path).parent):
            raise ProfileStoreMissing(Path(auth_path).parent)
        found = blocking_op(profile, store_key(auth_path))
        if found is not None:
            raise refuse_blocked(profile, found)
        yield


@contextmanager
def profile_publish_guard(profile: str, profile_dir: Path, action: str) -> Iterator[None]:
    """Creating / importing profile *profile* at *profile_dir*: under its lifecycle lock, refused
    while an unfinished op reserves that name or its store (RULE-G4, r8-1) and when the registry
    cannot be read (the reservations cannot be known). Held through the caller's publish."""
    from hermes_cli.auth_codex_registry import CodexAuthorityRefused, read_registry
    with lifecycle_locks([profile]):
        view = read_registry()
        if view.status == "unreadable":
            raise CodexAuthorityRefused(
                f"The Codex login registry is damaged, so {action} can't check for an unfinished profile "
                "move; run `hermes auth migrate --rebuild` (every Codex login stays paused) and try again.")
        from hermes_cli.auth_codex_ops import conflicting_op
        blocking = conflicting_op(view, [], [store_key(profile_dir / "auth.json")], [profile])
        if blocking is not None:
            raise CodexAuthorityRefused(reservation_message(view, blocking, action))
        yield


def reservation_message(view: Any, op_id: str, action: str) -> str:
    from hermes_cli.auth_codex_registry import OP_STUCK, OP_UNRECOVERABLE
    op = view.ops.get(op_id) or {}
    if op.get("status") == OP_UNRECOVERABLE:
        hint = f"`hermes auth recovery resolve {op_id[:8]}` releases it (its logins stay paused)"
    elif op.get("status") == OP_STUCK:
        hint = (f"run `hermes auth recover --op {op_id[:8]}` to finish it, or add `--abandon` to cancel it")
    else:
        hint = f"`hermes auth recover --op {op_id[:8]}` completes it"
    return (f"Another Codex login update ({op.get('kind')}, op {op_id[:8]}) is unfinished and reserves "
            f"what {action} would change; {hint}, then try again.")

