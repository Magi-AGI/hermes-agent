"""Profile rename / delete of a profile holding a Codex login (credential recovery protocol stage 2,
design rev 9 RULE-P1..P4, RULE-G4/G5, §13 r6-3 / r7-2 / r7-6 / r8-1 / r8-2 / r9-4).

Stage 1 refused both; now they are ownership ops. Fake credentials only: the token endpoint is the
in-process ledger of ``codex_authority_env`` and every service / process helper of the profile
commands is stubbed, so nothing outside the temp root is touched.
"""

import json
import os

import pytest

import hermes_cli.auth as auth
from hermes_cli import profiles
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, read_json, registry, write_store)


@pytest.fixture
def quiet_profiles(monkeypatch):
    for name, value in (("_cleanup_gateway_service", None), ("_maybe_unregister_gateway_service", None),
                        ("_stop_gateway_process", None), ("_stop_profile_backends", None),
                        ("_notify_multiplexer", None), ("_check_gateway_running", False),
                        ("_purge_identity", True), ("_live_default_multiplexer", False),
                        ("create_wrapper_script", None), ("remove_wrapper_script", None)):
        monkeypatch.setattr(profiles, name, lambda *a, _v=value, **k: _v)
    import hermes_cli.profile_identity as identity
    monkeypatch.setattr(identity, "_migrate_profile_identity", lambda *a, **k: None)


def _dispatch():
    from hermes_cli.auth_codex_authority import authority_dispatch
    return authority_dispatch()


def _key(path):
    from hermes_cli.auth_credential_io import store_key
    return store_key(path)


def _only_chain(root):
    (cid, chain), = [(c, e) for c, e in registry(root)["chains"].items() if e["state"] != "retired"]
    return cid, chain


def _refresh_in(monkeypatch, home):
    monkeypatch.setenv("HERMES_HOME", str(home))
    try:
        return auth.resolve_codex_runtime_credentials(force_refresh=True)["api_key"]
    finally:
        monkeypatch.setenv("HERMES_HOME", str(home.parent.parent if home.parent.name == "profiles" else home))


def _work_with_login(root, endpoint, name="work"):
    home = make_profile(root, name)
    pair = endpoint.issue()
    write_store(home, singleton=pair)
    assert _dispatch() == "bootstrapped" or registry(root)
    return home, pair


def _failing_move(monkeypatch, exc=PermissionError(13, "The process cannot access the file")):
    real = profiles._rename_checked_profile

    def wrapped(old_canon, new_canon, old_dir, new_dir, move):
        def failing(fn):
            def boom():
                raise exc
            return move(boom)
        return real(old_canon, new_canon, old_dir, new_dir, failing)

    monkeypatch.setattr(profiles, "_rename_checked_profile", wrapped)


def test_rename_moves_the_login_and_its_record(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, pair = _work_with_login(root, endpoint)
    cid, before = _only_chain(root)
    profiles.rename_profile("work", "play")
    play = root / "profiles" / "play"
    reg = registry(root)
    chain = reg["chains"][cid]
    assert not reg["ops"] and chain["lifecycle"] is None
    assert chain["owner"] == _key(play / "auth.json") and chain["epoch"] == before["epoch"] + 1
    assert all(r["store"] == _key(play / "auth.json") for r in chain["rows"])
    record = read_json(play / "auth.recovery" / f"{cid}.json")
    assert record["epoch"] == chain["epoch"], "RULE-S1: the record's epoch follows the decision"
    assert not work.exists()
    key = _refresh_in(monkeypatch, play)
    assert endpoint.calls == [pair[1]] and key != pair[0]


@pytest.mark.parametrize("reason", ["registry-rebuilt", "indeterminate", "legacy-rescue"])
def test_rename_ends_with_exactly_the_prior_fence(codex_home, quiet_profiles, reason):
    """r7-2: completion never activates anything; the chain keeps its credential fence."""
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    cid, _ = _only_chain(root)
    from hermes_cli.auth_codex_registry import migration_lock, read_registry, write_registry
    with migration_lock():
        data = read_registry().data
        data["chains"][cid].update(state="fenced", fence={"reason": reason, "epoch": 1, "evidence": {"x": 1}})
        write_registry(data)
    fence = registry(root)["chains"][cid]["fence"]
    profiles.rename_profile("work", "play")
    chain = registry(root)["chains"][cid]
    assert chain["state"] == "fenced" and chain["fence"] == fence and chain["lifecycle"] is None


def test_failed_move_is_stuck_terminal_and_scoped(codex_home, quiet_profiles, monkeypatch):
    """r6-3 / r7-6 / r8-1: a move that fails leaves the op lifecycle-stuck: automatic dispatch skips it
    (no retry per request), its chain stays excluded, every op touching P or Q is refused, and an
    unrelated profile R keeps working and can sign in. ``recover --op`` then finishes it."""
    from hermes_cli.auth_codex_profile_ops import recover_op
    root, endpoint = codex_home
    other = make_profile(root, "other")
    other_pair = endpoint.issue()
    write_store(other, singleton=other_pair)
    work, pair = _work_with_login(root, endpoint)
    _failing_move(monkeypatch)
    with pytest.raises(ValueError, match="recover --op"):
        profiles.rename_profile("work", "play")
    (op_id, op), = registry(root)["ops"].items()
    assert op["status"] == "lifecycle-stuck" and work.is_dir()
    for _ in range(5):
        assert _dispatch() == "done"
    assert registry(root)["ops"][op_id]["status"] == "lifecycle-stuck", "dispatch never retries it"
    with pytest.raises(AuthError) as err:
        _refresh_in(monkeypatch, work)
    assert f"recover --op {op_id[:8]}" in str(err.value)
    assert endpoint.calls == []
    # Refused while unfinished: a login into P, a profile named Q, a delete of P, a rename to Q.
    monkeypatch.setenv("HERMES_HOME", str(work))
    fresh = endpoint.issue()
    with pytest.raises(AuthError):
        auth._save_codex_tokens({"access_token": fresh[0], "refresh_token": fresh[1]})
    monkeypatch.setenv("HERMES_HOME", str(root))
    with pytest.raises(ValueError, match=op_id[:8]):
        profiles.create_profile("play")
    with pytest.raises(ValueError, match=op_id[:8]):
        profiles.delete_profile("work", yes=True)
    make_profile(root, "third")
    with pytest.raises(ValueError, match=op_id[:8]):
        profiles.rename_profile("third", "play")
    # The unrelated profile refreshes and signs in normally throughout.
    assert _refresh_in(monkeypatch, other) and endpoint.calls == [other_pair[1]]
    monkeypatch.setenv("HERMES_HOME", str(other))
    relogin = endpoint.issue()
    auth._save_codex_tokens({"access_token": relogin[0], "refresh_token": relogin[1]})
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert recover_op(op_id[:8]) == "rolled_forward"  # the handle is released: the move succeeds
    play = root / "profiles" / "play"
    assert play.is_dir() and not work.exists() and not registry(root)["ops"]
    assert _refresh_in(monkeypatch, play)
    assert endpoint.calls[-1] == pair[1]


def test_abandon_restores_the_source_at_new_epochs(codex_home, quiet_profiles, monkeypatch):
    from hermes_cli.auth_codex_profile_ops import recover_op
    root, endpoint = codex_home
    work, pair = _work_with_login(root, endpoint)
    cid, before = _only_chain(root)
    _failing_move(monkeypatch)
    with pytest.raises(ValueError):
        profiles.rename_profile("work", "play")
    (op_id, _op), = registry(root)["ops"].items()
    decided_epoch = registry(root)["chains"][cid]["epoch"]
    assert recover_op(op_id, abandon=True) == "abandoned"
    reg = registry(root)
    chain = reg["chains"][cid]
    assert not reg["ops"] and chain["lifecycle"] is None
    assert chain["owner"] == before["owner"] == _key(work / "auth.json")
    assert chain["epoch"] == decided_epoch + 1, "monotonic: never back to the old value"
    assert read_json(work / "auth.recovery" / f"{cid}.json")["epoch"] == chain["epoch"]
    assert not list((root / "auth.ops").glob("*.json")), "both payloads archived"
    archived = [p.name for p in (root / "auth.ops" / "archive").glob("*.json")]
    assert any(".abandon." in n for n in archived) and any(n.startswith(op_id) and ".abandon." not in n for n in archived)
    assert _refresh_in(monkeypatch, work) and endpoint.calls == [pair[1]]


def test_rename_stopped_before_its_move_is_cancelled_not_completed_later(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, _pair = _work_with_login(root, endpoint)
    cid, _ = _only_chain(root)

    def quiesce_fails(*_a, **_k):
        raise RuntimeError("gateway would not stop")

    monkeypatch.setattr(profiles, "_live_default_multiplexer", quiesce_fails)
    with pytest.raises(RuntimeError, match="would not stop"):
        profiles.rename_profile("work", "play")
    assert not registry(root)["ops"] and registry(root)["chains"][cid]["lifecycle"] is None
    _dispatch()
    assert work.is_dir() and not (root / "profiles" / "play").exists(), "no later dispatch moves it"


def test_delete_signs_the_login_out_and_keeps_its_evidence(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, pair = _work_with_login(root, endpoint)
    _refresh_in(monkeypatch, work)  # the chain now has history: the old token and its successor
    rotated = read_json(work / "auth.json")["providers"]["openai-codex"]["tokens"]
    cid, _ = _only_chain(root)
    profiles.delete_profile("work", yes=True)
    assert not work.exists()
    reg = registry(root)
    assert reg["chains"][cid]["state"] == "retired" and not reg["chains"][cid]["rows"] and not reg["ops"]
    index = read_json(root / "auth.chains.retired.json")["chains"][cid]
    assert index["complete"] is True
    from hermes_cli.auth_codex_authority import import_pair
    for access, refresh in (pair, (rotated["access_token"], rotated["refresh_token"])):
        assert import_pair(access, refresh, reason="test") is None, "a deleted profile's token came back"
    assert not (root / "profiles" / "work").exists(), "nothing recreated the deleted profile"


def test_root_chain_rows_in_a_renamed_profile_follow_the_path(codex_home, quiet_profiles, monkeypatch):
    """A root-owned chain with a copy row in the profile: the row's store key moves, the chain stays
    root's at a new epoch (its record at root is rewritten), and refresh continues from root."""
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    work = make_profile(root, "work")
    write_store(work, singleton=pair)
    _dispatch()
    cid, chain = _only_chain(root)
    assert chain["owner"] == _key(root / "auth.json")
    profiles.rename_profile("work", "play")
    chain = registry(root)["chains"][cid]
    stores = {r["store"] for r in chain["rows"]}
    assert _key(root / "profiles" / "play" / "auth.json") in stores and _key(work / "auth.json") not in stores
    assert read_json(root / "auth.recovery" / f"{cid}.json")["epoch"] == chain["epoch"]
    assert _refresh_in(monkeypatch, root / "profiles" / "play")


def test_rename_of_a_profile_linking_to_root_moves_no_codex_state(codex_home, quiet_profiles):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    work = make_profile(root, "work")
    try:
        os.symlink(root / "auth.json", work / "auth.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _dispatch()
    before = json.dumps(registry(root)["chains"], sort_keys=True)
    profiles.rename_profile("work", "play")
    assert json.dumps(registry(root)["chains"], sort_keys=True) == before and not registry(root)["ops"]
