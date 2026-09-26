"""Stage-1 refusal of restoring Codex credential state (credential recovery protocol, RULE-P1).

Every file of the protocol is credential state: ``auth.json`` (any store), the registry
``auth.chains.json`` (and its preserved copies), the retired-token index, chain records under
``auth.recovery/``, ownership payloads under ``auth.ops/``, the migration lock and legacy rescue
files. Restoring an older copy of any of them can roll the protocol back — an ACTIVE registry entry
for a chain that was retired, a refresh token that was already spent — so until stage 2 implements
restoration as an ownership operation that preserves fences, a restore (backup import, quick
snapshot, profile import) that would write such a file is REFUSED whenever this install already
has Codex authority state (newer protocol evidence is never overwritten or merged). Restoring onto
an install that never had any (a fresh machine) is allowed, but nothing in a backup proves its
refresh tokens are unspent: every Codex chain it brings in lands FENCED ``restored-from-backup``
behind a restore barrier (:func:`restore_guard`, :func:`fence_restored`); the exit is a fresh
sign-in. Other providers' credentials restore unchanged. Members are classified by their resolved
destination (``backup._member_destination`` for archives).

Callers run :func:`check_restore` and the file writes inside :func:`restore_guard`, which holds the
migration lock: no sign-in, import or bootstrap can register anything between the check and the
write (the check is re-run under the lock).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from hermes_cli.auth_codex_registry import (
    LEGACY_RESCUE_NAME, MIGRATION_LOCK_NAME, OPS_DIR_NAME, RECORDS_DIR_NAME, REGISTRY_NAME, RESTORE_BARRIER_NAME,
    CodexAuthorityRefused, all_store_paths, migration_lock, ops_dir, registry_path, restore_barrier_path)
from hermes_cli.auth_credential_io import canonical_path

AUTH_STORE_NAME = "auth.json"


def _names() -> Tuple[str, ...]:
    from hermes_cli.auth_codex_evidence import RETIRED_INDEX_NAME
    return (REGISTRY_NAME, RETIRED_INDEX_NAME, MIGRATION_LOCK_NAME, LEGACY_RESCUE_NAME, RESTORE_BARRIER_NAME)


def _norm(text: str) -> str:
    return os.path.normcase(text)


def _destination(path: Path) -> Path:
    """The file a write to *path* really lands in: symlinks and junctions resolved (a missing tail
    stays as given); comparisons below are case-normalized where the platform is."""
    return canonical_path(path)


def _known_credential_files() -> Iterable[Path]:
    """Root-level credential files a hard link elsewhere could alias (names catch the rest)."""
    from hermes_cli.auth_codex_evidence import retired_index_path
    from hermes_cli.auth_codex_registry import root_store_path
    return (root_store_path(), registry_path(), retired_index_path(), restore_barrier_path())


def _classify(path: Path) -> Optional[str]:
    """``store`` (an auth.json), ``protocol`` (a Codex authority file) or None, by the resolved
    destination — by name, directory, or identity with a known credential file (hard links)."""
    dest = _destination(path)
    parts = [_norm(p) for p in dest.parts]
    name = parts[-1] if parts else ""
    if name == _norm(AUTH_STORE_NAME):
        return "store"
    if any(name == _norm(n) or name.startswith(_norm(f"{n}.")) for n in _names()):
        return "protocol"
    if any(p in (_norm(OPS_DIR_NAME), _norm(RECORDS_DIR_NAME)) for p in parts[:-1]):
        return "protocol"
    if dest.exists():
        for known in _known_credential_files():
            try:
                if known.exists() and os.path.samefile(dest, known):
                    return "store" if _norm(known.name) == _norm(AUTH_STORE_NAME) else "protocol"
            except OSError:
                continue
    return None


# Runtime lock files of the credential protocol. A restore never extracts one: the restorer holds
# the migration lock while it extracts, and replacing a held lock file (a new inode on POSIX) lets
# another process lock the replacement and run recovery underneath the restore.
_RUNTIME_LOCK_NAMES = ("auth.lock", MIGRATION_LOCK_NAME, "nous_auth.lock", ".backup.lock")
_LIFECYCLE_LOCK_DIR = ".locks"  # <root>/profiles/.locks/<profile>.lock (stage 2)


def is_runtime_lock(path: Path) -> bool:
    """A protocol lock file, by its resolved destination: the migration lock, any auth store lock
    (``auth.lock`` / ``auth.*.lock``), the Nous shared-store lock, a profile lifecycle lock — or a
    file that IS the root's migration / store lock under another name (hard link)."""
    dest = _destination(path)
    name = _norm(dest.name)
    if name in {_norm(n) for n in _RUNTIME_LOCK_NAMES}:
        return True
    if name.startswith(_norm("auth.")) and name.endswith(_norm(".lock")):
        return True
    if name.endswith(_norm(".lock")) and _norm(_LIFECYCLE_LOCK_DIR) in [_norm(p) for p in dest.parts[:-1]]:
        return True
    if dest.exists():
        from hermes_cli.auth_codex_registry import root_home
        for known in (root_home() / MIGRATION_LOCK_NAME, root_home() / "auth.lock"):
            try:
                if known.exists() and os.path.samefile(dest, known):
                    return True
            except OSError:
                continue
    return False


def is_protocol_file(path: Path) -> bool:
    """A Codex authority protocol file (not ``auth.json`` itself), by its resolved destination."""
    return _classify(path) == "protocol"


def is_credential_state(path: Path) -> bool:
    return _classify(path) is not None


def authority_has_state() -> bool:
    """True once this install has Codex authority state of any kind (then restoring an older copy
    of credential state could roll it back)."""
    from hermes_cli.auth_codex_evidence import retired_index_path
    from hermes_cli.auth_codex_records import records_dir
    if (registry_path().exists() or retired_index_path().exists() or ops_dir().exists()
            or restore_barrier_path().exists()):
        return True
    return any(records_dir(p).is_dir() for p in all_store_paths())


def _has_codex_material(load: Callable[[], bytes]) -> bool:
    from hermes_cli import auth_codex_rows as rows_mod
    try:
        store = json.loads(load().decode("utf-8-sig"))
    except Exception:
        return True  # cannot tell: fail closed
    if not isinstance(store, dict):
        return True
    return any(any(rows_mod.row_tokens(store, row)) for row in rows_mod.codex_rows(store))


# Files whose presence makes a directory an INDEPENDENT Hermes authority (its own registry, lock,
# evidence, barrier, ops or profiles): never part of the enclosing root's managed locations.
_INDEPENDENT_ROOT_MARKERS = (REGISTRY_NAME, MIGRATION_LOCK_NAME, "auth.chains.retired.json", RESTORE_BARRIER_NAME,
                             OPS_DIR_NAME, "profiles")


def _is_independent_root(directory: Path) -> bool:
    for marker in _INDEPENDENT_ROOT_MARKERS:
        try:
            if os.path.lexists(directory / marker):
                return True
        except OSError:
            return True  # cannot tell: treat as independent (refuse)
    return False


def _relative_parts(dest: Path, base: Path) -> Optional[list]:
    base_parts = [_norm(p) for p in base.parts]
    parts = [_norm(p) for p in dest.parts]
    return parts[len(base_parts):] if parts[:len(base_parts)] == base_parts else None


def _managed_kind(dest: Path) -> Optional[str]:
    """``store`` / ``protocol`` when *dest* (resolved) is one of the PROTECTED authority's own managed
    credential locations, else None. The authority's locations are its root's auth.json and protocol
    files, and each profile store ``profiles/<id>/auth.json`` with its records and rescue file — a
    profile directory that carries its own authority markers is an independent root, not a store of
    this one. Membership is decided by this structure, never by directory containment."""
    from hermes_cli.auth_codex_registry import active_store_path, root_home
    from hermes_constants import PROFILE_ID_RE
    # The authority's ACTIVE store is one of its stores even when it is not ``<root>/auth.json``
    # or ``profiles/<id>/auth.json`` (HERMES_HOME = a directory beneath the root).
    active = canonical_path(active_store_path())
    if _norm(str(dest)) == _norm(str(active)):
        return "store"
    active_rel = _relative_parts(dest, active.parent)
    if active_rel and (active_rel == [_norm(LEGACY_RESCUE_NAME)]
                       or (len(active_rel) >= 2 and active_rel[0] == _norm(RECORDS_DIR_NAME))):
        return "protocol"
    root = canonical_path(root_home())
    root_parts = [_norm(p) for p in root.parts]
    parts = [_norm(p) for p in dest.parts]
    if parts[:len(root_parts)] != root_parts:
        return None
    rel = list(dest.parts[len(root_parts):])
    norm_rel = parts[len(root_parts):]
    if not rel:
        return None

    def classify_in(base_rel, allow_registry):
        if base_rel == [_norm(AUTH_STORE_NAME)]:
            return "store"
        if len(base_rel) >= 2 and base_rel[0] == _norm(RECORDS_DIR_NAME):
            return "protocol"
        if base_rel == [_norm(LEGACY_RESCUE_NAME)]:
            return "protocol"
        if allow_registry:
            if len(base_rel) >= 2 and base_rel[0] == _norm(OPS_DIR_NAME):
                return "protocol"
            if len(base_rel) == 1 and any(base_rel[0] == _norm(n) or base_rel[0].startswith(_norm(f"{n}."))
                                          for n in _names()):
                return "protocol"
        return None

    if norm_rel[0] == _norm("profiles"):
        if len(rel) < 3 or not PROFILE_ID_RE.match(rel[1]) or _is_independent_root(root / rel[0] / rel[1]):
            return None
        return classify_in(norm_rel[2:], allow_registry=False)
    return classify_in(norm_rel, allow_registry=True)


def check_restore(targets: Iterable[Tuple[Path, Callable[[], bytes]]], action: str) -> None:
    """Raise :class:`CodexAuthorityRefused` if restoring these ``(target path, bytes loader)`` pairs
    would overwrite Codex credential state unsafely (module docstring)."""
    from hermes_cli.auth_codex_authority import refuse_if_registered_store
    has_state: Optional[bool] = None
    for target, load in targets:
        kind = _classify(Path(target))
        if kind is None:
            continue
        target = _destination(Path(target))
        if _managed_kind(target) is None:
            # The state check, lock, barrier and fence below protect THIS authority only: a
            # credential store or protocol file anywhere else — another root, an independent root
            # nested in this one, an unmanaged path — would land there unprotected.
            raise CodexAuthorityRefused(
                f"The backup would write Hermes credentials outside this installation ({target}); "
                f"{action} isn't supported for them.")
        if kind == "store":
            refuse_if_registered_store(target, action)
        if has_state is None:
            has_state = authority_has_state()
        if not has_state:
            continue
        if kind == "protocol":
            raise CodexAuthorityRefused(
                f"The backup contains Hermes's Codex login records ({target.name}); {action} would roll "
                "your Codex login state back, which isn't supported yet. Restore without them, or sign "
                "in to Codex again after restoring.")
        if _has_codex_material(load):
            raise CodexAuthorityRefused(
                f"The backup's {target.name} holds Codex tokens that Hermes may already have used; "
                f"{action} isn't supported for them yet. Sign in to Codex again instead.")


def _brings_codex_material(targets: Iterable[Tuple[Path, Callable[[], bytes]]]) -> bool:
    for target, load in targets:
        kind = _classify(Path(target))
        if kind == "protocol" or (kind == "store" and _has_codex_material(load)):
            return True
    return False


@contextmanager
def restore_guard(targets_fn: Callable[[], Iterable[Tuple[Path, Callable[[], bytes]]]],
                  action: str) -> Iterator[None]:
    """Hold the migration lock across the check and the restore's writes (only when the restore
    touches credential state; otherwise no lock is taken).

    A restore that brings Codex credential material in (Codex tokens in an auth.json, or any
    protocol file) writes the RESTORE BARRIER before the first byte is extracted and fences every
    restored chain afterwards (:func:`fence_restored`). Until the fence is durable the barrier
    stays, and every dispatcher, ownership op and refresh admission resolves it first — so a crash
    at any step never leaves a restored credential refreshable."""
    targets = list(targets_fn())
    if not any(is_credential_state(t) for t, _ in targets):
        yield
        return
    with migration_lock():
        check_restore(targets, action)
        barrier = _brings_codex_material(targets)
        if barrier:
            write_restore_barrier(action, _restore_scope(targets))
        try:
            yield
        finally:
            if barrier:
                fence_restored()


# ── the restore barrier (credential recovery protocol, restores are never trusted) ─────────────

RESTORED_REASON = "restored-from-backup"


def _restore_scope(targets: Iterable[Tuple[Path, Callable[[], bytes]]]) -> Dict[str, list]:
    """Every destination store / protocol path the restore will write, resolved."""
    scope: Dict[str, list] = {"stores": [], "protocol": []}
    for target, _load in targets:
        kind = _classify(Path(target))
        if kind is not None:
            scope["stores" if kind == "store" else "protocol"].append(str(_destination(Path(target))))
    return scope


def write_restore_barrier(action: str, scope: Optional[Dict[str, list]] = None) -> None:
    """Committed BEFORE the first byte is extracted: the action and the COMPLETE scope (every
    destination store / protocol path, resolved), so recovery fences exactly what the restore could
    have written, whichever home the recovering process runs from."""
    from hermes_cli.auth_codex_registry import utc_now
    from hermes_cli.auth_credential_io import encode_json, require_committed, write_credential_file
    scope = scope or {"stores": [], "protocol": []}
    require_committed(write_credential_file(restore_barrier_path(), encode_json(
        {"version": 2, "action": action, "at": utc_now(), "stores": sorted(set(scope.get("stores", []))),
         "protocol": sorted(set(scope.get("protocol", [])))})), "the Codex restore barrier")


def _barrier_stores() -> List[Path]:
    """The destination stores the pending restore recorded. A barrier that cannot be read proves
    nothing about its scope: raise (the barrier stays; admission stays refused)."""
    from hermes_cli.auth_credential_io import read_bytes_retrying
    try:
        data = json.loads(read_bytes_retrying(restore_barrier_path()).decode("utf-8"))
        return [Path(p) for p in data.get("stores", [])]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError(f"the Codex restore barrier {restore_barrier_path()} is unreadable ({exc}); "
                           "restored logins stay paused until it can be read") from exc


def restore_barrier_pending() -> bool:
    return restore_barrier_path().exists()


def fence_restored() -> None:
    """Make every Codex chain the restore could have brought in durably FENCED
    (``restored-from-backup``), then clear the barrier. Idempotent; the caller holds the migration
    lock and no store lock. Nothing a backup holds proves its refresh tokens are still unspent, so
    no restored chain is ever refreshed: the exit is a fresh sign-in (``--retry-refresh`` refuses
    this fence). Ownership ops restored with the registry are never rolled forward: their entries
    are dropped and their payloads archived as ``restored`` (their chains are fenced here)."""
    import json as _json

    from hermes_cli.auth_codex_authority import _bootstrap, _codex_material_present
    from hermes_cli.auth_codex_evidence import retain_orphan_records
    from hermes_cli.auth_codex_ops import _archive_payload, run_op
    from hermes_cli.auth_codex_plans import load_snapshots, plan_registration
    from hermes_cli.auth_codex_registry import (
        STATE_RETIRED, preserve_unreadable_registry, read_registry, store_locks, write_registry)
    from hermes_cli.auth_credential_io import unlink_retrying

    from hermes_cli.auth_credential_io import store_key
    # Exactly the stores the restore recorded (whatever home THIS process runs from) plus its own
    # view; the barrier is cleared only after every one of them is fenced. Each step below is
    # idempotent and the registry records the progress, so a crash here just repeats it.
    paths = all_store_paths()
    known = {store_key(p) for p in paths}
    for store in _barrier_stores():
        if store_key(store) not in known and store.exists():
            known.add(store_key(store))
            paths.append(store)
    view = read_registry()
    if view.status == "unreadable":
        preserve_unreadable_registry()
        retain_orphan_records(paths)
        _bootstrap(fence_all=RESTORED_REASON, paths=paths)
    elif view.status == "absent":
        if _codex_material_present(paths):
            _bootstrap(fence_all=RESTORED_REASON, paths=paths)
    else:
        data = _json.loads(_json.dumps(view.data))
        for entry in data["chains"].values():
            if entry.get("state") != STATE_RETIRED and (entry.get("fence") or {}).get("reason") != RESTORED_REASON:
                entry.update(state="fenced", fence={"reason": RESTORED_REASON, "epoch": entry["epoch"]})
        data["ops"] = {}
        if data != view.data:
            write_registry(data)
        if ops_dir().is_dir():
            for payload in sorted(ops_dir().glob("*.json")):
                _archive_payload(payload, "restored")
        with store_locks(paths):
            view = read_registry()
            payload = plan_registration(view, load_snapshots(paths), only_unregistered=True,
                                        fence_all=RESTORED_REASON, kind="restore")
            if payload.chains:
                run_op(payload, view)
    try:
        unlink_retrying(restore_barrier_path())
    except FileNotFoundError:
        pass
