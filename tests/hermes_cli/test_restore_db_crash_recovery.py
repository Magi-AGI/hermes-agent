"""A database restore never writes into an alias: a pre-open guard, then upstream's own path (stage 1).

Rounds 10-12 built a custom database replacement (staging, sidecar set-aside, checkpoint-first
publication) and each review found new edge cases in it: SQLite's checkpoint wrote through a
sidecar hard-linked to a chain record (truncating it), probing a corrupt database deleted its WAL,
cleanup deleted a newer generation's WAL (Codex, rounds 11-12). That machinery is gone. A database
member (sessions.db, state.db, any ``.db``; also the post-update state.db auto-restore) is now
checked by ``backup._db_restore_refusal`` BEFORE anything opens it — ``lstat`` / ``stat`` and the
restore classification only: the database, each of its ``-wal`` / ``-shm`` / ``-journal`` sidecars
and upstream's staging name must not be a link, a hard link, credential state, outside the
database's directory or uninspectable (and the staging name must not exist) — and otherwise
upstream's database restore runs unchanged (SQLite backup-API page copy, its unlink+move fallback).
A refused database is not restored; the rest of the restore proceeds.

Codex's round-12 reproductions are adapted here (its fixture gives sessions.db a second hard link,
so every one of them is now refused by the guard); ``_prepare`` / ``_read`` / ``_run`` /
``_backup_image`` keep the names Codex's external file imports. Real SQLite and real child
processes; fake data only.
"""

import errno
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hermes_cli.codex_authority_env import codex_home  # noqa: F401
from tests.hermes_cli.test_codex_restore_aliases import _snapshot, live_root  # noqa: F401

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

POST_PUBLISH_WRITER = r'''
import os, sys
from pathlib import Path
from hermes_state import SessionDB
db = SessionDB(db_path=Path(sys.argv[1]))
c = db._conn
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("INSERT INTO review_rows VALUES ('arrived-after-publication')")
c.commit()
os._exit(73)
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
    source.unlink(missing_ok=True)
    conn = sqlite3.connect(str(source))
    conn.execute("CREATE TABLE review_rows(v TEXT)")
    conn.execute("INSERT INTO review_rows VALUES ('backup-image')")
    conn.commit()
    conn.close()
    return source


def _prepare(root, tmp_path, link=True):
    """A live database whose committed row is only in its WAL (with a second hard link, as in
    Codex's fixture, unless *link* is False), and a snapshot holding a different image."""
    live = root / "sessions.db"
    seeded = _run(SEED_WAL, live)
    assert seeded.returncode == 71, seeded.stderr[-2000:]
    if link:
        os.link(live, tmp_path / "other-name.db")
    return live, _snapshot(root, {"sessions.db": _backup_image(tmp_path).read_bytes()})


def _restore(tmp_path, root, how, image):
    from hermes_cli import backup
    if how == "snapshot":
        return backup.restore_quick_snapshot(_snapshot(root, {"sessions.db": image}), hermes_home=root)
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("config.yaml", "model: {}\n")
        zf.writestr("sessions.db", image)
    backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    return None


# ── Codex's round-12 reproductions: every one is refused by the guard ─────────────────────────

@pytest.mark.parametrize("publication", ["succeeds", "fails"])
def test_a_restore_never_writes_into_another_name_of_the_database(codex_home, tmp_path, monkeypatch, publication):
    from hermes_cli import backup
    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path)
    other = tmp_path / "other-name.db"
    before, wal_before = other.read_bytes(), live.with_name(live.name + "-wal").read_bytes()
    if publication == "fails":
        real_replace = os.replace

        def replace(src, dst, *args, **kwargs):
            if Path(dst) == live:
                raise PermissionError(errno.EACCES, "injected publication failure", str(dst))
            return real_replace(src, dst, *args, **kwargs)
        monkeypatch.setattr(os, "replace", replace)

    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is False  # refused

    assert other.read_bytes() == before, "the restore wrote into a different hard-link name"
    assert live.with_name(live.name + "-wal").read_bytes() == wal_before


@pytest.mark.parametrize("damage", ["empty", "schema", "page-size"])
def test_a_refused_unreadable_database_keeps_its_wal(codex_home, tmp_path, damage):
    from hermes_cli import backup
    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path)
    wal = live.with_name(live.name + "-wal")
    evidence = wal.read_bytes()
    if damage == "empty":
        live.write_bytes(b"")
    else:
        with live.open("r+b") as handle:
            handle.seek(100 if damage == "schema" else 16)
            handle.write(b"\xff" if damage == "schema" else b"\x00\x03")

    backup.restore_quick_snapshot(snapshot_id, hermes_home=root)

    assert wal.read_bytes() == evidence, f"the WAL of a {damage} database was lost"


def test_a_sidecar_hard_linked_to_a_credential_record_is_never_touched(live_root, tmp_path):
    from hermes_cli import backup
    root, _endpoint, chain_id = live_root
    record = root / "auth.recovery" / f"{chain_id}.json"
    before = record.read_bytes()
    live = root / "sessions.db"
    live.write_bytes(_backup_image(tmp_path).read_bytes())
    os.link(record, live.with_name(live.name + "-wal"))
    snapshot_id = _snapshot(root, {"sessions.db": live.read_bytes()})

    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is False

    assert record.read_bytes() == before, "SQLite wrote through a sidecar alias into credential state"


def test_a_refused_restore_leaves_the_live_database_and_later_commits_intact(codex_home, tmp_path):
    """Codex's post-publication writer, against a restore the guard refuses: nothing is published
    or cleaned up, and a separate SessionDB writer's commit survives the next startup."""
    from hermes_cli import backup
    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path)
    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is False
    out = _run(POST_PUBLISH_WRITER, live)
    assert out.returncode == 73, out.stderr[-2000:]
    assert _read(live) == {"rows": ["committed-live", "arrived-after-publication"], "integrity": "ok"}


# ── The guard, name by name ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("how", ["snapshot", "import"])
@pytest.mark.parametrize("name", ["sessions.db", "sessions.db-wal", "sessions.db-shm", "sessions.db-journal",
                                  ".sessions.db.snap_restore"])
def test_no_file_a_database_restore_may_write_can_be_a_credential_alias(live_root, tmp_path, how, name):
    root, _endpoint, chain_id = live_root
    record = root / "auth.recovery" / f"{chain_id}.json"
    before = record.read_bytes()
    live = root / "sessions.db"
    if name != "sessions.db":
        live.write_bytes(_backup_image(tmp_path).read_bytes())
    os.link(record, root / name)

    _restore(tmp_path, root, how, _backup_image(tmp_path).read_bytes())

    assert record.read_bytes() == before, f"a database restore wrote through {name} into a chain record"


def test_a_planted_staging_name_refuses_the_database(codex_home, tmp_path):
    from hermes_cli import backup
    root, _endpoint = codex_home
    live, snapshot_id = _prepare(root, tmp_path, link=False)
    staging = root / ".sessions.db.snap_restore"
    staging.write_bytes(b"planted")
    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is False
    assert staging.read_bytes() == b"planted"
    assert _read(live) == {"rows": ["committed-live"], "integrity": "ok"}


def test_the_post_update_auto_restore_is_guarded_too(live_root, tmp_path):
    from hermes_cli.update_cmd_maint import _restore_state_db_from_snapshot
    root, _endpoint, chain_id = live_root
    record = root / "auth.recovery" / f"{chain_id}.json"
    before = record.read_bytes()
    state = root / "state.db"
    state.write_bytes(b"\x00" * 4096)  # the corrupt database the auto-restore replaces
    os.link(record, state.with_name(state.name + "-wal"))

    assert _restore_state_db_from_snapshot(state, _backup_image(tmp_path)) is False

    assert record.read_bytes() == before, "the auto-restore deleted or wrote a credential alias"


@pytest.mark.parametrize("how", ["snapshot", "import"])
def test_an_ordinary_database_is_restored_by_upstreams_path(codex_home, tmp_path, how):
    """Control: no alias anywhere, so the guard lets upstream's page copy restore it."""
    root, _endpoint = codex_home
    live, _snapshot_id = _prepare(root, tmp_path, link=False)
    _restore(tmp_path, root, how, _backup_image(tmp_path).read_bytes())
    assert _read(live) == {"rows": ["backup-image"], "integrity": "ok"}
