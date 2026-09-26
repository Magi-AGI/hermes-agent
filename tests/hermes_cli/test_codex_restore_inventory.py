"""A restore's protected inventory is complete or the restore refuses; no restore writes through a
hard link (stage 1, review round 10).

Codex reproduced three bypasses on 997ecf92ee (cases adapted from its read-only review tests):

* the quick-snapshot restore used ``shutil.copy2``, which opens an existing destination for writing
  and so writes THROUGH a hard link — and the identity inventory left out the Codex CLI credential
  files, so ``notes.json`` hard-linked to ``~/.codex/auth.json`` / ``$CODEX_HOME/auth.json`` replaced
  the current CLI login with the backup's;
* the inventory covered root, the active home and ``profiles/*`` but not a REGISTERED home such as
  ``<native root>/work``: restoring from root through a hard link rolled back its store and its
  chain record after a real (fake-token) rotation;
* inventory errors were swallowed: an unreadable ``auth.recovery`` dropped out of the inventory and
  the snapshot overwrote the current chain record with its pre-refresh version.

Every restored file is now published by write-temp + fsync + replace (another link to the old inode
keeps its content), the inventory includes the CLI credential files and every store the registry
names, and a failed inspection (not absence) refuses the affected write. Fake credentials only.
"""

import errno
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, registry, write_store  # noqa: F401
from tests.hermes_cli.test_codex_restore_aliases import _snapshot, live_root  # noqa: F401


def _restore(root, snapshot_id):
    from hermes_cli import backup
    from hermes_cli.auth_codex_registry import CodexAuthorityRefused
    try:
        backup.restore_quick_snapshot(snapshot_id, hermes_home=root)
    except (CodexAuthorityRefused, OSError):
        pass  # refusing rather than writing through an unclassified alias is safe


@pytest.mark.parametrize("cli_home", ["default", "configured"])
def test_a_snapshot_never_overwrites_codex_cli_credentials_through_a_hard_link(live_root, cli_home):
    root, _endpoint, _chain_id = live_root
    home = Path.home() / ".codex" if cli_home == "default" else Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    cli_auth = home / "auth.json"
    current = json.dumps({"tokens": {"access_token": "fake-current-access", "refresh_token": "fake-current-refresh"}})
    stale = json.dumps({"tokens": {"access_token": "fake-backup-access", "refresh_token": "fake-spent-refresh"}})
    cli_auth.write_text(current, encoding="utf-8")
    os.link(cli_auth, root / "notes.json")

    _restore(root, _snapshot(root, {"notes.json": stale.encode()}))

    assert cli_auth.read_text(encoding="utf-8") == current, "backup tokens landed in the Codex CLI credentials"
    assert os.path.samefile(cli_auth, root / "notes.json"), "the CLI credential's other name was not kept"


@pytest.mark.parametrize("deny_records_enumeration", [False, True])
def test_an_unreadable_protected_directory_is_never_treated_as_empty(live_root, monkeypatch, deny_records_enumeration):
    from hermes_cli import auth

    root, _endpoint, chain_id = live_root
    records_dir = root / "auth.recovery"
    record = records_dir / f"{chain_id}.json"
    stale_record = record.read_bytes()
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    current = record.read_bytes()
    assert stale_record != current
    os.link(record, root / "notes.json")
    snapshot_id = _snapshot(root, {"notes.json": stale_record})
    real_scandir, denied = os.scandir, []

    def scandir(path="."):
        if deny_records_enumeration and os.path.normcase(os.path.abspath(os.fspath(path))) == \
                os.path.normcase(str(records_dir)):
            denied.append(str(path))
            raise PermissionError(errno.EACCES, "injected: unreadable records directory", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    _restore(root, snapshot_id)

    assert denied or not deny_records_enumeration, "the injected failure never reached the inventory"
    assert record.read_bytes() == current, "an incomplete inventory let a chain record be rolled back"
    assert (root / "notes.json").read_bytes() == current, \
        "a destination whose protection could not be established was written"


@pytest.mark.parametrize("protected", ["store", "chain-record"])
def test_a_registered_home_outside_profiles_is_protected_when_restoring_from_root(
        codex_home, tmp_path, monkeypatch, protected):
    from hermes_cli import auth
    from hermes_cli.auth_codex_authority import authority_dispatch
    from hermes_constants import _get_platform_default_hermes_home

    _fixture_root, endpoint = codex_home
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "native-localappdata"))
    root = _get_platform_default_hermes_home()
    work = root / "work"
    work.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(work))
    store = write_store(work, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    (chain_id,) = registry(root)["chains"]
    target = store if protected == "store" else work / "auth.recovery" / f"{chain_id}.json"
    stale = target.read_bytes()
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    current = target.read_bytes()
    assert stale != current
    os.link(target, root / "notes.json")
    snapshot_id = _snapshot(root, {"notes.json": stale})
    monkeypatch.setenv("HERMES_HOME", str(root))

    _restore(root, snapshot_id)

    assert target.read_bytes() == current, "the restore missed a registered store of the same authority"
    assert (root / "notes.json").read_bytes() == current, "the registered file's other name was written"


def _sqlite(path, value):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.execute("DELETE FROM t")
    conn.execute("INSERT INTO t VALUES (?)", (value,))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("how", ["snapshot", "import"])
@pytest.mark.parametrize("kind", ["file", "database"])
def test_a_restored_file_never_changes_another_hard_link_to_the_old_file(live_root, tmp_path, how, kind):
    """Structural: an ordinary file is published by replacing the directory entry, so its other hard
    link keeps the old content; a database with another hard link is not restored at all (the
    pre-open guard: upstream's page copy would write into the shared file)."""
    from hermes_cli import backup
    root, _endpoint, _chain_id = live_root
    name = "notes.txt" if kind == "file" else "sessions.db"
    live, other = root / name, tmp_path / f"other-{name}"
    source = tmp_path / f"backup-{name}"
    if kind == "file":
        live.write_text("current", encoding="utf-8")
        source.write_text("from the backup", encoding="utf-8")
    else:
        _sqlite(live, "current")
        _sqlite(source, "from the backup")
    os.link(live, other)
    before = other.read_bytes()

    if how == "snapshot":
        _restore(root, _snapshot(root, {name: source.read_bytes()}))
    else:
        archive = tmp_path / "old.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("config.yaml", "model: {}\n")
            zf.write(source, name)
        backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))

    assert other.read_bytes() == before, f"restoring {name} wrote through its other hard link"
    if kind == "file":
        assert live.read_bytes() == source.read_bytes(), f"{name} was not restored"
    else:
        assert live.read_bytes() == before, "a hard-linked database was restored instead of refused"
