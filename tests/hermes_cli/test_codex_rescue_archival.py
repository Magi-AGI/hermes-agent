"""Legacy rescue files: archival as untrusted and evidence-based unfencing (design rev 9.1 §7, stage 2).

A rev-0 rescue file fences every candidate chain when it is found (stage 1). Stage 2 settles it:
the fence is lifted ONLY on independent evidence — every rescued pair equals the owner's CURRENT
rows — otherwise it stays until a fresh sign-in; either way the file is archived as untrusted (the
exact bytes read, the original removed only while it still holds them) and never used as a token
source. Fake credentials only.
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, read_json, registry, write_store)

RESCUE = "auth.codex-rotation-rescue.json"


def _rescue(directory, pair):
    (directory / RESCUE).write_text(json.dumps({"entries": {"singleton": {
        "access_token": pair[0], "refresh_token": pair[1], "rescued_at": "2026-09-20T00:00:00Z"}}}), encoding="utf-8")


def _dispatch():
    from hermes_cli.auth_codex_authority import authority_dispatch
    return authority_dispatch()


def _archived(root):
    directory = root / "auth.ops" / "archive"
    return sorted(directory.glob("legacy-rescue.*.untrusted.json")) if directory.is_dir() else []


def test_a_rescue_the_rows_already_hold_is_archived_and_lifts_its_fence(codex_home):
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    _rescue(root, pair)  # the store already holds exactly what the rescue saved
    _dispatch()
    (cid, chain), = registry(root)["chains"].items()
    assert chain["state"] == "active" and chain["fence"] is None and chain["epoch"] == 2
    assert read_json(root / "auth.recovery" / f"{cid}.json")["epoch"] == 2
    assert not (root / RESCUE).exists() and len(_archived(root)) == 1
    record, = registry(root)["legacy_rescues"].values()
    assert record["verdict"] == "stale"
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert endpoint.calls == [pair[1]]


def test_a_rescue_holding_another_pair_stays_fenced_and_is_never_used(codex_home):
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    rescued = endpoint.issue()  # a successor the rows never received
    _rescue(root, rescued)
    _dispatch()
    (_cid, chain), = registry(root)["chains"].items()
    assert chain["state"] == "fenced" and chain["fence"]["reason"] == "legacy-rescue"
    assert not (root / RESCUE).exists() and len(_archived(root)) == 1
    with pytest.raises(AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert endpoint.calls == [], "neither the rows' pair nor the rescued pair was spent"
    assert rescued[1] not in (root / "auth.json").read_text(encoding="utf-8")
    fresh = endpoint.issue()
    auth._save_codex_tokens({"access_token": fresh[0], "refresh_token": fresh[1]})
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert endpoint.calls == [fresh[1]], "a fresh sign-in is the exit"


def test_a_stage1_recorded_rescue_is_settled_later(codex_home, monkeypatch):
    """An install upgraded from stage 1: the rescue was recorded (chain fenced, no verdict) and left
    in place. The next dispatch settles it."""
    from hermes_cli import auth_codex_rescue as rescue_mod
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    _rescue(root, pair)
    with monkeypatch.context() as stage1:
        stage1.setattr(rescue_mod, "settle_legacy_rescues", lambda: None)  # what stage 1 did: nothing
        _dispatch()
    (cid, chain), = registry(root)["chains"].items()
    assert chain["fence"]["reason"] == "legacy-rescue" and (root / RESCUE).exists()
    from hermes_cli.auth_codex_authority import startup_dispatch
    assert startup_dispatch() == "done", "a recorded, unsettled rescue is work for the dispatcher"
    chain = registry(root)["chains"][cid]
    assert chain["state"] == "active" and not (root / RESCUE).exists()


def test_archival_never_removes_a_rescue_that_changed_after_it_was_read(codex_home, monkeypatch):
    from hermes_cli import auth_codex_rescue as rescue_mod
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    _rescue(root, endpoint.issue())
    real = rescue_mod.require_committed

    def rewrite_after_copy(result, what):
        out = real(result, what)
        _rescue(root, ("a-new", "rt-written-meanwhile"))  # another writer replaces the file
        return out

    monkeypatch.setattr(rescue_mod, "require_committed", rewrite_after_copy)
    _dispatch()
    assert (root / RESCUE).exists(), "a file whose bytes were not archived was removed"
    assert "rt-written-meanwhile" in (root / RESCUE).read_text(encoding="utf-8")


def test_a_crash_between_copy_and_unlink_completes_on_the_next_pass(codex_home, monkeypatch):
    from hermes_cli import auth_codex_rescue as rescue_mod
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    _rescue(root, endpoint.issue())

    class Killed(SystemExit):
        pass

    with monkeypatch.context() as crash:
        crash.setattr(rescue_mod, "unlink_retrying", lambda p: (_ for _ in ()).throw(Killed("x")))
        with pytest.raises(Killed):
            from hermes_cli.auth_codex_authority import dispatch_locked
            from hermes_cli.auth_codex_registry import migration_lock
            with migration_lock():
                dispatch_locked()
    assert (root / RESCUE).exists() and len(_archived(root)) == 1
    _dispatch()
    assert not (root / RESCUE).exists() and len(_archived(root)) == 1, "idempotent: one archive copy"


@pytest.mark.require_symlinks
def test_a_rescue_beside_a_symlinked_alias_is_archived(codex_home):
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    work = make_profile(root, "work")
    (work / "auth.json").symlink_to(root / "auth.json")
    _rescue(work, endpoint.issue())
    _dispatch()
    (_cid, chain), = registry(root)["chains"].items()
    assert chain["fence"]["reason"] == "legacy-rescue"
    assert not (work / RESCUE).exists() and len(_archived(root)) == 1


def test_a_dispatch_pass_with_nothing_to_settle_takes_no_store_lock(codex_home, monkeypatch):
    """Settlement runs on EVERY dispatch pass (requests and the job): with no rescue file and no
    fence to lift it must not lock every store (measured: a 24x4 refresh run went from 0 % to
    1.3 % refused while each pass locked all stores)."""
    from hermes_cli import auth_codex_rescue as rescue_mod
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    _dispatch()
    monkeypatch.setattr(rescue_mod, "store_locks", lambda *a, **k: (_ for _ in ()).throw(AssertionError("locked")))
    from hermes_cli.auth_codex_registry import migration_lock
    with migration_lock():
        assert rescue_mod.settle_legacy_rescues() == {"unfenced": [], "archived": []}
