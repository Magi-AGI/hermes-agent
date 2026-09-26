"""Stage-1 restore rule: a restore NEVER brings Codex credential material back.

Nothing in a backup proves that a Codex refresh token is still unspent, and restoring the credential
authority's own files would roll its evidence back (an ACTIVE entry for a retired chain, a consumed
token recognised as new). So every restore path — backup import, quick-snapshot restore, profile
import, ``_external/`` members — treats Codex material specially, by BOTH the logical and the
canonical destination of each member (``backup._member_destination`` for archives) and the identity
of an existing file, against the canonical forms of every protected location; the most protected
verdict wins, so an alias never erases a credential file's identity. Credential classification
precedes every extension/type handler of the restore paths:

* protocol files (registry and its preserved copies, retired-token index, ``auth.recovery/``,
  ``auth.ops/``, legacy rescue files) and Codex CLI credentials (``.codex/`` / ``$CODEX_HOME``) are
  never written;
* runtime lock files are never written (``is_runtime_lock``);
* the protected inventory is complete or the write is refused (a location that cannot be inspected
  is never treated as absent), and no restore path writes THROUGH an existing file: every restored
  file is published by write-temp + fsync + replace, so another hard link keeps its content;
* an ``auth.json`` is written ONLY at one of the protected authority's own stores, and as one
  read-merge-write under the migration lock + that store's lock with the strict writer: the
  restored content with ALL Codex material removed (a legacy-format store first converted by
  upstream's own conversion), plus the destination's CURRENT Codex material exactly as it is. A
  live Codex login therefore survives a restore untouched, and no old token ever lands; every other
  provider restores as upstream does.

After restoring a backup a Codex user signs in again. Restoring Codex logins (paused until proven)
is stage 2 (design B3).
"""

from __future__ import annotations

import contextvars
import errno
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, Iterator, List, Optional, Tuple

from hermes_cli.auth_codex_registry import (
    LEGACY_RESCUE_NAME, MIGRATION_LOCK_NAME, OPS_DIR_NAME, PROVIDER, RECORDS_DIR_NAME, REGISTRY_NAME,
    CodexAuthorityRefused, migration_lock, registry_path)
from hermes_cli.auth_credential_io import canonical_path

AUTH_STORE_NAME = "auth.json"
CODEX_CLI_DIR = ".codex"
SIGN_IN_AGAIN = ("Codex logins are never restored from a backup (only this machine's current Codex login is "
                 "kept). If you use Codex, sign in again with `hermes auth codex`.")


def _names() -> Tuple[str, ...]:
    from hermes_cli.auth_codex_evidence import RETIRED_INDEX_NAME
    return (REGISTRY_NAME, RETIRED_INDEX_NAME, MIGRATION_LOCK_NAME, LEGACY_RESCUE_NAME)


def _norm(text: str) -> str:
    return os.path.normcase(text)


def _key(path: Path) -> str:
    return _norm(str(path))


def _destination(path: Path) -> Path:
    """The file a write to *path* really lands in: symlinks and junctions resolved (a missing tail
    stays as given); comparisons below are case-normalized where the platform is."""
    return canonical_path(path)


def _logical(path: Path) -> Path:
    """The destination as NAMED (absolute, nothing resolved): an alias must never erase what the
    name says it is."""
    return Path(os.path.abspath(os.fspath(path)))


# Runtime lock files of the credential protocol. A restore never extracts one: the restorer holds
# the migration lock while it writes, and replacing a held lock file (a new inode on POSIX) lets
# another process lock the replacement and act underneath the restore.
_RUNTIME_LOCK_NAMES = ("auth.lock", MIGRATION_LOCK_NAME, "nous_auth.lock", ".backup.lock")
_LIFECYCLE_LOCK_DIR = ".locks"  # <root>/profiles/.locks/<profile>.lock (stage 2)


@dataclass(frozen=True)
class _Protected:
    """The protected authority's credential locations in CANONICAL, case-normalized form, so a
    destination that reaches one through an alias (symlink, junction, differently named file) is
    recognised whatever it is called. ``skip_dirs`` hold protocol state (every store's
    ``auth.recovery/``, ``auth.ops/``, the profile lifecycle locks) and Codex CLI homes."""
    skip_dirs: FrozenSet[str]
    codex_dirs: FrozenSet[str]
    codex_files: FrozenSet[str]
    protocol_files: FrozenSet[str]
    lock_files: FrozenSet[str]
    store_files: FrozenSet[str]


_classification: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "codex_restore_classification", default=None)


@contextmanager
def classification_scope() -> Iterator[None]:
    """Compute the protected locations once for one restore pass (a fresh scope every time: the
    guarded re-check under the migration lock never reuses what an earlier pass saw)."""
    token = _classification.set({})
    try:
        yield
    finally:
        _classification.reset(token)


def _codex_homes() -> List[Path]:
    homes = [Path.home() / CODEX_CLI_DIR]
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        homes.append(Path(os.path.expanduser(codex_home)))
    return homes


def _missing(exc: OSError) -> bool:
    return isinstance(exc, (FileNotFoundError, NotADirectoryError)) or exc.errno in (errno.ENOENT, errno.ENOTDIR)


def _refuse_incomplete(what: str, exc: object) -> CodexAuthorityRefused:
    return CodexAuthorityRefused(
        f"This restore can't tell which files hold this installation's credentials ({what}: {exc}); "
        "nothing was restored. Fix the access problem (or run `hermes auth migrate --rebuild` for an "
        "unreadable registry) and restore again.")


def _authority_stores() -> List[Path]:
    """Every store of the protected authority: root, active, each profile directory's auth.json
    (present or not — a profile's protocol directories may exist before its store does), and every
    store the REGISTRY names (chain owners, their rows' stores, unfinished ops' stores): a registered
    home outside ``profiles/`` belongs to this authority whichever home runs the restore. An
    enumeration that fails (anything but absence) raises :class:`CodexAuthorityRefused`."""
    from hermes_cli.auth_codex_registry import active_store_location, profiles_root, read_registry, root_store_path
    stores = [root_store_path(), active_store_location()]
    directory = profiles_root()
    try:
        with os.scandir(directory) as entries:
            dirs = sorted(Path(e.path) for e in entries if e.is_dir())
    except OSError as exc:
        if not _missing(exc):
            raise _refuse_incomplete(f"the profiles directory {directory}", exc) from exc
        dirs = []
    stores.extend(d / AUTH_STORE_NAME for d in dirs)
    view = read_registry()
    if view.status == "unreadable":
        raise _refuse_incomplete(f"the credential registry {registry_path()}", view.error)
    keys = set()
    for entry in view.chains.values():
        keys.add(entry.get("owner"))
        keys.update(r.get("store") for r in entry.get("rows", ()))
    for op in view.ops.values():
        keys.update(op.get("stores") or ())
    stores.extend(Path(k) for k in sorted(k for k in keys if isinstance(k, str) and k))
    return stores


def _protected() -> _Protected:
    cache = _classification.get()
    if cache is not None and "protected" in cache:
        return cache["protected"]
    from hermes_cli.auth_codex_evidence import retired_index_path
    from hermes_cli.auth_codex_registry import legacy_rescue_path, ops_dir, profiles_root, root_home

    def canon(p: Path) -> str:
        return _key(canonical_path(p))

    root = root_home()
    skip_dirs = {canon(ops_dir()), canon(profiles_root() / _LIFECYCLE_LOCK_DIR)}
    codex_dirs, codex_files = set(), set()
    for home in _codex_homes():
        codex_dirs.update((canon(home), _key(_logical(home))))
        codex_files.add(canon(home / AUTH_STORE_NAME))
    protocol_files = {canon(registry_path()), canon(retired_index_path()), canon(root / LEGACY_RESCUE_NAME)}
    lock_files = {canon(root / MIGRATION_LOCK_NAME), canon(root / "auth.lock")}
    store_files = set()
    for store in _authority_stores():
        real = canonical_path(store)
        store_files.add(_key(real))
        # The runtime keeps a store's records beside its CANONICAL auth.json (records_dir) and looks
        # for rescues at every alias; its lock is the canonical store's ``.lock`` sibling.
        skip_dirs.update((canon(real.parent / RECORDS_DIR_NAME), canon(Path(store).parent / RECORDS_DIR_NAME)))
        protocol_files.update((canon(legacy_rescue_path(store)), canon(legacy_rescue_path(real))))
        lock_files.add(canon(real.with_suffix(".lock")))
    found = _Protected(frozenset(skip_dirs), frozenset(codex_dirs), frozenset(codex_files), frozenset(protocol_files),
                       frozenset(lock_files), frozenset(store_files))
    if cache is not None:
        cache["protected"] = found
    return found


def _identities(prot: _Protected) -> Tuple[Dict[Tuple[int, int], str], Optional[str]]:
    """``((st_dev, st_ino) -> kind, problem)`` for every protected file (hard links share it): the
    stores, protocol files and everything under the protocol directories, the locks, and the Codex
    CLI credential files (``auth.json`` of each CLI home; its sessions are not scanned). *problem*
    names the first protected location that could not be INSPECTED (absence is not a problem): the
    inventory is then incomplete and a multi-link destination it cannot rule out is refused. Built
    only when a destination has more than one link, once per classification scope."""
    cache = _classification.get()
    if cache is not None and "identities" in cache:
        return cache["identities"]
    out: Dict[Tuple[int, int], str] = {}
    problems: List[str] = []

    def add(path: str, kind: str) -> None:
        try:
            st = os.stat(path)
        except OSError as exc:
            if not _missing(exc):
                problems.append(f"{path}: {exc}")
            return
        if not stat.S_ISDIR(st.st_mode):
            out.setdefault((st.st_dev, st.st_ino), kind)

    def walk_error(exc: OSError) -> None:
        if not _missing(exc):
            problems.append(f"{exc.filename}: {exc}")

    for path in prot.lock_files:
        add(path, "lock")
    for path in prot.codex_files:
        add(path, "codex-cli")
    for path in prot.protocol_files:
        add(path, "protocol")
    for directory in prot.skip_dirs:
        for dirpath, _dirs, files in os.walk(directory, onerror=walk_error):
            for name in files:
                add(os.path.join(dirpath, name), "protocol")
    for path in prot.store_files:
        add(path, "store")
    found = (out, problems[0] if problems else None)
    if cache is not None:
        cache["identities"] = found
    return found


def _kind_by_location(path: Path, prot: _Protected) -> Optional[str]:
    """What *path* (logical OR canonical) is, by its name, its directory and the protected
    locations: ``lock``, ``codex-cli``, ``protocol``, ``store`` or None."""
    key = _key(path)
    parts = [_norm(p) for p in path.parts]
    name = parts[-1] if parts else ""
    ancestors = {_key(p) for p in path.parents}
    if (name in {_norm(n) for n in _RUNTIME_LOCK_NAMES} or key in prot.lock_files
            or (name.startswith(_norm("auth.")) and name.endswith(_norm(".lock")))
            or (name.endswith(_norm(".lock")) and _norm(_LIFECYCLE_LOCK_DIR) in parts[:-1])):
        return "lock"
    if _norm(CODEX_CLI_DIR) in parts[:-1] or ancestors & prot.codex_dirs or key in prot.codex_files:
        return "codex-cli"
    if (any(name == _norm(n) or name.startswith(_norm(f"{n}.")) for n in _names())
            or any(p in (_norm(OPS_DIR_NAME), _norm(RECORDS_DIR_NAME)) for p in parts[:-1])
            or key in prot.protocol_files or ancestors & prot.skip_dirs):
        return "protocol"
    if name == _norm(AUTH_STORE_NAME) or key in prot.store_files:
        return "store"
    return None


# ``unknown``: the destination exists but whether it IS a protected file could not be established
# (it could not be inspected, or it has other links and the protected inventory is incomplete).
_PRECEDENCE = ("lock", "codex-cli", "protocol", "unknown", "store")


def _classify(path: Path) -> Optional[str]:
    """``lock``, ``codex-cli``, ``protocol``, ``unknown``, ``store`` or None for a write to *path*,
    judged by BOTH its logical destination (the name) and its canonical destination (where the bytes
    land), plus the identity of an existing file (hard links). The most protected verdict wins, so
    an alias can never erase a credential file's identity in either direction."""
    prot = _protected()
    logical, dest = _logical(Path(path)), _destination(Path(path))
    kinds = {_kind_by_location(logical, prot), _kind_by_location(dest, prot)}
    try:
        st = os.stat(dest)
    except OSError as exc:
        if not _missing(exc):
            kinds.add("unknown")
    else:
        if st.st_nlink > 1 and not stat.S_ISDIR(st.st_mode):
            identities, problem = _identities(prot)
            kind = identities.get((st.st_dev, st.st_ino))
            kinds.add(kind if kind is not None or problem is None else "unknown")
    return next((k for k in _PRECEDENCE if k in kinds), None)


def is_runtime_lock(path: Path) -> bool:
    """A protocol lock file by its logical or canonical destination or identity: the migration
    lock, any auth store lock (``auth.lock`` / ``auth.*.lock`` / a store's canonical ``.lock``), the
    Nous shared-store lock, a profile lifecycle lock."""
    return _classify(path) == "lock"


def restore_action(path: Path) -> Optional[str]:
    """What a restore does with a member bound for *path*: ``skip`` (a Codex protocol file, a Codex
    CLI credential, a runtime lock: never written), ``refuse`` (an existing destination whose
    protection could not be established: never written, reported as an error), ``store`` (an
    auth.json: written through :func:`write_restored_store`) or None (an ordinary file: restored as
    upstream does, always published by atomic replace — never written through an existing file)."""
    kind = _classify(path)
    if kind in ("lock", "protocol", "codex-cli"):
        return "skip"
    if kind == "unknown":
        return "refuse"
    return "store" if kind == "store" else None


REFUSED_UNVERIFIABLE = ("not restored: it could not be checked against the credential files it might be "
                        "linked to")


# Files whose presence makes a directory an INDEPENDENT Hermes authority (its own registry, lock,
# evidence, ops or profiles): never part of the enclosing root's managed stores.
_INDEPENDENT_ROOT_MARKERS = (REGISTRY_NAME, MIGRATION_LOCK_NAME, "auth.chains.retired.json", OPS_DIR_NAME,
                             "profiles")


def _is_independent_root(directory: Path) -> bool:
    for marker in _INDEPENDENT_ROOT_MARKERS:
        try:
            if os.path.lexists(directory / marker):
                return True
        except OSError:
            return True  # cannot tell: treat as independent (refuse)
    return False


def _is_store_location(dest: Path) -> bool:
    """*dest* is one of the PROTECTED authority's own credential stores: its active store, its root
    ``auth.json``, or ``profiles/<id>/auth.json`` of a profile directory that is not an independent
    root. Decided by this structure, never by directory containment."""
    from hermes_cli.auth_codex_registry import active_store_location, root_home
    from hermes_constants import PROFILE_ID_RE
    if _norm(str(dest)) == _norm(str(canonical_path(active_store_location()))):
        return True
    root = canonical_path(root_home())
    root_parts = [_norm(p) for p in root.parts]
    parts = [_norm(p) for p in dest.parts]
    if parts[:len(root_parts)] != root_parts:
        return False
    rel, norm_rel = list(dest.parts[len(root_parts):]), parts[len(root_parts):]
    if norm_rel == [_norm(AUTH_STORE_NAME)]:
        return True
    return (len(rel) == 3 and norm_rel[0] == _norm("profiles") and norm_rel[2] == _norm(AUTH_STORE_NAME)
            and bool(PROFILE_ID_RE.match(rel[1])) and not _is_independent_root(root / rel[0] / rel[1]))


def _is_managed_store(target: Path) -> bool:
    """A restored auth.json bound for *target* may be merged there: its canonical destination is one
    of the protected authority's stores, or its LOGICAL destination is and the alias stays in the
    same directory (a profile's auth.json that is a differently named file beside it — the runtime
    reads that file as the profile's store). An alias that leaves the store's directory for an
    unmanaged place (another root) is refused."""
    dest = _destination(target)
    if _is_store_location(dest):
        return True
    logical = _logical(target)
    return _is_store_location(logical) and _key(dest.parent) == _key(canonical_path(logical.parent))


def check_restore(targets: Iterable[Tuple[Path, Callable[[], bytes]]], action: str) -> None:
    """Refuse (:class:`CodexAuthorityRefused`) a restore that would write an ``auth.json`` anywhere but
    one of the protected authority's own stores (another root, an independent root nested in this
    one, an unmanaged path): the merge below protects THIS authority only."""
    for target, _load in targets:
        if restore_action(Path(target)) != "store":
            continue
        if not _is_managed_store(Path(target)):
            raise CodexAuthorityRefused(
                f"The backup would write Hermes credentials outside this installation ({_destination(Path(target))}); "
                f"{action} isn't supported for them.")


def brings_codex_material(targets: Iterable[Tuple[Path, Callable[[], bytes]]]) -> bool:
    """Whether the restore carries Codex material that it will NOT restore (for the user notice)."""
    for target, load in targets:
        kind = _classify(Path(target))
        if kind in ("protocol", "codex-cli"):
            return True
        if kind == "store":
            try:
                if codex_material(json.loads(load().decode("utf-8-sig"))):
                    return True
            except Exception:
                return True
    return False


@contextmanager
def restore_guard(targets_fn: Callable[[], Iterable[Tuple[Path, Callable[[], bytes]]]],
                  action: str) -> Iterator[None]:
    """The migration lock is held across the check and every write of a restore that touches
    credential stores: no Codex sign-in, import or bootstrap can interleave with a store merge. The
    protected locations are recomputed under the lock and serve every write of the restore."""
    targets = list(targets_fn())
    with classification_scope():
        stores = any(restore_action(Path(t)) == "store" for t, _ in targets)
    if not stores:
        with classification_scope():
            yield
        return
    with migration_lock(), classification_scope():
        check_restore(targets, action)
        yield


# ── The Codex-stripping store merge ────────────────────────────────────────────────────────────

_CODEX_SECTIONS = ("providers", "credential_pool", "suppressed_sources")


def codex_material(store: Any) -> Dict[str, Any]:
    """The Codex material of a parsed store: ``{section: value}`` for every section holding it."""
    out: Dict[str, Any] = {}
    if not isinstance(store, dict):
        return out
    for section in _CODEX_SECTIONS:
        block = store.get(section)
        if isinstance(block, dict) and PROVIDER in block:
            out[section] = block[PROVIDER]
    if store.get("active_provider") == PROVIDER:
        out["active_provider"] = PROVIDER
    return out


def merge_without_codex(restored: Dict[str, Any], current: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """*restored* with all Codex material removed, plus *current*'s Codex material exactly as it is.
    A restored ``active_provider`` naming Codex yields to the destination's current one."""
    merged = json.loads(json.dumps(restored))
    from hermes_cli.auth import _auth_store_from_legacy, _is_modern_auth_store
    if not _is_modern_auth_store(merged):
        # Upstream's own conversion FIRST: once a Codex section is added below, the store reads as
        # the modern format and a legacy one (``systems.nous_portal``) would never be converted.
        merged = _auth_store_from_legacy(merged)
    for section in _CODEX_SECTIONS:
        block = merged.get(section)
        if isinstance(block, dict):
            block.pop(PROVIDER, None)
    current = current if isinstance(current, dict) else {}
    for section, value in codex_material(current).items():
        if section == "active_provider":
            continue
        merged.setdefault(section, {})
        if not isinstance(merged[section], dict):
            merged[section] = {}
        merged[section][PROVIDER] = json.loads(json.dumps(value))
    if merged.get("active_provider") == PROVIDER:
        if current.get("active_provider") is not None:
            merged["active_provider"] = current["active_provider"]
        else:
            merged.pop("active_provider", None)
    return merged


def strip_codex_bytes(data: bytes) -> Optional[bytes]:
    """A restored auth.json's bytes with its Codex material removed (no destination material to
    keep: a new store). None when the bytes do not parse (never written: they cannot be checked)."""
    from hermes_cli.auth_credential_io import encode_json
    try:
        restored = json.loads(data.decode("utf-8-sig"))
    except Exception:
        return None
    if not isinstance(restored, dict):
        return None
    return encode_json(merge_without_codex(restored, None))


def write_restored_store(dest: Path, data: bytes) -> Optional[str]:
    """Write a restored auth.json at *dest*: ONE read-merge-write under that store's lock (the caller
    holds the migration lock, :func:`restore_guard`) with the strict writer — the restored content
    without Codex material plus the destination's current Codex material, so the destination never
    lacks its Codex section. Returns None when written, else why it was not (the destination keeps
    its current file)."""
    from hermes_cli.auth import _auth_store_lock
    from hermes_cli.auth_credential_io import (
        encode_json, read_witnessed_bytes, require_committed, write_credential_file)
    try:
        restored = json.loads(data.decode("utf-8-sig"))
    except Exception:
        return "the backup's copy is not valid JSON, so its Codex material cannot be removed"
    if not isinstance(restored, dict):
        return "the backup's copy is not a Hermes credential store"
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with _auth_store_lock(target_path=Path(dest)):
        raw, _sha = read_witnessed_bytes(dest)
        current = None
        if raw is not None:
            try:
                current = json.loads(raw.decode("utf-8-sig"))
            except Exception:
                return "this machine's copy does not parse, so its Codex login could not be kept"
        require_committed(write_credential_file(dest, encode_json(merge_without_codex(restored, current))),
                          f"restored credential store {dest}")
    return None
