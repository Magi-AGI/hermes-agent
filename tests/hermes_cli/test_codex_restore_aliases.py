"""An alias never erases a credential file's identity in a restore, in EITHER direction (stage 1).

Classification looked only at a member's RESOLVED destination, and its identity checks covered a
few root files. So a protected NAME reaching an ordinary target lost its protection
(``auth.recovery`` a junction to ``<root>/records``: ``auth.recovery/<chain>.json`` resolved to
``records/<chain>.json``, was classified ordinary and overwritten — an older record can erase a
crashed refresh's intent, so a consumed token could be retried), and an ordinary name reaching a
protected file that was not one of those few was written (a hard link to a chain record or to a
profile's store, a profile's auth.json symlinked to a differently named file). Every destination is
now judged by its logical path AND its canonical path (and an existing file's identity) against the
canonical forms of every protected location; the most protected verdict wins.

A second bypass: the backup import picked the ``.db`` handler before checking the credential
classification, so a ``.db`` name aliasing a missing auth.json extracted raw backup credentials
through the alias. Credential classification now precedes every type handler.

Fake credentials only; directory aliases are junctions on Windows, file symlinks need the
``require_symlinks`` marker.
"""

import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, read_json, registry, write_store)

ANTHROPIC = {"id": "anth01", "label": "claude", "auth_type": "oauth", "priority": 0, "source": "manual",
             "access_token": "sk-ant-oat-restored", "refresh_token": "rt-ant-restored"}


@pytest.fixture
def live_root(codex_home, monkeypatch):
    """A bootstrapped root with a live Codex login; restores run with their side effects stubbed."""
    from hermes_cli import backup
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    (chain_id,) = registry(root)["chains"]
    return root, endpoint, chain_id


def _alias_dir(link, target):
    """A real directory alias on every host: a junction on Windows, a directory symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _backup_store(endpoint):
    spent = endpoint.issue()
    store = {"version": 1, "active_provider": "openai-codex",
             "providers": {"openai-codex": {"tokens": {"access_token": spent[0], "refresh_token": spent[1]}}},
             "credential_pool": {"anthropic": [ANTHROPIC]}}
    return store, set(spent)


def _import(tmp_path, members):
    from hermes_cli import backup
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("config.yaml", "model: {}\n")
        for name, data in members.items():
            zf.writestr(name, data)
    backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))


def _snapshot(root, members):
    """A quick snapshot of *root* holding exactly *members* (rel -> bytes)."""
    from hermes_cli import backup
    snap = backup._quick_snapshot_root(root) / "20260926-000000-crafted"
    for rel, data in members.items():
        (snap / rel).parent.mkdir(parents=True, exist_ok=True)
        (snap / rel).write_bytes(data)
    (snap / "manifest.json").write_text(json.dumps({"id": snap.name, "files": {r: len(d) for r, d in members.items()}}),
                                        encoding="utf-8")
    return snap.name


def _restore(tmp_path, root, members, how):
    from hermes_cli import backup
    if how == "import":
        _import(tmp_path, members)
    else:
        backup.restore_quick_snapshot(_snapshot(root, {k: v.encode() for k, v in members.items()}), hermes_home=root)


@pytest.mark.parametrize("how", ["import", "snapshot"])
@pytest.mark.parametrize("member", ["protected-name", "canonical-target"])
def test_a_records_directory_alias_protects_the_records_under_both_names(live_root, tmp_path, member, how):
    """``auth.recovery`` is a junction to ``<root>/records`` (the runtime writes its records THROUGH
    it). Neither ``auth.recovery/<chain>.json`` (a protected name reaching an ordinary target) nor
    ``records/<chain>.json`` (an ordinary name reaching the canonical records directory) is written."""
    root, _endpoint, chain_id = live_root
    (root / "auth.recovery").rename(root / "records")
    _alias_dir(root / "auth.recovery", root / "records")
    record_path = root / "records" / f"{chain_id}.json"
    kept = record_path.read_bytes()
    older = json.loads(kept)
    older["history"], older["attempt"] = [], None
    rel = f"auth.recovery/{chain_id}.json" if member == "protected-name" else f"records/{chain_id}.json"

    _restore(tmp_path, root, {rel: json.dumps(older)}, how)

    assert record_path.read_bytes() == kept, f"the chain record was overwritten through {rel}"


@pytest.mark.parametrize("protected", ["chain-record", "profile-store"])
def test_an_ordinary_name_hard_linked_to_a_protected_file_is_never_written(live_root, tmp_path, protected):
    """A snapshot restore copies INTO an existing file (``copy2``), so a hard link is written through:
    identity decides, for every protected file — not only root's store, registry and index."""
    root, endpoint, chain_id = live_root
    if protected == "chain-record":
        target = root / "auth.recovery" / f"{chain_id}.json"
    else:
        target = write_store(make_profile(root, "work"), singleton=endpoint.issue())
    kept = target.read_bytes()
    os.link(target, root / "notes.json")
    store, spent = _backup_store(endpoint)

    _restore(tmp_path, root, {"notes.json": json.dumps(store)}, "snapshot")

    assert target.read_bytes() == kept, f"the {protected} was written through a hard link"
    assert not any(t in target.read_text(encoding="utf-8") for t in spent)


def test_a_protected_name_hard_linked_to_an_ordinary_file_keeps_its_protection(live_root, tmp_path):
    root, endpoint, chain_id = live_root
    record = root / "auth.recovery" / f"{chain_id}.json"
    kept = record.read_bytes()
    os.link(record, root / "notes.json")
    _restore(tmp_path, root, {f"auth.recovery/{chain_id}.json": json.dumps({"from": "the backup"})}, "import")
    assert record.read_bytes() == kept and (root / "notes.json").read_bytes() == kept


def test_the_codex_cli_home_is_protected_at_its_canonical_location(live_root, tmp_path):
    """``~/.codex`` is a junction to ``~/codexdata``: a member naming ``codexdata`` directly is a Codex
    CLI credential, never written (and not mistaken for a Hermes store outside the root)."""
    root, endpoint, _chain_id = live_root
    userhome = tmp_path / "userhome"
    (userhome / "codexdata").mkdir()
    cli = userhome / "codexdata" / "auth.json"
    cli.write_text(json.dumps({"tokens": {"refresh_token": "rt-cli-live"}}), encoding="utf-8")
    _alias_dir(userhome / ".codex", userhome / "codexdata")
    store, spent = _backup_store(endpoint)
    _import(tmp_path, {"_external/codexdata/auth.json": json.dumps(store)})
    assert "rt-cli-live" in cli.read_text(encoding="utf-8")
    assert not any(t in cli.read_text(encoding="utf-8") for t in spent)


@pytest.mark.require_symlinks
def test_a_profile_store_symlinked_to_a_differently_named_file_is_stripped(live_root, tmp_path):
    from hermes_cli.auth_codex_authority import authority_dispatch
    root, endpoint, _chain_id = live_root
    profile = make_profile(root, "work")
    live = endpoint.issue()
    creds = write_store(profile, singleton=live).rename(profile / "creds.dat")
    os.symlink(creds, profile / "auth.json")
    authority_dispatch()
    kept_codex = read_json(creds)["providers"]["openai-codex"]
    store, spent = _backup_store(endpoint)

    _import(tmp_path, {"profiles/work/auth.json": json.dumps(store)})

    now = read_json(creds)
    assert not any(t in json.dumps(now) for t in spent), "backup Codex tokens landed through the alias"
    assert now["providers"]["openai-codex"] == kept_codex, "the profile's live Codex login was changed"
    assert now["credential_pool"]["anthropic"][0]["access_token"] == ANTHROPIC["access_token"]


@pytest.mark.require_symlinks
def test_a_db_name_aliasing_a_missing_store_never_extracts_raw_credentials(codex_home, tmp_path, monkeypatch):
    """Fresh install: ``saved.db`` is a dangling symlink to the missing auth.json. The member is a
    credential store by its canonical destination, so it goes through the Codex-stripping merge —
    never the database handler's missing-target extraction."""
    from hermes_cli import backup
    root, endpoint = codex_home
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    os.symlink(root / "auth.json", root / "saved.db")
    store, spent = _backup_store(endpoint)
    _import(tmp_path, {"saved.db": json.dumps(store)})
    on_disk = (root / "auth.json").read_text(encoding="utf-8") if (root / "auth.json").exists() else ""
    assert not any(t in on_disk for t in spent), "backup Codex tokens landed in auth.json through saved.db"


def test_credential_classification_precedes_the_database_handler(codex_home, tmp_path, monkeypatch):
    """The ordering invariant behind the ``.db`` alias case, on every host: a member classified as a
    credential store is written ONLY by the Codex-stripping merge, whatever its extension."""
    from hermes_cli import auth_codex_restore, backup
    root, endpoint = codex_home
    real_action = auth_codex_restore.restore_action
    monkeypatch.setattr(auth_codex_restore, "restore_action",
                        lambda path: "store" if os.path.basename(path) == "saved.db" else real_action(path))
    merged, db_handled = [], []
    monkeypatch.setattr(auth_codex_restore, "write_restored_store",
                        lambda target, data: merged.append(os.path.basename(target)))
    monkeypatch.setattr(backup, "_import_db_member", lambda zf, member, target, *a: db_handled.append(member))
    store, _spent = _backup_store(endpoint)
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("saved.db", json.dumps(store))
    with zipfile.ZipFile(archive) as zf:
        backup._import_members(zf, ["saved.db"], "", root, 1)
    assert db_handled == [], "the database handler ran for a credential store"
    assert merged == ["saved.db"]
