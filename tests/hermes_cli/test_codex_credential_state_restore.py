"""Restoring a backup can never roll the Codex authority protocol back (stage 1).

An older backup taken before a logout carries an ACTIVE registry entry and the refresh token that
chain has since spent. On every restore path (backup import, quick snapshot, profile import) the
protocol files are never written and the restored auth.json loses its Codex material, so neither
the entry nor the spent token comes back; the rest of the restore proceeds. Fake credentials only.
"""

import io
import json
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

import hermes_cli.auth as auth
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, read_json, registry, write_store)


def _files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file() and "snapshots" not in p.parts}


def _backup_zip(root, dest):
    with zipfile.ZipFile(dest, "w") as zf:
        for rel, data in _files(root).items():
            zf.writestr(rel, data)
    return dest


def _profile_archive(root, dest):
    """A hand-built ``hermes profile import`` archive carrying the root store as its auth.json."""
    with tarfile.open(dest, "w:gz") as tf:
        for rel, data in (("imported/config.yaml", b"model: {}\n"),
                          ("imported/auth.json", (root / "auth.json").read_bytes())):
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return dest


@pytest.mark.parametrize("path", ["backup-import", "quick-snapshot", "profile-import"])
def test_an_older_backup_cannot_reinstate_a_retired_chain_or_its_spent_token(codex_home, tmp_path, monkeypatch,
                                                                             path):
    from hermes_cli import backup
    from hermes_cli.auth_codex_authority import authority_dispatch, logout_active_store

    root, endpoint = codex_home
    # A restore that got through must not go on to install services / wrappers on this machine.
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    r0 = endpoint.issue()
    write_store(root, singleton=r0)
    assert authority_dispatch() == "bootstrapped"
    if path == "backup-import":
        archive = _backup_zip(root, tmp_path / "old-backup.zip")
    elif path == "profile-import":
        archive = _profile_archive(root, tmp_path / "imported.tar.gz")
    else:
        snapshot_id = backup.create_quick_snapshot(label="old", hermes_home=root)
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # R0 is spent
    logout_active_store()  # the chain is retired, its rows removed
    before = {k: v for k, v in _files(root).items() if k.startswith("auth")}

    if path == "backup-import":
        backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    elif path == "profile-import":
        from hermes_cli.profiles import import_profile
        imported = import_profile(str(archive)) / "auth.json"
        assert r0[1] not in imported.read_text(encoding="utf-8"), "the spent token came back with the profile"
    else:
        assert backup.restore_quick_snapshot(snapshot_id, hermes_home=root) is True

    after = {k: v for k, v in _files(root).items() if k.startswith("auth") and k != "auth.json"}
    assert after == {k: v for k, v in before.items() if k != "auth.json"}, "protocol state came from the backup"
    assert all(c["state"] != "active" for c in registry(root)["chains"].values())
    store = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    assert r0[1] not in json.dumps(store), "the spent refresh token is back in auth.json"
