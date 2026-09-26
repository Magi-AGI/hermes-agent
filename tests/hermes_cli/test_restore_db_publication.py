"""A database restore replaces the file in one step and never deletes or writes into the current one.

The replacement path (every multi-link database, and the page-copy fallback) staged the image at the
predictable ``.<name>.snap_restore`` with ``shutil.copy2`` — so a hard link planted at that name to a
chain record received the SQLite bytes (Codex, stage-1 review round 11) — and then UNLINKED the live
database and its sidecars before moving the image in: a failed move left no database at all.

Now the image is staged in an exclusively created unique file and replaces the database with one
``os.replace``; sidecars are discarded only after SQLite has proven they hold no recovery data
(test_restore_db_crash_recovery.py). Cases adapted from Codex's review reproductions. Fake
credentials only.
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
