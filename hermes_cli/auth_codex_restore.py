"""Stage-1 restore rule: a restore NEVER brings Codex credential material back.

Nothing in a backup proves that a Codex refresh token is still unspent, and restoring the credential
authority's own files would roll its evidence back (an ACTIVE entry for a retired chain, a consumed
token recognised as new). So every restore path — backup import, quick-snapshot restore, profile
import, ``_external/`` members — treats Codex material specially, by the RESOLVED destination of
each member (``backup._member_destination`` for archives):

* protocol files (registry and its preserved copies, retired-token index, ``auth.recovery/``,
  ``auth.ops/``, legacy rescue files) and Codex CLI credentials (``.codex/`` / ``$CODEX_HOME``) are
  never written;
* runtime lock files are never written (``is_runtime_lock``);
* an ``auth.json`` is written ONLY at one of the protected authority's own stores, and as one
  read-merge-write under the migration lock + that store's lock with the strict writer: the
  restored content with ALL Codex material removed, plus the destination's CURRENT Codex material
  exactly as it is. A live Codex login therefore survives a restore untouched, and no old token
  ever lands; every other provider restores as upstream does.

After restoring a backup a Codex user signs in again. Restoring Codex logins (paused until proven)
is stage 2 (design B3).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Tuple

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


def _destination(path: Path) -> Path:
    """The file a write to *path* really lands in: symlinks and junctions resolved (a missing tail
    stays as given); comparisons below are case-normalized where the platform is."""
    return canonical_path(path)


def _known_credential_files() -> Iterable[Path]:
    """Root-level credential files a hard link elsewhere could alias (names catch the rest)."""
    from hermes_cli.auth_codex_evidence import retired_index_path
    from hermes_cli.auth_codex_registry import root_store_path
    return (root_store_path(), registry_path(), retired_index_path())


def _is_codex_cli_file(dest: Path) -> bool:
    if _norm(CODEX_CLI_DIR) in [_norm(p) for p in dest.parts[:-1]]:
        return True
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        base = [_norm(p) for p in canonical_path(Path(os.path.expanduser(codex_home))).parts]
        return [_norm(p) for p in dest.parts][:len(base)] == base
    return False


def _classify(path: Path) -> Optional[str]:
    """``store`` (an auth.json), ``protocol`` (a Codex authority file), ``codex-cli`` (a Codex CLI
    credential) or None, by the resolved destination — by name, directory, or identity with a
    known credential file (hard links)."""
    dest = _destination(path)
    parts = [_norm(p) for p in dest.parts]
    name = parts[-1] if parts else ""
    if _is_codex_cli_file(dest):
        return "codex-cli"
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
# the migration lock while it writes, and replacing a held lock file (a new inode on POSIX) lets
# another process lock the replacement and act underneath the restore.
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


def restore_action(path: Path) -> Optional[str]:
    """What a restore does with a member bound for *path*: ``skip`` (a Codex protocol file, a Codex
    CLI credential, a runtime lock: never written), ``store`` (an auth.json: written through
    :func:`write_restored_store`) or None (an ordinary file: restored as upstream does)."""
    if is_runtime_lock(path):
        return "skip"
    kind = _classify(path)
    if kind in ("protocol", "codex-cli"):
        return "skip"
    return "store" if kind == "store" else None


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


def _is_managed_store(dest: Path) -> bool:
    """*dest* (resolved) is one of the PROTECTED authority's own credential stores: its active store,
    its root ``auth.json``, or ``profiles/<id>/auth.json`` of a profile directory that is not an
    independent root. Decided by this structure, never by directory containment."""
    from hermes_cli.auth_codex_registry import active_store_path, root_home
    from hermes_constants import PROFILE_ID_RE
    if _norm(str(dest)) == _norm(str(canonical_path(active_store_path()))):
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


def check_restore(targets: Iterable[Tuple[Path, Callable[[], bytes]]], action: str) -> None:
    """Refuse (:class:`CodexAuthorityRefused`) a restore that would write an ``auth.json`` anywhere but
    one of the protected authority's own stores (another root, an independent root nested in this
    one, an unmanaged path): the merge below protects THIS authority only."""
    for target, _load in targets:
        if restore_action(Path(target)) != "store":
            continue
        dest = _destination(Path(target))
        if not _is_managed_store(dest):
            raise CodexAuthorityRefused(
                f"The backup would write Hermes credentials outside this installation ({dest}); "
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
    credential stores: no Codex sign-in, import or bootstrap can interleave with a store merge."""
    targets = list(targets_fn())
    if not any(restore_action(Path(t)) == "store" for t, _ in targets):
        yield
        return
    with migration_lock():
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
