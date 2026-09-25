"""Rotating state.db backups (``sessions.backup``; gate item G9).

The pre-update snapshot skips a state.db over 1 GiB, so a large install had no copy of its session
history at all. Backups are VACUUM INTO images: consistent while other connections write, rotated
to ``keep``, throttled by the newest image's age, one per store at a time.
"""

import sqlite3
import threading
import time
from pathlib import Path

import yaml

from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from hermes_state_backup import rotate_state_backup


def _seed(db_path: Path, sessions: int = 3) -> SessionDB:
    db = SessionDB(db_path=db_path)
    for i in range(sessions):
        db.create_session(f"s{i}", "cli")
        db.append_message(session_id=f"s{i}", role="user", content="hello " * 50)
    return db


def _count(image: Path, table: str) -> int:
    conn = sqlite3.connect(str(image))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_backups_are_consistent_under_writes_and_rotate_to_keep(tmp_path):
    db = _seed(tmp_path / "state.db")
    dest = tmp_path / "state-backups"
    stop = threading.Event()

    def keep_writing():
        n = 0
        while not stop.is_set():
            db.append_message(session_id="s0", role="assistant", content=f"reply {n}")
            n += 1

    writer = threading.Thread(target=keep_writing, daemon=True)
    writer.start()
    try:
        base = time.time()
        results = [rotate_state_backup(tmp_path / "state.db", dest, interval_hours=1, keep=2, now=base + h * 3600)
                   for h in range(3)]
    finally:
        stop.set()
        writer.join(timeout=10)
        db.close()
    assert [r["skipped"] for r in results] == [False, False, False]
    images = sorted(dest.glob("state-*.db"))
    assert len(images) == 2 and [str(p) for p in images] == [r["path"] for r in results[1:]]
    for image in images:
        conn = sqlite3.connect(str(image))
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
        assert _count(image, "sessions") == 3


def test_a_backup_is_not_repeated_before_the_interval_nor_run_twice_at_once(tmp_path):
    _seed(tmp_path / "state.db").close()
    dest = tmp_path / "state-backups"
    now = time.time()
    assert rotate_state_backup(tmp_path / "state.db", dest, interval_hours=24, keep=3, now=now)["skipped"] is False
    later = rotate_state_backup(tmp_path / "state.db", dest, interval_hours=24, keep=3, now=now + 3600)
    assert later["skipped"] and later["reason"] == "not due"

    from hermes_state_repair import _open_lock_file, _release_lock_handle, _try_lock_nonblocking
    _path, held = _open_lock_file(tmp_path / "state.db", ".backup.lock", "backup", "test")
    _try_lock_nonblocking(held)
    try:
        busy = rotate_state_backup(tmp_path / "state.db", dest, interval_hours=24, keep=3, now=now + 90000)
    finally:
        _release_lock_handle(held)
    assert busy["skipped"] and busy["reason"] == "another process is backing up"
    assert len(list(dest.glob("state-*.db"))) == 1


def test_enabling_sessions_backup_makes_the_housekeeping_tick_write_one(tmp_path):
    """The config key reaches the behaviour through the loader the gateway uses."""
    home = get_hermes_home()
    (home / "config.yaml").write_text(yaml.safe_dump({"sessions": {
        "auto_prune": False, "backup": {"enabled": True, "interval_hours": 24, "keep": 2}}}), encoding="utf-8")
    _seed(home / "state.db").close()

    from gateway.run import _housekeeping_state_db_maintenance
    _housekeeping_state_db_maintenance()
    deadline = time.monotonic() + 60
    backups = home / "state-backups"
    while time.monotonic() < deadline and not list(backups.glob("state-*.db")):
        time.sleep(0.1)
    images = list(backups.glob("state-*.db"))
    assert len(images) == 1 and _count(images[0], "sessions") == 3
