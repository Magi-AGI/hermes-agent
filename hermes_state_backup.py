"""Rotating state.db backups (``sessions.backup`` in config.yaml, off by default).

Each backup is a ``VACUUM INTO`` image: one read transaction, so it is a consistent snapshot even
while every session window keeps writing (the paged backup API restarts whenever another connection
writes, and never finishes on a busy store), and it never modifies the live file. The newest
backup's timestamp is the throttle, so the job keeps no state of its own and never writes to the
store. The pre-update snapshot skips a state.db over 1 GiB, so on a large install this is the only
copy of the session history.

Restore: stop Hermes, move the live ``state.db`` and any ``state.db-wal`` / ``state.db-shm`` aside,
and copy the chosen ``state-<UTC time>.db`` to ``state.db``.
"""

from __future__ import annotations

import calendar
import logging
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes_state")

BACKUP_DIR_NAME = "state-backups"
_NAME_PREFIX = "state-"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
# A backup needs room for the image plus this margin of free space left over.
_FREE_SPACE_MARGIN = 1.25

_running: set = set()
_running_lock = threading.Lock()


def _backups(dest_dir: Path) -> list:
    """Finished backups, oldest first (the UTC stamp in the name sorts chronologically)."""
    return sorted(p for p in dest_dir.glob(f"{_NAME_PREFIX}*.db") if p.is_file())


def _stamp_time(path: Path) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(path.stem[len(_NAME_PREFIX):], _STAMP_FORMAT)))
    except ValueError:
        return None


def rotate_state_backup(db_path: Path, dest_dir: Path, *, interval_hours: float, keep: int,
                        now: Optional[float] = None) -> Dict[str, Any]:
    """Write a new backup when the newest is older than *interval_hours*, then keep the newest *keep*.

    Returns ``{"skipped": bool, "reason": str, "path": str | None, "removed": int}``. Never raises.
    """
    result: Dict[str, Any] = {"skipped": True, "reason": "", "path": None, "removed": 0}
    now = time.time() if now is None else now
    if keep < 1 or interval_hours <= 0:
        result["reason"] = "disabled (keep < 1 or interval_hours <= 0)"
        return result
    if not db_path.is_file():
        result["reason"] = "no state.db"
        return result
    from hermes_state_repair import _open_lock_file, _release_lock_handle, _try_lock_nonblocking
    # Gateway and Desktop backends share the store and both tick: one backup per store at a time.
    _lock_path, lock = _open_lock_file(db_path, ".backup.lock", "backup", "skipping the backup.")
    if lock is None:
        result["reason"] = "lock unavailable"
        return result
    try:
        _try_lock_nonblocking(lock)
    except OSError:
        lock.close()
        result["reason"] = "another process is backing up"
        return result
    try:
        return _rotate_locked(db_path, dest_dir, interval_hours, keep, now, result)
    finally:
        _release_lock_handle(lock)


def _rotate_locked(db_path: Path, dest_dir: Path, interval_hours: float, keep: int, now: float,
                   result: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from hermes_constants import mkdir_under_hermes_home
        mkdir_under_hermes_home(dest_dir)
        existing = _backups(dest_dir)
        newest = _stamp_time(existing[-1]) if existing else None
        if newest is not None and now - newest < interval_hours * 3600:
            result["reason"] = "not due"
            return result
        needed = db_path.stat().st_size * _FREE_SPACE_MARGIN
        free = shutil.disk_usage(dest_dir).free
        if free < needed:
            result["reason"] = f"only {free} bytes free, need about {int(needed)}"
            logger.warning("state.db backup skipped: %s in %s", result["reason"], dest_dir)
            return result

        final = dest_dir / f"{_NAME_PREFIX}{time.strftime(_STAMP_FORMAT, time.gmtime(now))}.db"
        partial = final.with_name(final.name + ".partial")
        for stale in dest_dir.glob(f"{_NAME_PREFIX}*.db.partial"):
            stale.unlink(missing_ok=True)  # a crash mid-backup; never a usable image
        started = time.monotonic()
        # VACUUM INTO only reads the source (a plain connection: read-only WAL access needs sidecars
        # that a cleanly closed store does not have).
        source = sqlite3.connect(str(db_path), timeout=60)
        try:
            source.execute("VACUUM INTO ?", (str(partial),))
        finally:
            source.close()
        image = sqlite3.connect(str(partial))
        try:
            verdict = image.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            image.close()
        if verdict != "ok":
            partial.unlink(missing_ok=True)
            result["reason"] = f"image failed quick_check: {verdict}"
            logger.warning("state.db backup discarded: %s", result["reason"])
            return result
        os.replace(partial, final)
        result.update(skipped=False, reason="", path=str(final))
        for old in _backups(dest_dir)[:-keep]:
            old.unlink(missing_ok=True)
            result["removed"] += 1
        logger.info("state.db backup written: %s (%d bytes, %.1fs; %d older removed)", final,
                    final.stat().st_size, time.monotonic() - started, result["removed"])
        return result
    except Exception as exc:  # noqa: BLE001 -- housekeeping: a failed backup must never take its caller down
        result["reason"] = f"failed: {exc}"
        logger.warning("state.db backup failed: %s", exc)
        return result


def start_state_backup_if_enabled(config: Optional[dict] = None, home: Optional[Path] = None) -> bool:
    """Read ``sessions.backup`` for the calling profile and, when enabled, run one rotation on a
    daemon thread (a multi-GB image takes minutes; housekeeping ticks must not wait). One run per
    store at a time. True when a run was started."""
    if config is None:
        from hermes_cli.config import load_config
        config = load_config()
    settings = ((config or {}).get("sessions") or {}).get("backup") or {}
    if not settings.get("enabled", False):
        return False
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    db_path = Path(home) / "state.db"
    custom_dir = str(settings.get("dir") or "").strip()
    dest_dir = Path(os.path.expanduser(custom_dir)) if custom_dir else Path(home) / BACKUP_DIR_NAME
    key = str(db_path.resolve())
    with _running_lock:
        if key in _running:
            return False
        _running.add(key)

    def _run() -> None:
        try:
            rotate_state_backup(db_path, dest_dir, interval_hours=float(settings.get("interval_hours", 24)),
                                keep=int(settings.get("keep", 3)))
        finally:
            with _running_lock:
                _running.discard(key)

    threading.Thread(target=_run, name="state-db-backup", daemon=True).start()
    return True
