"""SQLite online backup whose wait and run time are bounded (A21).

``Connection.backup`` retries a busy / locked step forever, and each step can also wait inside
SQLite's own busy handler before Python sees it. So a writer holding the source or destination could
stall a restore, a snapshot, ``hermes doctor`` or a kanban export without limit. Here both
connections are opened with SQLite's implicit wait disabled (``timeout=0``) and one explicit clock,
started before the copy begins, owns every wait.

A step that succeeds is not necessarily progress: when another connection changes the source
between steps, SQLite restarts the copy from the beginning, so a source that keeps changing can keep
a copy alive forever with every step "OK" (Codex, round 16: 336 steps, 950 pages remaining each
time). Progress is therefore measured on the pages REMAINING, checked on every callback:

* no progress — neither a busy step nor a successful one reduced the remaining page count — for
  *lock_seconds* aborts (:class:`BackupLockTimeout` while locked, :class:`BackupNoProgress` while the
  source keeps restarting the copy);
* the whole copy is capped at ``max(3 * lock_seconds, MIN_COPY_SECONDS, initial_pages /
  MIN_PAGES_PER_SECOND)`` (:class:`BackupNoProgress`), computed ONCE from the first known page
  count and never extended — finite even for a source that changes every other step and grows
  meanwhile (round 18). A finite cap may refuse a legitimately slow copy; that is a refusal like any
  other.

Completion always wins: a callback reporting ``SQLITE_DONE`` comes after SQLite has COMMITTED the
destination, so nothing is checked or raised there. Every refusal therefore happens while the copy is
unfinished, and SQLite keeps an unfinished copy inside the destination's write transaction: a
refused copy leaves the destination exactly as it was, and callers may say so.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional, Union

# The overall cap never falls below this, and grows with the database: a copy slower than
# MIN_PAGES_PER_SECOND (4 MB/s at 4 KiB pages) is treated as not finishing.
MIN_COPY_SECONDS = 60.0
MIN_PAGES_PER_SECOND = 1000.0


class BackupLockTimeout(TimeoutError):
    """The source or destination stayed locked by another connection for the whole budget."""


class BackupNoProgress(BackupLockTimeout):
    """The copy stopped making progress: the source kept changing, restarting it."""


def open_backup_connection(target: Union[str, Path], *, uri: bool = False) -> sqlite3.Connection:
    """A connection for :func:`bounded_backup`: SQLite's implicit busy wait disabled."""
    return sqlite3.connect(str(target), uri=uri, timeout=0.0)


def bounded_backup(src_conn: sqlite3.Connection, dst_conn: sqlite3.Connection, *, lock_seconds: float,
                   what: str = "the database", pages: int = 256, sleep: float = 0.1) -> None:
    """``src_conn.backup(dst_conn)`` in steps of *pages*, bounded as the module docstring says. Both
    connections must come from :func:`open_backup_connection` (no implicit wait inside a step)."""
    budget = max(0.0, lock_seconds)
    started = time.monotonic()
    last_progress = started
    previous_remaining: Optional[int] = None
    cap_at: Optional[float] = None

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal last_progress, previous_remaining, cap_at
        if status == sqlite3.SQLITE_DONE:
            return  # committed: completion wins, nothing may be reported as refused now
        now = time.monotonic()
        if cap_at is None and total > 0:  # once, from the first known page count; growth never extends it
            cap_at = started + max(3 * budget, MIN_COPY_SECONDS, total / MIN_PAGES_PER_SECOND)
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            if now - last_progress >= budget:
                raise BackupLockTimeout(f"{what} stayed locked by another process for {lock_seconds:g} seconds")
        else:
            if previous_remaining is None or remaining < previous_remaining:
                last_progress = now
            elif now - last_progress >= budget:
                raise BackupNoProgress(f"{what} kept changing during the copy (no progress for {lock_seconds:g} "
                                       "seconds); try again when it is idle")
            previous_remaining = remaining
        if cap_at is not None and now >= cap_at:
            raise BackupNoProgress(f"copying {what} did not finish within its {cap_at - started:.0f}-second budget "
                                   "(too slow, or it kept changing during the copy); nothing was copied — try "
                                   "again when it is idle")

    src_conn.backup(dst_conn, pages=pages, progress=progress, sleep=sleep)
