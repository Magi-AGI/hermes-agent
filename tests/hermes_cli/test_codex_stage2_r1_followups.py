"""Stage-2 review round 1 follow-ups beyond the five reproductions (``test_codex_stage2_review_edges``).

r1-1  a process whose profile was renamed away fails closed on EVERY resolution path (forced
      refresh, the pool, a borrowing profile) — never another store's (account's) credential;
r1-2  abandonment with either payload temporarily unreadable, and a crash during that recovery:
      completed only from the compensation, at the recorded epochs;
r1-3  a delete whose directory removal fails retires nothing; a crash between the decision and the
      removal keeps the login, a crash after the removal retires it; an alias profile (auth.json
      linking to root's) deletes without touching root's login;
r1-5  a rename interrupted after its move (before APPLY, before finalization, during it, before
      COMMIT) and recovered by the job alone or by ``hermes auth recover --op`` leaves the profile
      state — active profile, wrappers, identity migration, rename record — exactly as the
      uninterrupted rename does.
Crashes are ``SystemExit`` raised at the boundary (nothing in these paths catches it). Fake
credentials only.
"""

import json
from pathlib import Path

import pytest

import hermes_cli.auth as auth
from hermes_cli import auth_codex_profile_ops as pops
from hermes_cli import profiles
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, pool_row, read_json, registry, write_store)
from tests.hermes_cli.test_codex_lifecycle_recovery import _stuck_rename
from tests.hermes_cli.test_codex_profile_lifecycle import (  # noqa: F401
    _dispatch, _failing_move, _key, _only_chain, _refresh_in, _work_with_login, quiet_profiles)


class Killed(SystemExit):
    pass


def _die(*_a, **_k):
    raise Killed("crash")


# ── r1-1 ──────────────────────────────────────────────────────────────────────────────────────

def test_a_renamed_away_profile_fails_closed_on_every_path(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    root_pair, work_pair = endpoint.issue("root"), endpoint.issue("work")
    write_store(root, singleton=root_pair, pool=[pool_row("rootrow", root_pair)])
    work = make_profile(root, "work")
    write_store(work, singleton=work_pair)
    _dispatch()
    profiles.rename_profile("work", "play")
    monkeypatch.setenv("HERMES_HOME", str(work))  # the stale process
    for force in (False, True):
        with pytest.raises(AuthError) as err:
            auth.resolve_codex_runtime_credentials(force_refresh=force)
        assert err.value.code == "profile_store_missing"
    with pytest.raises(AuthError) as err:
        auth.read_credential_pool("openai-codex")
    assert err.value.code == "profile_store_missing"
    from agent.auxiliary_client import _read_codex_access_token
    assert _read_codex_access_token() not in (root_pair[0],), "the aux path picked root's account"
    assert endpoint.calls == [] and not work.exists()
    monkeypatch.setenv("HERMES_HOME", str(root))
    borrower = make_profile(root, "borrower")  # an EXISTING profile still borrows root as upstream does
    monkeypatch.setenv("HERMES_HOME", str(borrower))
    assert auth.resolve_codex_runtime_credentials()["api_key"] == root_pair[0]


# ── r1-2 ──────────────────────────────────────────────────────────────────────────────────────

def _abandon_to_decision2(root, endpoint, monkeypatch):
    work, pair, cid, _before, op_id = _stuck_rename(root, endpoint, monkeypatch)
    with monkeypatch.context() as stop:
        stop.setattr(pops, "apply_payload", _die)
        with pytest.raises(Killed):
            pops.recover_op(op_id, abandon=True)
    op = registry(root)["ops"][op_id]
    assert op["direction"] == "abandon"
    return work, pair, cid, op_id, op["abandon_epochs"][cid]


def _assert_abandoned(root, work, cid, epoch):
    reg = registry(root)
    chain = reg["chains"][cid]
    assert not reg["ops"] and chain["lifecycle"] is None
    assert chain["owner"] == _key(work / "auth.json") and chain["epoch"] == epoch and chain["state"] == "active"
    assert read_json(work / "auth.recovery" / f"{cid}.json")["epoch"] == epoch


def test_abandonment_never_needs_the_forward_payload(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, pair, cid, op_id, epoch = _abandon_to_decision2(root, endpoint, monkeypatch)
    forward = root / "auth.ops" / f"{op_id}.json"
    raw = forward.read_bytes()
    forward.write_bytes(b"{")  # the forward payload is unreadable: irrelevant after DECISION-2
    _dispatch()
    _assert_abandoned(root, work, cid, epoch)
    assert raw and _refresh_in(monkeypatch, work) and endpoint.calls == [pair[1]]


def test_a_crash_during_recovery_of_a_reread_compensation(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, pair, cid, op_id, epoch = _abandon_to_decision2(root, endpoint, monkeypatch)
    comp = root / "auth.ops" / f"{op_id}.abandon.json"
    raw = comp.read_bytes()
    comp.write_bytes(b"{")
    _dispatch()
    assert registry(root)["ops"][op_id]["status"] == "unrecoverable"
    assert registry(root)["chains"][cid]["epoch"] == epoch, "never an older epoch"
    comp.write_bytes(raw)
    with monkeypatch.context() as crash:
        crash.setattr(pops, "commit_op", _die)  # the recovery itself dies after APPLY-2
        with pytest.raises(Killed):
            from hermes_cli.auth_codex_authority import dispatch_locked
            from hermes_cli.auth_codex_registry import migration_lock
            with migration_lock():
                dispatch_locked()
    assert registry(root)["chains"][cid]["epoch"] == epoch
    _dispatch()
    _assert_abandoned(root, work, cid, epoch)
    assert _refresh_in(monkeypatch, work) and endpoint.calls == [pair[1]]


# ── r1-3 ──────────────────────────────────────────────────────────────────────────────────────

def test_a_delete_whose_directory_removal_fails_retires_nothing(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, pair = _work_with_login(root, endpoint)
    before = registry(root)["chains"]
    monkeypatch.setattr(profiles, "_rmtree_with_retry", lambda *a: (_ for _ in ()).throw(PermissionError(13, "held")))
    with pytest.raises(RuntimeError, match="Could not remove"):
        profiles.delete_profile("work", yes=True)
    reg = registry(root)
    assert reg["chains"] == before and not reg["ops"], "a surviving profile's login was retired"
    assert pair[1] in (work / "auth.json").read_text(encoding="utf-8"), "its credential was removed"


@pytest.mark.parametrize("boundary", ["after-decision", "after-removal"])
def test_a_crash_around_the_directory_removal(codex_home, quiet_profiles, monkeypatch, boundary):
    root, endpoint = codex_home
    work, pair = _work_with_login(root, endpoint)
    cid, _ = _only_chain(root)
    real = profiles._rmtree_with_retry

    def rmtree(path, handler):
        if boundary == "after-removal":
            real(path, handler)
        raise Killed(boundary)

    with monkeypatch.context() as crash:
        crash.setattr(profiles, "_rmtree_with_retry", rmtree)
        crash.setattr(pops, "_settle_delete", _die)  # the settling never runs: a real death
        with pytest.raises(Killed):
            profiles.delete_profile("work", yes=True)
    _dispatch()  # the job alone decides by what the removal left
    reg = registry(root)
    assert not reg["ops"] and reg["chains"][cid]["lifecycle"] is None
    if boundary == "after-decision":
        assert reg["chains"][cid]["state"] == "active" and work.is_dir()
    else:
        assert reg["chains"][cid]["state"] == "retired" and not work.exists()
        from hermes_cli.auth_codex_evidence import known_material
        from hermes_cli.auth_codex_registry import read_registry
        assert known_material(read_registry()).retired(*pair)


@pytest.mark.require_symlinks
def test_deleting_a_profile_linked_to_root_keeps_roots_login(codex_home, quiet_profiles):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    work = make_profile(root, "work")
    (work / "auth.json").symlink_to(root / "auth.json")
    _dispatch()
    before = registry(root)["chains"]
    profiles.delete_profile("work", yes=True)
    assert registry(root)["chains"] == before and (root / "auth.json").exists()


# ── r1-5 ──────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def profile_state_spies(monkeypatch):
    events = []
    monkeypatch.setattr(profiles, "remove_wrapper_script", lambda name: events.append(("remove-wrapper", name)))
    monkeypatch.setattr(profiles, "create_wrapper_script", lambda name: events.append(("create-wrapper", name)) or Path("wrapper-stub"))
    monkeypatch.setattr(profiles, "check_alias_collision", lambda name: None)
    import hermes_cli.profile_identity as identity
    monkeypatch.setattr(identity, "_migrate_profile_identity",
                        lambda old, new, live: events.append(("identity", old, new)) or True)
    return events


def _profile_state(root, events):
    play = root / "profiles" / "play"
    return {"active": profiles.get_active_profile(), "moved": play.is_dir() and not (root / "profiles" / "work").exists(),
            "previous_names": profiles.read_profile_meta(play).get("previous_names"),
            "wrappers": sorted({e for e in events if e[0] != "identity"}),
            "identity": sorted({e for e in events if e[0] == "identity"}), "ops": registry(root)["ops"]}


BOUNDARIES = ["before-moved", "before-finalizing", "during-finalize", "before-commit"]


@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize("recovered_by", ["job", "recover-op"])
def test_a_recovered_rename_finishes_the_profile_state(codex_home, quiet_profiles, profile_state_spies,
                                                         monkeypatch, boundary, recovered_by):
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    profiles.set_active_profile("work")
    events = profile_state_spies
    with monkeypatch.context() as crash:
        if boundary == "before-moved":
            real_update = pops._update_op
            crash.setattr(pops, "_update_op", lambda op_id, **f: _die() if f.get("phase") == pops.PHASE_MOVED
                          else real_update(op_id, **f))
        elif boundary == "before-finalizing":
            crash.setattr(pops, "apply_payload", _die)
        elif boundary == "during-finalize":
            crash.setattr(profiles, "set_active_profile", lambda name: (_ for _ in ()).throw(OSError(28, "disk full")))
        else:
            crash.setattr(pops, "commit_op", _die)
        with pytest.raises((Killed, ValueError)):
            profiles.rename_profile("work", "play")
    (op_id, op), = registry(root)["ops"].items()
    if boundary == "during-finalize":
        assert op["status"] == "lifecycle-stuck", "a failed finalization is named, never dropped"
    if recovered_by == "job" and op["status"] != "lifecycle-stuck":
        _dispatch()
    else:
        assert pops.recover_op(op_id) == "rolled_forward"
    state = _profile_state(root, events)
    assert state == {"active": "play", "moved": True, "previous_names": ["work"],
                     "wrappers": [("create-wrapper", "play"), ("remove-wrapper", "work")],
                     "identity": [("identity", "work", "play")], "ops": {}}, state
    cid, chain = _only_chain(root)
    assert chain["owner"] == _key(root / "profiles" / "play" / "auth.json") and chain["lifecycle"] is None


def test_an_uninterrupted_rename_reaches_the_same_profile_state(codex_home, quiet_profiles, profile_state_spies):
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    profiles.set_active_profile("work")
    profiles.rename_profile("work", "play")
    state = _profile_state(root, profile_state_spies)
    assert state == {"active": "play", "moved": True, "previous_names": ["work"],
                     "wrappers": [("create-wrapper", "play"), ("remove-wrapper", "work")],
                     "identity": [("identity", "work", "play")], "ops": {}}, state
    assert json.dumps(state)


# ── round 2: finalization results, resolve outcomes ────────────────────────────────────────────

def test_a_finalization_failure_is_retried_and_then_completes(codex_home, quiet_profiles, monkeypatch):
    """A required step (here: the new wrapper cannot be written) reports failure by its RESULT, not
    an exception: the rename stays stuck, claims no alias, and ``recover --op`` completes it once the
    step succeeds."""
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    written = []
    monkeypatch.setattr(profiles, "check_alias_collision", lambda name: None)
    monkeypatch.setattr(profiles, "create_wrapper_script", lambda name: None)  # the helper's failure result
    with pytest.raises(ValueError, match="could not be written"):
        profiles.rename_profile("work", "play")
    (op_id, op), = registry(root)["ops"].items()
    assert op["status"] == "lifecycle-stuck" and op["phase"] == "finalizing"
    assert _refresh_in(monkeypatch, root / "profiles" / "play"), "credentials are usable while finalizing"
    target = profiles._wrapper_path("play")

    def create(name):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("hermes -p play\n", encoding="utf-8")
        written.append(name)
        return target

    monkeypatch.setattr(profiles, "create_wrapper_script", create)
    assert pops.recover_op(op_id) == "rolled_forward"
    assert written == ["play"] and not registry(root)["ops"]


def test_a_colliding_alias_name_is_accepted_and_reported(codex_home, quiet_profiles, monkeypatch, capsys):
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    monkeypatch.setattr(profiles, "check_alias_collision", lambda name: "'play' conflicts with an existing command")
    profiles.rename_profile("work", "play")
    assert not registry(root)["ops"]
    out = capsys.readouterr().out
    assert "Cannot create alias 'play'" in out and "Alias updated" not in out


def test_resolve_reports_a_completed_abandonment(codex_home, quiet_profiles, monkeypatch, capsys):
    from types import SimpleNamespace

    from hermes_cli.auth_commands import auth_recovery_command
    root, endpoint = codex_home
    work, _pair, cid, op_id, epoch = _abandon_to_decision2(root, endpoint, monkeypatch)
    comp = root / "auth.ops" / f"{op_id}.abandon.json"
    raw = comp.read_bytes()
    comp.write_bytes(b"{")
    _dispatch()
    comp.write_bytes(raw)
    auth_recovery_command(SimpleNamespace(recovery_action="resolve", op_id=op_id[:8]))
    out = capsys.readouterr().out
    assert "cancelled as recorded" in out and "recover --op" not in out
    _assert_abandoned(root, work, cid, epoch)
