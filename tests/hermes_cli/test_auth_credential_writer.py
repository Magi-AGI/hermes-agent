"""Strict credential writer + witnessed auth.json saves (credential recovery protocol, stage 1).

Invariants (design rev 9 §1 W/R):
- a store derived from one read never silently overwrites a NEWER auth.json (lost update);
- a store derived from an unparseable read can only replace those same bytes;
- a constructed (unwitnessed) store is merged into the file on disk, never blindly replaces it;
- a write that cannot rename atomically leaves the destination untouched (no in-place fallback);
- an uncertain write poisons its snapshot: nothing more may be saved from it.

Fake credentials only.
"""

import json
import os
from pathlib import Path

import pytest

import hermes_cli.auth as auth

_STALE = getattr(auth, "StaleSnapshotError", Exception)
_DEGRADED_STALE = getattr(auth, "AuthStoreStaleReadError", Exception)


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = home / "auth.json"
    path.write_text(json.dumps({"version": 1, "providers": {"nous": {"api_key": "fake-a"}}}),
                    encoding="utf-8")
    return path


def _disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_stale_snapshot_never_overwrites_a_newer_store(store):
    mine = auth._load_auth_store(store)
    peer = auth._load_auth_store(store)
    peer["providers"]["xai-oauth"] = {"tokens": {"access_token": "fake-peer"}}
    auth._save_auth_store(peer)

    mine["providers"]["nous"]["api_key"] = "fake-mine"
    with pytest.raises(_STALE):
        auth._save_auth_store(mine)

    on_disk = _disk(store)
    assert on_disk["providers"]["xai-oauth"]["tokens"]["access_token"] == "fake-peer"
    assert on_disk["providers"]["nous"]["api_key"] == "fake-a"


def test_degraded_read_cannot_erase_a_store_that_parses_again(store):
    good = store.read_bytes()
    store.write_text("{ torn", encoding="utf-8")
    degraded = auth._load_auth_store(store)
    assert degraded["providers"] == {}

    store.write_bytes(good)  # the peer's write completes: the file parses again
    with pytest.raises(_DEGRADED_STALE):
        auth._save_auth_store(degraded)
    assert _disk(store)["providers"]["nous"]["api_key"] == "fake-a"


def test_degraded_store_may_replace_the_same_corrupt_bytes(store):
    store.write_text("{ still corrupt", encoding="utf-8")
    degraded = auth._load_auth_store(store)
    degraded["providers"]["nous"] = {"api_key": "fake-relogin"}
    auth._save_auth_store(degraded)
    assert _disk(store)["providers"]["nous"]["api_key"] == "fake-relogin"


def test_constructed_store_merges_into_the_file_on_disk(store):
    auth._save_auth_store({"version": 1, "providers": {"xai-oauth": {"tokens": {"access_token": "fake-x"}}}})
    providers = _disk(store)["providers"]
    assert providers["nous"]["api_key"] == "fake-a", "an unwitnessed save must not drop existing providers"
    assert providers["xai-oauth"]["tokens"]["access_token"] == "fake-x"


def test_witness_renews_after_a_committed_save(store):
    mine = auth._load_auth_store(store)
    mine["providers"]["nous"]["api_key"] = "fake-b"
    auth._save_auth_store(mine)
    mine["providers"]["nous"]["api_key"] = "fake-c"
    auth._save_auth_store(mine)  # the dict matches disk after the first save
    assert _disk(store)["providers"]["nous"]["api_key"] == "fake-c"


@pytest.mark.windows_only
def test_contended_rename_is_not_committed_and_never_rewrites_in_place(store, monkeypatch):
    before = store.read_bytes()
    mine = auth._load_auth_store(store)
    mine["providers"]["nous"]["api_key"] = "fake-new"

    def _held(src, dst):  # another process holds auth.json without FILE_SHARE_DELETE
        err = PermissionError(13, "Access is denied")
        err.winerror = 5
        raise err

    monkeypatch.setattr(os, "replace", _held)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(OSError):
        auth._save_auth_store(mine)
    assert store.read_bytes() == before, "a failed rename must leave auth.json byte-for-byte unchanged"
    assert not [p for p in store.parent.iterdir() if p.name.endswith(".tmp")], "temp file left behind"


def test_uncertain_write_poisons_the_snapshot(store, monkeypatch):
    from hermes_cli import auth_credential_io as io

    mine = auth._load_auth_store(store)
    mine["providers"]["nous"]["api_key"] = "fake-new"
    with monkeypatch.context() as patch:
        patch.setattr(io, "_read_back", lambda _p: b"not what was written")
        with pytest.raises(io.CredentialWriteUncertain):
            auth._save_auth_store(mine)
    with pytest.raises(_STALE):
        auth._save_auth_store(mine)


def test_symlinked_store_is_written_through(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    real = tmp_path / "shared" / "auth.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"version": 1, "providers": {}}), encoding="utf-8")
    link = home / "auth.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    monkeypatch.setenv("HERMES_HOME", str(home))
    mine = auth._load_auth_store(link)
    mine["providers"]["nous"] = {"api_key": "fake-s"}
    auth._save_auth_store(mine, target_path=link)
    assert link.is_symlink(), "the store symlink was replaced by a regular file"
    assert _disk(real)["providers"]["nous"]["api_key"] == "fake-s"
