"""A stale caller never recreates a moved or deleted profile's directory through its store lock.

Design rev 9 r9-3 / §10 implementation notes: ``_file_lock`` used to ``mkdir`` its parent
unconditionally, so a process still pointed at a profile that another process renamed or deleted
recreated ``profiles/<old>/`` (and a fresh ``auth.lock`` in it) the moment it took the store lock.
For a PROFILE store whose directory is missing the lock now raises a transient "profile missing or
moved" error BEFORE anything is created. The root store keeps creating its home on first use.
"""

import shutil

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import codex_home, make_profile  # noqa: F401


def test_the_store_lock_of_a_missing_profile_raises_and_creates_nothing(codex_home, monkeypatch, tmp_path):
    root, _endpoint = codex_home
    work = make_profile(root, "work")
    monkeypatch.setenv("HERMES_HOME", str(work))
    shutil.rmtree(work)  # another process renamed / deleted the profile
    with pytest.raises(AuthError) as err:
        with auth._auth_store_lock():
            pass
    assert err.value.code == "profile_store_missing" and err.value.relogin_required is False
    assert not work.exists(), "the stale caller recreated the moved profile's directory"

    fresh_root = tmp_path / "fresh-root"  # legitimate first use of a root home still initializes it
    monkeypatch.setenv("HERMES_HOME", str(fresh_root))
    with auth._auth_store_lock():
        pass
    assert fresh_root.is_dir()
