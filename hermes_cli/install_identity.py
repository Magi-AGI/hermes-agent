"""Stable opaque identity shared by every profile in one Hermes install."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import threading
from typing import Optional
import uuid

from hermes_constants import get_default_hermes_root
from utils import atomic_write_text, lock_file_polled, pad_lock_sentinel, unlock_file

_INSTALL_ID_FILENAME = "install_id"
_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_INSTALL_ID_CACHE: dict[str, Optional[str]] = {"root": None, "value": None}
_INSTALL_ID_LOCK, _INSTALL_ID_PUBLICATION_LOCK = threading.Lock(), threading.Lock()


@contextlib.contextmanager
def _install_id_file_lock(root: Path):
    """Serialize identity publication across processes on POSIX and Windows; a sibling holding the
    lock is waited out up to ``utils.BOUNDED_LOCK_WAIT_S`` (msvcrt LK_LOCK gave up after ~10 s)."""
    fd = os.open(root / ".install_id.lock", os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        pad_lock_sentinel(fd)
        lock_file_polled(fd)
        try:
            yield
        finally:
            unlock_file(fd)
    finally:
        os.close(fd)


def _read_existing(path: Path) -> tuple[Optional[str], bool]:
    """``(valid id or None, mint?)`` — mint on a missing or malformed file, never on a read failure."""
    try:
        existing = path.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        return None, True
    except (OSError, UnicodeDecodeError):
        return None, False
    return (existing, False) if _INSTALL_ID_RE.fullmatch(existing) else (None, True)


def read_or_create_install_id(root: Path | None = None) -> Optional[str]:
    """Read or atomically mint the opaque id for the physical install.

    ``None`` = neither readable nor persistable; an ephemeral id would violate the authority/registry contract.
    """
    root = get_default_hermes_root() if root is None else root
    path = root / _INSTALL_ID_FILENAME
    existing, mint = _read_existing(path)
    if not mint:
        return existing
    try:
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(root)
        # Windows byte-range locks can report a same-process conflict instead of waiting for another
        # thread: serialize threads here, then keep the file lock as the cross-process publication fence.
        with _INSTALL_ID_PUBLICATION_LOCK, _install_id_file_lock(root):
            existing, mint = _read_existing(path)
            if not mint:
                return existing
            atomic_write_text(path, uuid.uuid4().hex + "\n", tmp_prefix=".install_id-", fsync_dir=True, mode=0o600)
            committed = path.read_text(encoding="utf-8").strip().lower()
            return committed if _INSTALL_ID_RE.fullmatch(committed) else None
    except OSError:
        return None


def get_install_id(*, cache: dict[str, Optional[str]] | None = None) -> Optional[str]:
    """Return the process-cached stable id for the active Hermes root."""
    root = get_default_hermes_root()
    root_key = str(root)
    target_cache = _INSTALL_ID_CACHE if cache is None else cache

    def _cached() -> Optional[str]:
        cached = target_cache.get("value")
        return cached if cached and target_cache.get("root") in (None, root_key) else None

    if value := _cached():
        return value
    with _INSTALL_ID_LOCK:
        if value := _cached():
            return value
        value = read_or_create_install_id(root)
        if value:
            target_cache["root"] = root_key
            target_cache["value"] = value
        return value


__all__ = ["get_install_id", "read_or_create_install_id"]
