"""A restore protects only its own Hermes root, so it never writes credentials into another one.

The guard's state check, lock, barrier and fence all belong to the root the restore runs under
(A). An ``_external/`` member can name a destination inside a DIFFERENT Hermes root (B) under the
user's home; restored from fresh root A, B's auth.json / registry / records were overwritten with
spent credentials that nothing fenced. Credential or protocol destinations outside the protected
root are now refused. Fake credentials only.
"""

import json
import zipfile
from types import SimpleNamespace

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, registry, write_store  # noqa: F401


def test_a_restore_under_root_a_never_writes_credentials_into_root_b(codex_home, tmp_path, monkeypatch):
    from hermes_cli import backup
    from hermes_cli.auth_codex_authority import authority_dispatch

    _old_root, endpoint = codex_home
    home = tmp_path / "userhome"
    root_a, root_b = home / "hermes-a", home / "hermes-b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    monkeypatch.setenv("HERMES_HOME", str(root_b))  # B: an existing install with a live login
    write_store(root_b, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    before = {p.name: p.read_bytes() for p in root_b.iterdir() if p.is_file() and not p.name.endswith(".lock")}

    monkeypatch.setenv("HERMES_HOME", str(root_a))  # the restore runs under fresh root A
    spent = endpoint.issue()
    forged = registry(root_b)
    for chain in forged["chains"].values():
        chain.update(state="active", fence=None)
    archive = tmp_path / "crafted.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("config.yaml", "model: {}\n")
        zf.writestr("_external/hermes-b/auth.json", json.dumps({"version": 1, "providers": {"openai-codex": {
            "tokens": {"access_token": spent[0], "refresh_token": spent[1]}}}}))
        zf.writestr("_external/hermes-b/auth.chains.json", json.dumps(forged))
    with pytest.raises(SystemExit):
        backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    after = {p.name: p.read_bytes() for p in root_b.iterdir() if p.is_file() and not p.name.endswith(".lock")}
    assert after == before, "another Hermes root's credentials were overwritten"
