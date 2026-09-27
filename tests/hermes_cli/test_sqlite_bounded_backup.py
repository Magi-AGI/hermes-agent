"""Every SQLite online backup in the restore / snapshot / doctor / kanban paths has a bounded lock wait (A21).

``Connection.backup`` retries a busy step forever, and a connection's own busy handler can wait inside
each step before Python sees it: a restore onto a locked destination hung, and with a locked SOURCE
the 10 s restore budget took 17.4 s (Codex, round 15). ``hermes_cli.sqlite_bounded_backup`` opens
both sides with the implicit wait disabled and one clock, started before the first step, owns the
whole wait; the restore page copy, the snapshot copy, ``hermes doctor``'s write-health snapshot and
the kanban export all use it. Real separate processes hold the locks; each copy runs in a child with
a hang guard (the old code never returned). A source that keeps CHANGING restarts the copy with every
step "OK" (Codex, round 16): progress is the pages remaining, and the copy is capped overall; a real
writer process churns the source between steps, under a watchdog. Round 18: a copy whose DONE
callback arrives past the cap committed and is reported as success (also by the restore caller),
and a source that grows meanwhile cannot move the cap, fixed at the start. Fake data only.
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


# ── Round 17: a source that keeps changing (every step "OK", no progress) is bounded too ──

CHURN_WRITER = (
    "import sqlite3, sys\n"
    "c = sqlite3.connect(sys.argv[1], timeout=2, isolation_level=None)\n"
    "print('ready', flush=True)\n"
    "for line in sys.stdin:\n"
    "    if line.strip() == 'stop':\n"
    "        break\n"
    "    c.execute('UPDATE counter SET value = value + 1')\n"
    "    print('committed', flush=True)\n"
    "c.close()\n"
)
WATCHDOG_SECONDS = 30.0


class _Watchdog(RuntimeError):
    pass


def _churn_copy(tmp_path, monkeypatch, *, churn_every, lock_seconds, step_delay=0.0):
    """Adapted from Codex (round 16): a real separate writer commits one source update after every
    *churn_every*-th successful backup step (0 = quiet), synchronised through an observing connection
    that only watches progress; a watchdog stops the old, unbounded helper instead of hanging."""
    from contextlib import closing
    from hermes_cli import sqlite_bounded_backup as helper
    source, destination = tmp_path / "source.db", tmp_path / "destination.db"
    with closing(sqlite3.connect(source)) as seed:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("CREATE TABLE payload(value BLOB)")
        seed.executemany("INSERT INTO payload VALUES (zeroblob(4000))", [()] * 1200)
        seed.execute("CREATE TABLE counter(value INTEGER)")
        seed.execute("INSERT INTO counter VALUES (0)")
        seed.commit()
    with closing(sqlite3.connect(destination)) as seed:
        seed.execute("CREATE TABLE original(value TEXT)")
        seed.execute("INSERT INTO original VALUES ('keep on failure')")
        seed.commit()
    writer = subprocess.Popen([sys.executable, "-u", "-B", "-c", CHURN_WRITER, str(source)], cwd=str(REPO),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    real_connect, steps, started = sqlite3.connect, [], [time.monotonic()]

    class Observed(sqlite3.Connection):
        def backup(self, target, *, progress=None, **kwargs):
            def observed(status, remaining, total):
                steps.append((status, remaining))
                if status == sqlite3.SQLITE_OK:
                    time.sleep(step_delay)
                    if churn_every and len(steps) % churn_every == 0:
                        writer.stdin.write("write\n")
                        writer.stdin.flush()
                        assert writer.stdout.readline().strip() == "committed"
                progress(status, remaining, total)
                if time.monotonic() - started[0] >= WATCHDOG_SECONDS:
                    raise _Watchdog("the copy never ended")
            return super().backup(target, progress=observed, **kwargs)

    try:
        assert writer.stdout.readline().strip() == "ready"
        monkeypatch.setattr(helper.sqlite3, "connect", lambda *a, **k: real_connect(*a, **{**k, "factory": Observed}))
        with closing(helper.open_backup_connection(source)) as src, closing(helper.open_backup_connection(destination)) as dst:
            started[0] = time.monotonic()
            try:
                helper.bounded_backup(src, dst, lock_seconds=lock_seconds)
                outcome = "finished"
            except helper.BackupLockTimeout as exc:
                outcome = type(exc).__name__
            except _Watchdog:
                outcome = "watchdog"
            elapsed = time.monotonic() - started[0]
    finally:
        monkeypatch.setattr(helper.sqlite3, "connect", real_connect)
        writer.stdin.write("stop\n")
        writer.stdin.flush()
        writer.communicate(timeout=10)
    report = {"outcome": outcome, "elapsed": round(elapsed, 3), "steps": len(steps)}
    print(f"measured: {report}")
    with closing(real_connect(destination)) as check:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return report, tables


def test_the_quiet_control_still_completes(tmp_path, monkeypatch):
    report, tables = _churn_copy(tmp_path, monkeypatch, churn_every=0, lock_seconds=0.2)
    assert report["outcome"] == "finished" and "payload" in tables, report


def test_a_source_that_changes_after_every_step_is_refused_within_the_budget(tmp_path, monkeypatch):
    report, tables = _churn_copy(tmp_path, monkeypatch, churn_every=1, lock_seconds=0.2)
    assert report["outcome"] == "BackupNoProgress", report
    assert report["elapsed"] <= 0.2 + MARGIN_SECONDS, report
    assert tables == {"original"}, "an aborted copy changed the destination"


def test_a_source_that_changes_every_other_step_hits_the_overall_cap(tmp_path, monkeypatch):
    """Alternating churn shows 'progress' on every other step, so only the overall cap ends it."""
    from hermes_cli import sqlite_bounded_backup as helper
    monkeypatch.setattr(helper, "MIN_COPY_SECONDS", 2.0)
    monkeypatch.setattr(helper, "MIN_PAGES_PER_SECOND", 1e9)
    report, tables = _churn_copy(tmp_path, monkeypatch, churn_every=2, lock_seconds=0.2)
    assert report["outcome"] == "BackupNoProgress", report
    assert report["elapsed"] <= 2.0 + MARGIN_SECONDS, report
    assert tables == {"original"}


def test_a_slow_but_steady_copy_is_never_aborted(tmp_path, monkeypatch):
    """Each step takes longer than the whole lock budget, yet reduces the pages remaining: not a stall."""
    report, tables = _churn_copy(tmp_path, monkeypatch, churn_every=0, lock_seconds=0.2, step_delay=0.3)
    assert report["outcome"] == "finished" and report["elapsed"] > 0.2 * 3 and "payload" in tables, report


# ── Round 18: completion always wins; the overall cap is fixed at the start ──

def _seed(path, value):
    from contextlib import closing
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE data(value TEXT)")
        conn.execute("INSERT INTO data VALUES (?)", (value,))
        conn.commit()


def _observe(monkeypatch, helper, on_step):
    real_connect = sqlite3.connect

    class Observed(sqlite3.Connection):
        def backup(self, target, *, progress=None, **kwargs):
            def observed(status, remaining, total):
                on_step(status, remaining, total)
                progress(status, remaining, total)
            return super().backup(target, progress=observed, **kwargs)

    monkeypatch.setattr(helper.sqlite3, "connect", lambda *a, **k: real_connect(*a, **{**k, "factory": Observed}))
    return real_connect


@pytest.mark.parametrize("late_done", [False, True], ids=["quiet", "done-after-the-deadline"])
def test_a_copy_that_committed_is_never_reported_as_refused(tmp_path, monkeypatch, late_done):
    """Codex (round 17): the DONE callback arrives after SQLite committed; if it is scheduled past
    the cap, the copy still finished and must be reported so."""
    from contextlib import closing
    from hermes_cli import sqlite_bounded_backup as helper
    source, destination = tmp_path / "source.db", tmp_path / "destination.db"
    _seed(source, "snapshot")
    _seed(destination, "live")
    monkeypatch.setattr(helper, "MIN_COPY_SECONDS", 0.1)
    monkeypatch.setattr(helper, "MIN_PAGES_PER_SECOND", 1e9)
    real_connect = _observe(monkeypatch, helper, lambda status, *_: time.sleep(
        0.2 if late_done and status == sqlite3.SQLITE_DONE else 0))
    with closing(helper.open_backup_connection(source)) as src, closing(helper.open_backup_connection(destination)) as dst:
        helper.bounded_backup(src, dst, lock_seconds=0.01)  # must not raise
    with closing(real_connect(destination)) as conn:
        assert conn.execute("SELECT value FROM data").fetchall() == [("snapshot",)]


def test_the_restore_reports_success_when_its_copy_committed_at_the_deadline(tmp_path, monkeypatch):
    """Codex's real-caller case: _page_copy_db said False / 'left as it is' while the database held
    the restored snapshot."""
    from contextlib import closing
    from hermes_cli import backup, sqlite_bounded_backup as helper
    source, destination = tmp_path / "source.db", tmp_path / "destination.db"
    _seed(source, "restored snapshot")
    _seed(destination, "original live data")
    monkeypatch.setattr(helper, "MIN_COPY_SECONDS", 0.1)
    monkeypatch.setattr(helper, "MIN_PAGES_PER_SECOND", 1e9)
    monkeypatch.setattr(backup, "_RESTORE_LOCK_DEADLINE_SECONDS", 0.01)
    real_connect = _observe(monkeypatch, helper, lambda status, *_: time.sleep(
        0.2 if status == sqlite3.SQLITE_DONE else 0))
    assert backup._page_copy_db(source, destination) is True
    with closing(real_connect(destination)) as conn:
        assert conn.execute("SELECT value FROM data").fetchall() == [("restored snapshot",)]


GROW_WRITER = (
    "import sqlite3, sys\n"
    "c = sqlite3.connect(sys.argv[1], timeout=2, isolation_level=None)\n"
    "print('ready', flush=True)\n"
    "for line in sys.stdin:\n"
    "    if line.strip() == 'stop':\n"
    "        break\n"
    "    c.execute('BEGIN')\n"
    "    c.executemany('INSERT INTO payload VALUES (zeroblob(4000))', [()] * 100)\n"
    "    c.execute('COMMIT')\n"
    "    print('committed', flush=True)\n"
    "c.close()\n"
)


def test_a_growing_source_cannot_extend_the_overall_cap(tmp_path, monkeypatch):
    """Codex (round 17): alternating restarts plus a writer growing the source faster than the
    1000 pages/s allowance kept moving the cap (1.2 s -> 8.6 s). The cap is fixed at the start."""
    from contextlib import closing
    from hermes_cli import sqlite_bounded_backup as helper
    source, destination = tmp_path / "growing.db", tmp_path / "destination.db"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE payload(value BLOB)")
        conn.executemany("INSERT INTO payload VALUES (zeroblob(4000))", [()] * 1200)
        conn.commit()
    _seed(destination, "live")
    monkeypatch.setattr(helper, "MIN_COPY_SECONDS", 0.6)  # only the floor is shortened
    writer = subprocess.Popen([sys.executable, "-u", "-B", "-c", GROW_WRITER, str(source)], cwd=str(REPO),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    steps, started = [], [time.monotonic()]

    def on_step(status, remaining, total):
        steps.append((status, remaining, total))
        if status == sqlite3.SQLITE_OK:
            time.sleep(0.01)
            if len(steps) % 2 == 0:
                writer.stdin.write("grow\n")
                writer.stdin.flush()
                assert writer.stdout.readline().strip() == "committed"
        if time.monotonic() - started[0] >= WATCHDOG_SECONDS:
            raise _Watchdog("the copy never ended")

    try:
        assert writer.stdout.readline().strip() == "ready"
        real_connect = _observe(monkeypatch, helper, on_step)
        with closing(helper.open_backup_connection(source)) as src, closing(helper.open_backup_connection(destination)) as dst:
            started[0] = time.monotonic()
            try:
                helper.bounded_backup(src, dst, lock_seconds=0.2)
                outcome = "finished"
            except helper.BackupNoProgress:
                outcome = "BackupNoProgress"
            except _Watchdog:
                outcome = "watchdog"
            elapsed = time.monotonic() - started[0]
    finally:
        writer.stdin.write("stop\n")
        writer.stdin.flush()
        writer.communicate(timeout=10)
    initial_cap = max(0.6, 3 * 0.2, steps[0][2] / helper.MIN_PAGES_PER_SECOND)
    report = {"outcome": outcome, "elapsed": round(elapsed, 3), "steps": len(steps), "initial_cap": initial_cap,
              "pages": (steps[0][2], steps[-1][2])}
    print(f"measured: {report}")
    with closing(real_connect(destination)) as conn:
        assert conn.execute("SELECT value FROM data").fetchall() == [("live",)], report
    assert outcome == "BackupNoProgress" and elapsed <= initial_cap + MARGIN_SECONDS, report
