"""A database restore replaces the file in one step and never deletes or writes into the current one.

The replacement path (every multi-link database, and the page-copy fallback) staged the image at the
predictable ``.<name>.snap_restore`` with ``shutil.copy2`` — so a hard link planted at that name to a
chain record received the SQLite bytes (Codex, stage-1 review round 11) — and then UNLINKED the live
database and its sidecars before moving the image in: a failed move left no database at all.

Now the image is staged in an exclusively created unique file, the old sidecars are set aside, the
image replaces the database with one ``os.replace``, and only then are the set-aside sidecars
deleted; any failure before the replace puts everything back. Cases 1-2 are adapted from Codex's
review reproductions. Fake credentials only.
"""

import errno
import os
from pathlib import Path

import pytest

from tests.hermes_cli.codex_authority_env import codex_home  # noqa: F401
from tests.hermes_cli.test_codex_restore_aliases import _import, _snapshot, live_root  # noqa: F401
from tests.hermes_cli.test_codex_restore_inventory import _sqlite


@pytest.mark.parametrize("how", ["snapshot", "import"])
def test_database_staging_never_writes_through_a_planted_hard_link(live_root, tmp_path, how):
    from hermes_cli import backup

    root, _endpoint, chain_id = live_root
    record = root / "auth.recovery" / f"{chain_id}.json"
    kept = record.read_bytes()
    live, source = root / "sessions.db", tmp_path / "source.db"
    _sqlite(live, "current database")
    _sqlite(source, "backup database")
    os.link(live, tmp_path / "other-database-name.db")
    os.link(record, root / ".sessions.db.snap_restore")

    if how == "snapshot":
        backup.restore_quick_snapshot(_snapshot(root, {"sessions.db": source.read_bytes()}), hermes_home=root)
    else:
        _import(tmp_path, {"sessions.db": source.read_bytes()})

    assert record.read_bytes() == kept, "database staging overwrote the chain record through a temp name"
    assert live.read_bytes() == source.read_bytes(), "the database was not restored"


def test_a_failed_database_publication_keeps_the_current_database(live_root, tmp_path, monkeypatch):
    from hermes_cli import backup

    root, _endpoint, _chain_id = live_root
    live, source = root / "sessions.db", tmp_path / "source.db"
    _sqlite(live, "current database")
    _sqlite(source, "backup database")
    os.link(live, tmp_path / "other-database-name.db")
    kept = live.read_bytes()
    snapshot_id = _snapshot(root, {"sessions.db": source.read_bytes()})
    real_replace, failed = os.replace, []

    def fail_publish(src, dst, *args, **kwargs):
        if Path(dst) == live:
            failed.append(True)
            raise PermissionError(errno.EACCES, "injected final-publication failure", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_publish)
    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is False
    assert failed, "the failure injection did not reach the final publication"
    assert live.exists() and live.read_bytes() == kept, "a failed publication lost the current database"


SIDECARS = {"-wal": b"old wal frames", "-shm": b"old shm index", "-journal": b"old hot journal"}


def _fail_on(monkeypatch, step):
    """Inject a failure at one step of ``_publish_db_replacing``."""
    from hermes_cli import backup
    real = {"copy": backup.shutil.copyfileobj, "mkstemp": backup.tempfile.mkstemp,
            "replace": os.replace, "unlink": os.unlink}
    calls = {"mkstemp": 0}

    def boom(what):
        raise OSError(errno.EIO, f"injected failure: {what}")

    if step == "stage":
        monkeypatch.setattr(backup.shutil, "copyfileobj", lambda *a, **k: boom("staging"))
    elif step == "reserve-aside":
        def mkstemp(*a, **k):
            calls["mkstemp"] += 1
            if k.get("suffix") == ".aside" and calls["mkstemp"] >= 3:  # after one sidecar is aside
                boom("reserving an aside name")
            return real["mkstemp"](*a, **k)
        monkeypatch.setattr(backup.tempfile, "mkstemp", mkstemp)
    elif step in ("move-aside", "publish"):
        def replace(src, dst, *a, **k):
            if step == "move-aside" and str(src).endswith("-shm"):
                boom("moving a sidecar aside")
            if step == "publish" and str(dst).endswith("sessions.db"):
                boom("replacing the database")
            return real["replace"](src, dst, *a, **k)
        monkeypatch.setattr(os, "replace", replace)
    elif step == "cleanup":
        def unlink(path, *a, **k):
            if str(path).endswith(".aside"):
                boom("deleting a set-aside sidecar")
            return real["unlink"](path, *a, **k)
        monkeypatch.setattr(os, "unlink", unlink)


@pytest.mark.parametrize("step", ["stage", "reserve-aside", "move-aside", "publish", "cleanup"])
def test_every_step_of_the_publication_keeps_a_matched_database_and_sidecars(tmp_path, monkeypatch, step):
    """At every failure point the path holds the OLD database with ALL its sidecars (and no staging
    leftovers), or — once the replace happened — the NEW database with none of the old sidecars."""
    from hermes_cli import backup

    live, source = tmp_path / "sessions.db", tmp_path / "image.db"
    live.write_bytes(b"old database")
    source.write_bytes(b"new database image")
    for suffix, data in SIDECARS.items():
        live.with_name(live.name + suffix).write_bytes(data)
    _fail_on(monkeypatch, step)

    if step == "cleanup":
        backup._publish_db_replacing(source, live)
        monkeypatch.undo()
        assert live.read_bytes() == b"new database image"
        assert not any(live.with_name(live.name + s).exists() for s in SIDECARS), \
            "an old sidecar stayed beside the new database"
        return
    with pytest.raises(OSError, match="injected"):
        backup._publish_db_replacing(source, live)
    monkeypatch.undo()
    assert live.read_bytes() == b"old database", f"a failure at {step} lost the current database"
    for suffix, data in SIDECARS.items():
        assert live.with_name(live.name + suffix).read_bytes() == data, f"{suffix} lost at {step}"
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("."))
    assert leftovers == [], f"staging leftovers after a failure at {step}: {leftovers}"
