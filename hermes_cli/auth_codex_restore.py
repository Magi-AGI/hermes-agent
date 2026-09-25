"""Stage-1 refusal of restoring Codex credential state (credential recovery protocol, RULE-P1).

Every file of the protocol is credential state: ``auth.json`` (any store), the registry
``auth.chains.json`` (and its preserved copies), the retired-token index, chain records under
``auth.recovery/``, ownership payloads under ``auth.ops/``, the migration lock and legacy rescue
files. Restoring an older copy of any of them can roll the protocol back — an ACTIVE registry entry
for a chain that was retired, a refresh token that was already spent — so until stage 2 implements
restoration as an ownership operation that preserves fences, a restore (backup import, quick
snapshot, profile import) that would write such a file is REFUSED whenever this install already
has Codex authority state. Restoring onto an install that never had any (a fresh machine) is the
upstream behaviour and stays allowed.

Callers run :func:`check_restore` and the file writes inside :func:`restore_guard`, which holds the
migration lock: no sign-in, import or bootstrap can register anything between the check and the
write (the check is re-run under the lock).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Tuple

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
    if registry_path().exists() or retired_index_path().exists() or ops_dir().exists():
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


@contextmanager
def restore_guard(targets_fn: Callable[[], Iterable[Tuple[Path, Callable[[], bytes]]]],
                  action: str) -> Iterator[None]:
    """Hold the migration lock across the check and the restore's writes (only when the restore
    touches credential state; otherwise no lock is taken)."""
    targets = list(targets_fn())
    if not any(is_credential_state(t) for t, _ in targets):
        yield
        return
    with migration_lock():
        check_restore(targets, action)
        yield
