"""SQLite online backup whose lock wait is bounded (A21).

``Connection.backup`` retries a busy / locked step forever, and each step can also wait inside
SQLite's own busy handler before Python sees it. So a writer holding the source or destination could
stall a restore, a snapshot, ``hermes doctor`` or a kanban export without limit. Here both
connections are opened with SQLite's implicit wait disabled (``timeout=0``) and one explicit clock,
started before the copy begins, owns the whole wait: the copy aborts with :class:`BackupLockTimeout`
once the source or destination has stayed busy for *lock_seconds* (progress on the copy renews it).
SQLite keeps an aborted copy inside the destination's write transaction, so the destination is left
as it was.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Union


class BackupLockTimeout(TimeoutError):
    """The source or destination stayed locked by another connection for the whole budget."""


def open_backup_connection(target: Union[str, Path], *, uri: bool = False) -> sqlite3.Connection:
    """A connection for :func:`bounded_backup`: SQLite's implicit busy wait disabled."""
    return sqlite3.connect(str(target), uri=uri, timeout=0.0)


def bounded_backup(src_conn: sqlite3.Connection, dst_conn: sqlite3.Connection, *, lock_seconds: float,
                   what: str = "the database", pages: int = 256, sleep: float = 0.1) -> None:
    """``src_conn.backup(dst_conn)`` in steps of *pages*, raising :class:`BackupLockTimeout` once a
    step has stayed busy / locked for *lock_seconds* (every retry sleep counted). Both connections
    must come from :func:`open_backup_connection` (no implicit wait inside a step)."""
    busy_deadline = time.monotonic() + max(0.0, lock_seconds)

    def progress(status: int, _remaining: int, _total: int) -> None:
        nonlocal busy_deadline
        now = time.monotonic()
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            if now >= busy_deadline:
                raise BackupLockTimeout(f"{what} stayed locked by another process for {lock_seconds:g} seconds")
        else:
            busy_deadline = now + max(0.0, lock_seconds)

    src_conn.backup(dst_conn, pages=pages, progress=progress, sleep=sleep)
