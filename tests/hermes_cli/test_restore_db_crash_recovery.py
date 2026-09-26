"""A database restore never discards a sidecar that still holds recovery data (stage 1, round 12).

The replacement path moved the old WAL aside before publishing the new image and kept that fact only
in memory (rollback errors were suppressed). Codex reproduced, with a real WAL: a restorer killed
right after the WAL moved, or a failed publication whose WAL rollback also failed, and normal Hermes
startup (``SessionDB``) opened the old database WITHOUT a committed row.

Now SQLite proves the old database self-contained first — it opens it (resolving a hot rollback
journal) and checkpoints the WAL with TRUNCATE; unless that fully succeeds, the restore is refused
with nothing touched — and only then is the image published in one rename and the now-empty
sidecars removed. Real SQLite; the crash cases die for real in child processes and a separate child
performs the normal ``SessionDB`` startup. Crash and failed-publication cases are adapted from
Codex's review reproductions. Fake data only.
"""

import errno
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.hermes_cli.codex_authority_env import codex_home  # noqa: F401
from tests.hermes_cli.test_codex_restore_aliases import _snapshot

REPO = Path(__file__).resolve().parents[2]

SEED_WAL = r'''
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("CREATE TABLE review_rows(v TEXT)")
c.commit()
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.execute("INSERT INTO review_rows VALUES ('committed-live')")
c.commit()
os._exit(71)  # no close-time checkpoint: the committed row lives only in the WAL
'''

# A writer that dies mid-transaction after spilling pages into the database file: a HOT journal.
SEED_HOT_JOURNAL = r'''
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("PRAGMA journal_mode=DELETE")
c.execute("CREATE TABLE review_rows(v TEXT)")
c.execute("INSERT INTO review_rows VALUES ('committed-live')")
c.execute("PRAGMA cache_size=2")
c.execute("BEGIN")
for i in range(3000):
    c.execute("INSERT INTO review_rows VALUES (?)", ("uncommitted-" + "x" * 200,))
os._exit(72)
'''

CRASH_RESTORE = r'''
import os, sys
from pathlib import Path
from hermes_cli import backup
root, snapshot_id, boundary = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
live = root / "sessions.db"
real_replace = os.replace
def replace(src, dst, *args, **kwargs):
    if Path(dst) == live and boundary == "before-publication":
        os._exit(81)
    result = real_replace(src, dst, *args, **kwargs)
    if Path(dst) == live and boundary == "after-publication":
        os._exit(82)
    return result
os.replace = replace
backup.restore_quick_snapshot(snapshot_id, hermes_home=root)
raise SystemExit(99)
'''

READ_WITH_HERMES = r'''
import json, sys
from pathlib import Path
from hermes_state import SessionDB
db = SessionDB(db_path=Path(sys.argv[1]))
rows = [r[0] for r in db._conn.execute("SELECT v FROM review_rows")]
ok = db._conn.execute("PRAGMA integrity_check").fetchone()[0]
db.close()
print(json.dumps({"rows": rows, "integrity": ok}))
'''

# Holds the database open until a gate file appears: a reader inside a snapshot, or a writer
# holding the write lock with an uncommitted row.
HOLDER = r'''
import sqlite3, sys, time
from pathlib import Path
path, mode, ready, gate = sys.argv[1:5]
c = sqlite3.connect(path, isolation_level=None)
if mode == "reader":
    c.execute("BEGIN")
    c.execute("SELECT count(*) FROM review_rows").fetchone()
else:
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO review_rows VALUES ('uncommitted-writer')")
Path(ready).write_text("ready")
while not Path(gate).exists():
    time.sleep(0.05)
c.execute("ROLLBACK")
c.close()
'''


def _run(code, *args):
    return subprocess.run([sys.executable, "-B", "-c", code, *map(str, args)], cwd=str(REPO),
                          capture_output=True, text=True, timeout=120)


def _read(live):
    out = _run(READ_WITH_HERMES, live)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _backup_image(tmp_path):
    source = tmp_path / "source.db"
    with closing_db(source) as c:
        c.execute("CREATE TABLE review_rows(v TEXT)")
        c.execute("INSERT INTO review_rows VALUES ('backup-image')")
        c.commit()
    return source


class closing_db:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()


def _prepare(root, tmp_path, seed=SEED_WAL, code=71):
    """A live multi-link database (the replacement branch) whose committed row is only in its
    sidecar state, and a snapshot holding a different image."""
    live = root / "sessions.db"
    seeded = _run(seed, live)
    assert seeded.returncode == code, seeded.stderr[-2000:]
    os.link(live, tmp_path / "other-name.db")
    return live, _snapshot(root, {"sessions.db": _backup_image(tmp_path).read_bytes()})


@pytest.mark.parametrize("boundary", ["before-publication", "after-publication"])
def test_a_restorer_killed_mid_publication_never_loses_a_committed_row(codex_home, tmp_path, boundary):
    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path)
    wal = live.with_name(live.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 32, "the committed row is not in the WAL"

    killed = _run(CRASH_RESTORE, root, snapshot_id, boundary)
    assert killed.returncode == (81 if boundary == "before-publication" else 82), killed.stderr[-2000:]

    state = _read(live)  # a SEPARATE process: normal Hermes startup
    expected = ["committed-live"] if boundary == "before-publication" else ["backup-image"]
    assert state == {"rows": expected, "integrity": "ok"}, f"startup after a kill {boundary}: {state}"


def test_a_failed_publication_keeps_every_committed_row(codex_home, tmp_path, monkeypatch):
    from hermes_cli import backup

    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path)
    real_replace, failures = os.replace, []

    def replace(src, dst, *args, **kwargs):
        if Path(dst) == live or str(dst).endswith(("-wal", "-journal")):
            failures.append(Path(dst))
            raise PermissionError(errno.EACCES, "injected publication / rollback failure", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is False
    monkeypatch.undo()
    assert live in failures, "the failure injection did not reach the publication"
    assert _read(live) == {"rows": ["committed-live"], "integrity": "ok"}


@pytest.mark.parametrize("holder", ["reader", "writer"])
def test_a_checkpoint_that_cannot_complete_refuses_the_restore_untouched(codex_home, tmp_path, holder):
    from hermes_cli import backup

    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path)
    wal = live.with_name(live.name + "-wal")
    ready, gate = tmp_path / "ready", tmp_path / "gate"
    proc = subprocess.Popen([sys.executable, "-B", "-c", HOLDER, str(live), holder, str(ready), str(gate)],
                            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert proc.poll() is None and time.monotonic() < deadline, proc.communicate()
            time.sleep(0.05)
        wal_before = wal.read_bytes()

        assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is False

        # SQLite's own (partial) checkpoint may copy committed frames into the main file; the WAL
        # itself, and the database file's identity, are untouched.
        assert wal.read_bytes() == wal_before, "a refused restore touched the WAL"
        assert os.path.samefile(live, tmp_path / "other-name.db"), "a refused restore replaced the database"
    finally:
        gate.write_text("go")
        proc.communicate(timeout=60)
    assert _read(live) == {"rows": ["committed-live"], "integrity": "ok"}


@pytest.mark.parametrize("publication", ["succeeds", "fails"])
def test_a_hot_rollback_journal_is_resolved_by_sqlite_before_anything_is_replaced(
        codex_home, tmp_path, monkeypatch, publication):
    from hermes_cli import backup

    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path, seed=SEED_HOT_JOURNAL, code=72)
    journal = live.with_name(live.name + "-journal")
    assert journal.exists() and journal.stat().st_size > 0, "no hot journal was left"
    if publication == "fails":
        real_replace = os.replace

        def replace(src, dst, *args, **kwargs):
            if Path(dst) == live:
                raise PermissionError(errno.EACCES, "injected publication failure", str(dst))
            return real_replace(src, dst, *args, **kwargs)
        monkeypatch.setattr(os, "replace", replace)

    restored = backup.restore_quick_snapshot(snapshot_id, hermes_home=root)
    monkeypatch.undo()

    assert restored is (publication == "succeeds")
    assert not (journal.exists() and journal.stat().st_size > 0), "a journal was left beside the database"
    expected = ["backup-image"] if publication == "succeeds" else ["committed-live"]
    assert _read(live) == {"rows": expected, "integrity": "ok"}


def test_an_unreadable_database_is_replaced_and_its_sidecars_are_preserved(codex_home, tmp_path):
    from hermes_cli import backup

    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path)  # a committed row in the WAL ...
    wal = live.with_name(live.name + "-wal")
    old_wal = wal.read_bytes()
    with open(live, "r+b") as handle:  # ... beside a main file whose header is destroyed
        handle.write(b"\x00" * 100)

    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is True

    assert not wal.exists(), "the old WAL was left beside the new image"
    kept = list(root.glob(".sessions.db-wal.*.preserved"))
    assert [p.read_bytes() for p in kept] == [old_wal], "the unreadable database's WAL was not preserved"
    assert _read(live) == {"rows": ["backup-image"], "integrity": "ok"}
