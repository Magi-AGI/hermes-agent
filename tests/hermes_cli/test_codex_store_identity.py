"""One canonical store identity decides a store's lock file and its chain-record directory.

Design rev 9 §1: "Store identity = canonical path of the store's auth.json. Distinct profiles
resolving to one file are one store." Registry keys were canonical, but the store lock file and the
``auth.recovery/`` directory followed whichever ALIAS path the caller used, so a profile whose
auth.json is a symlink to root's took a different lock than root and looked for the root chain's
record in its own directory — finding none, it fenced the healthy root chain. Fake credentials only.
"""

import os
import threading

import pytest

import hermes_cli.auth as auth
from tests.hermes_cli.codex_authority_env import codex_home, make_profile, registry, write_store  # noqa: F401


def _alias_dir(link, target):
    """A real directory alias on every host: a junction on Windows (no privilege needed), a
    directory symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _other_thread_gets_lock(path) -> bool:
    entered = []

    def attempt():
        try:
            with auth._auth_store_lock(timeout_seconds=0.3, target_path=path):
                entered.append(True)
        except TimeoutError:
            entered.append(False)

    t = threading.Thread(target=attempt)
    t.start()
    t.join(15)
    return entered == [True]


def test_aliases_of_one_store_share_its_record_directory(codex_home, tmp_path):
    from hermes_cli.auth_codex_records import records_dir

    root, _endpoint = codex_home
    write_store(root)
    alias_home = tmp_path / "alias-of-root"
    _alias_dir(alias_home, root)
    assert records_dir(alias_home / "auth.json") == records_dir(root / "auth.json")


@pytest.mark.require_symlinks
def test_a_profile_symlinked_to_root_shares_its_lock_and_refreshes_without_fencing(codex_home, monkeypatch):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    work = make_profile(root, "work")
    (work / "auth.json").symlink_to(root / "auth.json")
    with auth._auth_store_lock(target_path=root / "auth.json"):
        assert not _other_thread_gets_lock(work / "auth.json"), "the alias took a different store lock"
    from hermes_cli.auth_codex_authority import authority_dispatch
    assert authority_dispatch() == "bootstrapped"
    monkeypatch.setenv("HERMES_HOME", str(work))
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert len(endpoint.calls) == 1
    assert all(c["state"] == "active" for c in registry(root)["chains"].values())
