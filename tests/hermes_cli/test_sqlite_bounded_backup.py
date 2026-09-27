"""Every SQLite online backup in the restore / snapshot / doctor / kanban paths has a bounded lock wait (A21).

``Connection.backup`` retries a busy step forever, and a connection's own busy handler can wait inside
each step before Python sees it: a restore onto a locked destination hung, and with a locked SOURCE
the 10 s restore budget took 17.4 s (Codex, round 15). ``hermes_cli.sqlite_bounded_backup`` opens
both sides with the implicit wait disabled and one clock, started before the first step, owns the
whole wait; the restore page copy, the snapshot copy, ``hermes doctor``'s write-health snapshot and
the kanban export all use it. Real separate processes hold the locks; each copy runs in a child with
a hang guard (the old code never returned). Fake data only.
"""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HANG_GUARD_SECONDS = 60
MARGIN_SECONDS = 1.5

HOLDER = (
    "import sqlite3, sys, time\n"
    "from pathlib import Path\n"
    "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
    "c.execute('BEGIN EXCLUSIVE')\n"
    "Path(sys.argv[2]).write_text('ready')\n"
    "while not Path(sys.argv[3]).exists():\n"
    "    time.sleep(0.02)\n"
    "c.execute('ROLLBACK')\n"
    "c.close()\n"
)

# Adapted from Codex's review test: only the real backup call is timed.
RESTORE_COPY = (
    "import json, sys, time\n"
    "from pathlib import Path\n"
    "from hermes_cli import backup\n"
    "backup._RESTORE_LOCK_DEADLINE_SECONDS = float(sys.argv[3])\n"
    "original = backup._bounded_backup\n"
    "timings = []\n"
    "def measured(*args):\n"
    "    before = time.monotonic()\n"
    "    try:\n"
    "        return original(*args)\n"
    "    finally:\n"
    "        timings.append(time.monotonic() - before)\n"
    "backup._bounded_backup = measured\n"
    "result = backup._safe_restore_db(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    "print(json.dumps({'result': result, 'copy_elapsed': timings}))\n"
)

DOCTOR_PROBE = (
    "import json, sys, time\n"
    "from pathlib import Path\n"
    "import hermes_state_repair\n"
    "hermes_state_repair._live_writer_holds_db = lambda path: True  # the holder scan is not under test\n"
    "from hermes_cli import doctor_state\n"
    "started = time.monotonic()\n"
    "reason = doctor_state._write_health_reason(Path(sys.argv[1]), should_fix=True)\n"
    "print(json.dumps({'reason': reason, 'elapsed': time.monotonic() - started}))\n"
)

KANBAN_SNAPSHOT = (
    "import json, sys, time\n"
    "from pathlib import Path\n"
    "from hermes_cli import kanban_transfer\n"
    "started = time.monotonic()\n"
    "try:\n"
    "    kanban_transfer._snapshot_db(Path(sys.argv[1]), Path(sys.argv[2]))\n"
    "    outcome = 'copied'\n"
    "except OSError as exc:\n"
    "    outcome = type(exc).__name__ + ': ' + str(exc)\n"
    "print(json.dumps({'outcome': outcome, 'elapsed': time.monotonic() - started}))\n"
)


def _database(path, label):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE data(value TEXT)")
    conn.execute("INSERT INTO data VALUES (?)", (label,))
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM data").fetchall()
    finally:
        conn.close()


def _held(tmp_path, locked, run):
    """Run *run()* (a child with a hang guard) while a separate process holds *locked* EXCLUSIVE."""
    ready, release = tmp_path / "ready", tmp_path / "release"
    holder = subprocess.Popen([sys.executable, "-B", "-c", HOLDER, str(locked), str(ready), str(release)],
                              cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert holder.poll() is None and time.monotonic() < deadline, holder.communicate()
            time.sleep(0.02)
        try:
            out = run()
        except subprocess.TimeoutExpired:
            pytest.fail("the backup waited without bound for the locked database")
        assert out.returncode == 0, out.stderr[-2000:]
        report = json.loads(out.stdout.strip().splitlines()[-1])
        print(f"measured: {report}")  # the elapsed times are the evidence (-rP shows them)
        return report
    finally:
        release.write_text("go")
        holder.communicate(timeout=30)


def _child(code, *args):
    return subprocess.run([sys.executable, "-B", "-c", code, *map(str, args)], cwd=str(REPO),
                          capture_output=True, text=True, timeout=HANG_GUARD_SECONDS)


@pytest.mark.parametrize("budget", [0.2, 10.0], ids=["short", "production"])
@pytest.mark.parametrize("locked_side", ["source", "destination"])
def test_the_restore_page_copy_stays_within_its_lock_budget(tmp_path, monkeypatch, budget, locked_side):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    source, destination = root / "source.db", root / "destination.db"
    _database(source, "snapshot")
    _database(destination, "live")

    report = _held(tmp_path, source if locked_side == "source" else destination,
                   lambda: _child(RESTORE_COPY, source, destination, budget))

    assert report["result"] is False, report
    assert len(report["copy_elapsed"]) == 1, report
    assert report["copy_elapsed"][0] <= budget + MARGIN_SECONDS, f"{locked_side}-locked copy overran: {report}"
    assert _rows(destination) == [("live",)], "a refused restore changed the database"


def test_the_doctor_write_health_snapshot_gives_up_on_a_locked_store(tmp_path, monkeypatch):
    from hermes_cli import doctor_state
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    state = root / "state.db"
    _database(state, "live")

    report = _held(tmp_path, state, lambda: _child(DOCTOR_PROBE, state))

    assert report["reason"] is None, report  # skipped (reported), never a false corruption verdict
    assert report["elapsed"] <= doctor_state._WRITE_PROBE_LOCK_SECONDS + MARGIN_SECONDS + 3, report
    assert _rows(state) == [("live",)]


def test_a_kanban_export_snapshot_gives_up_on_a_locked_board(tmp_path, monkeypatch):
    from hermes_cli import kanban_transfer
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    board = root / "kanban.db"
    _database(board, "live")

    report = _held(tmp_path, board, lambda: _child(KANBAN_SNAPSHOT, board, tmp_path / "staged.db"))

    assert report["outcome"].startswith("BackupLockTimeout"), report
    assert report["elapsed"] <= kanban_transfer._SNAPSHOT_LOCK_SECONDS + MARGIN_SECONDS + 3, report
    assert _rows(board) == [("live",)]
