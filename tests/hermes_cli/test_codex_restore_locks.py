"""A restore never replaces the runtime lock files that protect it (credential recovery protocol).

The restorer holds the migration lock while it extracts. An archive that carries
``auth.migration.lock`` (older backups did) used to be extracted over it: on POSIX the restorer then
held the OLD inode's lock while another process locked the new file, fenced the partial restore,
cleared the barrier — and the restorer's later members published active spent credentials. Runtime
lock files (the migration lock, every auth store lock, the Nous shared-store lock, lifecycle locks)
are never extracted, whatever path or alias the archive names them by. Real processes: the restorer
pauses mid-extraction while a separate process tries to recover. Fake credentials only.
"""

import os
import zipfile

from tests.hermes_cli.codex_authority_env import codex_home, registry, write_store  # noqa: F401
from tests.hermes_cli.codex_restore_children import recover, restore


def _identity(path):
    st = os.stat(path)  # a held lock cannot be read on Windows; its identity can
    return st.st_ino, st.st_size, st.st_mtime_ns


def test_a_restore_never_touches_the_lock_it_holds(codex_home, tmp_path):
    root, endpoint = codex_home
    userhome = tmp_path / "userhome"
    write_store(tmp_path, singleton=endpoint.issue())
    archive = tmp_path / "old-backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:  # an archive that carries the runtime locks
        zf.writestr("config.yaml", "model: {}\n")
        zf.writestr("auth.migration.lock", " ")
        zf.writestr("auth.lock", " ")
        zf.writestr("auth.json", (tmp_path / "auth.json").read_bytes())
        zf.writestr("zz-last.txt", "the restorer is still extracting after this pause")
    proc, log, gate = restore(archive, tmp_path, userhome, point="after-auth-json", mode="pause", wait_ready=True)
    try:
        lock = root / "auth.migration.lock"
        held = _identity(lock)
        other = recover(tmp_path, userhome, budget=0.5, refresh=False)
        assert other["dispatch"] == "busy", "another process took the protocol lock during the restore"
    finally:
        gate.write_text("go")
        out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, err[-2000:]
    extracted = [os.path.normcase(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert not [p for p in extracted if p.endswith(".lock")], f"lock files were extracted: {extracted}"
    assert _identity(lock) == held, "the lock file was replaced or rewritten"
    live = [c for c in registry(root)["chains"].values() if c["state"] != "retired"]
    assert live and all(c["fence"]["reason"] == "restored-from-backup" for c in live)


def test_an_external_alias_of_the_lock_is_not_extracted_either(codex_home, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from hermes_cli import backup
    from hermes_cli.auth_codex_authority import authority_dispatch

    _old_root, endpoint = codex_home
    root = tmp_path / "userhome" / "hroot"  # the default layout: the root lives under the home
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    lock = root / "auth.migration.lock"
    before = lock.read_bytes()
    archive = tmp_path / "crafted.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("config.yaml", "model: {}\n")
        zf.writestr("_external/hroot/auth.migration.lock", "FORGED")
    backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    assert lock.read_bytes() == before, "the migration lock was replaced through an external alias"
