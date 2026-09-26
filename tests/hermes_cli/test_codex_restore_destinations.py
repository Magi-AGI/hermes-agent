"""The Codex restore guard classifies archive members by where they are WRITTEN (stage 1).

Inspection and extraction used to compute destinations separately: ``_external/`` members were
skipped by inspection although import writes them under the user's home (so
``_external/<home-relative hermes root>/auth.chains.json`` replaced the registry unguarded), and
names were compared case-sensitively (``AUTH.JSON`` is the same file as ``auth.json`` on a
case-insensitive filesystem). One destination resolver now serves both, and classification uses
the resolved, canonical, case-normalized destination: a Codex protocol file is never written
whatever path it is named by, and an auth.json by any name keeps the destination's Codex login.
Fake credentials only.
"""

import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, registry, write_store  # noqa: F401


@pytest.fixture
def home_root(codex_home, tmp_path, monkeypatch):
    """A Hermes root INSIDE the user's home (the default layout), bootstrapped with one chain."""
    from hermes_cli import backup
    from hermes_cli.auth_codex_authority import authority_dispatch

    _old_root, endpoint = codex_home
    root = tmp_path / "userhome" / "hroot"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    return root, endpoint


def _import(tmp_path, members):
    from hermes_cli import backup
    archive = tmp_path / "crafted.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("config.yaml", "model: {}\n")
        for name, data in members.items():
            zf.writestr(name, data)
    backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))


def _alias_dir(link, target):
    """A real directory alias on every host: a junction on Windows, a directory symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _auth_files(root):
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and p.name.startswith("auth")}


@pytest.mark.parametrize("member", ["external-registry", "aliased-records-dir"])
def test_a_member_is_classified_by_its_real_destination(home_root, tmp_path, member):
    root, _endpoint = home_root
    before = _auth_files(root)
    forged = registry(root)
    for chain in forged["chains"].values():
        chain.update(state="active", fence=None)
    if member == "external-registry":
        _import(tmp_path, {"_external/hroot/auth.chains.json": json.dumps(forged)})
    elif member == "aliased-records-dir":
        (cid,) = forged["chains"]
        _alias_dir(root / "notes", root / "auth.recovery")
        record = json.loads((root / "auth.recovery" / f"{cid}.json").read_text(encoding="utf-8"))
        record["history"] = []
        _import(tmp_path, {f"notes/{cid}.json": json.dumps(record)})
        on_disk = (root / "auth.recovery" / f"{cid}.json").read_bytes()
        assert on_disk != json.dumps(record).encode(), "the chain record was overwritten through a directory alias"
    assert _auth_files(root) == before, "Codex credential state was overwritten through an unguarded destination"


@pytest.mark.windows_only  # NTFS is case-insensitive: AUTH.JSON IS auth.json there
def test_a_differently_cased_store_name_is_the_same_store(home_root, tmp_path):
    root, endpoint = home_root
    before = _auth_files(root)
    spent = endpoint.issue()
    store = {"version": 1, "providers": {"openai-codex": {"tokens": {
        "access_token": spent[0], "refresh_token": spent[1]}}}, "active_provider": "openai-codex"}
    _import(tmp_path, {"AUTH.JSON": json.dumps(store)})
    after = _auth_files(root)
    assert {k: v for k, v in after.items() if k != "auth.json"} == \
        {k: v for k, v in before.items() if k != "auth.json"}
    kept, now = json.loads(before["auth.json"]), json.loads(after["auth.json"])
    assert now["providers"]["openai-codex"] == kept["providers"]["openai-codex"], \
        "the differently cased name replaced the live Codex login"
    assert spent[1] not in after["auth.json"].decode("utf-8")
