"""Profile delete / rename cannot race a Codex login landing in that profile (RULE-P1, stage 1).

The refusal was an unlocked pre-check: delete checked an unregistered profile, waited for the typed
confirmation, another process signed into Codex there, and the delete went ahead — the new
credential vanished while the registry still named that store as its owner. The check is now
repeated under the migration lock (every sign-in / import / bootstrap takes it first) and the lock
is held through the filesystem operation. Fake credentials only; every service / process helper of
the profile commands is stubbed so nothing outside the temp root is touched.
"""

import threading

import pytest

import hermes_cli.auth as auth
from hermes_cli import profiles
from tests.hermes_cli.codex_authority_env import codex_home, make_profile, write_store  # noqa: F401


@pytest.fixture
def quiet_profiles(monkeypatch):
    for name, value in (("_cleanup_gateway_service", None), ("_maybe_unregister_gateway_service", None),
                        ("_stop_gateway_process", None), ("_stop_profile_backends", None),
                        ("_notify_multiplexer", None), ("_check_gateway_running", False),
                        ("_purge_identity", True), ("_live_default_multiplexer", False)):
        monkeypatch.setattr(profiles, name, lambda *a, _v=value, **k: _v)


def test_a_sign_in_during_the_delete_confirmation_is_not_deleted(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work = make_profile(root, "work")
    write_store(work)
    pair = endpoint.issue()

    def confirm(_prompt):
        # Another Hermes signs into Codex in this profile while the user is typing.
        monkeypatch.setenv("HERMES_HOME", str(work))
        auth._save_codex_tokens({"access_token": pair[0], "refresh_token": pair[1]})
        monkeypatch.setenv("HERMES_HOME", str(root))
        return "work"

    monkeypatch.setattr("builtins.input", confirm)
    with pytest.raises(ValueError, match="Codex login"):
        profiles.delete_profile("work")
    assert (work / "auth.json").exists() and pair[1] in (work / "auth.json").read_text(encoding="utf-8")


def _migration_lock_free_elsewhere() -> bool:
    from hermes_cli.auth_codex_registry import try_migration_lock
    seen = []

    def probe():
        with try_migration_lock(0.0) as acquired:
            seen.append(acquired)

    t = threading.Thread(target=probe)
    t.start()
    t.join(10)
    return seen == [True]


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_ownership_changes_stay_excluded_through_the_filesystem_operation(codex_home, quiet_profiles,
                                                                         monkeypatch, operation):
    root, _endpoint = codex_home
    work = make_profile(root, "work")
    write_store(work)
    during = []
    if operation == "delete":
        real = profiles._rmtree_with_retry
        monkeypatch.setattr(profiles, "_rmtree_with_retry", lambda d, h: (
            during.append(_migration_lock_free_elsewhere()), real(d, h))[1])
        profiles.delete_profile("work", yes=True)
    else:
        real = profiles._record_profile_rename
        monkeypatch.setattr(profiles, "_record_profile_rename", lambda d, o: (
            during.append(_migration_lock_free_elsewhere()), real(d, o))[1])
        profiles.rename_profile("work", "play")
    assert during == [False], "a sign-in / import / bootstrap could register a login mid-operation"
