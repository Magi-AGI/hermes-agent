"""A restore writes credential state only into the authority it protects (stage 1).

The guard (state check, migration lock, barrier, fence) belongs to ONE Hermes root. Directory
containment is not authority membership: an independent root nested inside the restoring root
kept its own registry and locks, so old state restored into it was never fenced and replayed; and a
restore into another root held only the restorer's own lock, so a Codex sign-in landing there
between the check and the write was overwritten. A restore now writes an auth store or protocol
file only at one of the protected authority's own managed locations; an explicit quick-snapshot
restore into another home runs the whole guard under THAT home's authority (its migration lock
held from validation through the write, its barrier and fence). Fake credentials only.
"""

import json
import time
import zipfile
from types import SimpleNamespace

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, registry, write_store  # noqa: F401
from tests.hermes_cli.codex_restore_children import SIGNER, SNAPSHOT_RESTORER, spawn_in


@pytest.fixture
def quiet_backup(monkeypatch):
    from hermes_cli import backup
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    return backup


def _bootstrap_root(monkeypatch, root, pair):
    from hermes_cli.auth_codex_authority import authority_dispatch
    monkeypatch.setenv("HERMES_HOME", str(root))
    write_store(root, singleton=pair)
    assert authority_dispatch() == "bootstrapped"


def test_an_independent_root_nested_inside_is_not_part_of_the_restoring_authority(
        codex_home, monkeypatch, tmp_path, quiet_backup):
    root_a, endpoint = codex_home
    (root_a / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    root_b = root_a / "nested" / "root-b"
    root_b.mkdir(parents=True)
    _bootstrap_root(monkeypatch, root_b, endpoint.issue())  # B: its own registry, locks, login
    before = {p.name: p.read_bytes() for p in root_b.iterdir() if p.is_file() and not p.name.endswith(".lock")}
    monkeypatch.setenv("HERMES_HOME", str(root_a))  # the restore runs under fresh root A
    forged = registry(root_b)
    for chain in forged["chains"].values():
        chain.update(state="active", fence=None)
    spent = endpoint.issue()
    archive = tmp_path / "old.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("config.yaml", "model: {}\n")
        zf.writestr("nested/root-b/auth.json", json.dumps({"version": 1, "providers": {"openai-codex": {
            "tokens": {"access_token": spent[0], "refresh_token": spent[1]}}}}))
        zf.writestr("nested/root-b/auth.chains.json", json.dumps(forged))
    with pytest.raises(SystemExit):
        quiet_backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    after = {p.name: p.read_bytes() for p in root_b.iterdir() if p.is_file() and not p.name.endswith(".lock")}
    assert after == before, "a nested independent root's credentials were overwritten"


def test_a_snapshot_restore_into_another_home_is_guarded_by_that_home(codex_home, monkeypatch, tmp_path):
    from hermes_cli import backup

    root_a, endpoint = codex_home
    home_b = tmp_path / "hermes-b"
    home_b.mkdir()
    write_store(home_b, singleton=endpoint.issue())  # B: Codex tokens, no authority state yet
    snapshot_id = backup.create_quick_snapshot(label="b", hermes_home=home_b)
    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=home_b) is True
    chains = registry(home_b)["chains"].values()
    assert chains and all(c["fence"]["reason"] == "restored-from-backup" for c in chains), \
        "B's restored login must land fenced under B's own authority"
    assert not (root_a / "auth.chains.json").exists() and not (root_a / "auth.restore-barrier.json").exists()


def test_a_sign_in_into_the_target_home_cannot_land_between_check_and_write(codex_home, tmp_path):
    from hermes_cli import backup

    _root_a, endpoint = codex_home
    home_b = tmp_path / "hermes-b"
    home_b.mkdir()
    (home_b / "auth.json").write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": {
        "anthropic": [{"id": "anth01", "label": "c", "auth_type": "api_key", "priority": 0, "source": "manual",
                       "access_token": "sk-ant-old"}]}}), encoding="utf-8")
    snapshot_id = backup.create_quick_snapshot(label="b", hermes_home=home_b)  # no Codex material
    userhome = tmp_path / "userhome"
    ready, gate = tmp_path / "ready", tmp_path / "gate"
    restorer = spawn_in(SNAPSHOT_RESTORER, [snapshot_id, home_b, ready, gate], userhome, _root_a)
    deadline = time.monotonic() + 120
    while not ready.exists():
        assert restorer.poll() is None and time.monotonic() < deadline, restorer.communicate()
        time.sleep(0.05)
    fresh = endpoint.issue()
    signer = spawn_in(SIGNER, list(fresh), userhome, home_b)  # B's own Codex sign-in, mid-restore
    time.sleep(3)
    signed_early = signer.poll() is not None
    gate.write_text("go")
    r_out, r_err = restorer.communicate(timeout=180)
    s_out, s_err = signer.communicate(timeout=180)
    assert "RESTORED" in r_out, r_err[-2000:]
    assert not signed_early, "B's sign-in completed while the restore into B was between check and write"
    assert "SIGNED-IN" in s_out, s_err[-2000:]
    store = json.loads((home_b / "auth.json").read_text(encoding="utf-8"))
    assert store["providers"]["openai-codex"]["tokens"]["refresh_token"] == fresh[1], \
        "the restore overwrote B's fresh Codex login"
