"""Recovery of a profile move through the REAL dispatcher and lock helpers (design rev 9.1 RULE-P4,
RULE-G5, §10 B4 notes, §13 r8-2 / r9-4 / r7-1 / r8-3).

- abandonment with a crash at every step, split by where it lands: BEFORE DECISION-2 the unreferenced
  compensation payload is archived and the original op, its reservations and every epoch are
  unchanged (still forward, still stuck); AFTER DECISION-2 ordinary recovery completes APPLY-2 /
  COMMIT-2 with the RECORDED epochs, never allocating new ones;
- negative continuation tokens: a stale ``forward`` token after DECISION-2 is refused, any token after
  the entry is removed is refused; recovery takes store locks inside its own reservation;
- a writer inside a profile never deadlocks against a starting move (the mover waits on the lifecycle
  lock first); a lock-taking reader arriving while the move is decided is held, then refused
  transiently, and a stale caller after the move never recreates the old directory;
- a competing sign-in between an op's PAYLOAD and its DECISION waits for the migration lock and never
  lands a login in the moved profile.
A "crash" is a ``SystemExit`` raised at the boundary: nothing in these paths catches it, so what is
left on disk is exactly what a process death leaves (real-process kills: ``scripts/bench_codex_authority.py
kills``). Fake credentials only.
"""

import threading
import time

import pytest

import hermes_cli.auth as auth
from hermes_cli import auth_codex_profile_ops as pops
from hermes_cli import profiles
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, read_json, registry, write_store)
from tests.hermes_cli.test_codex_profile_lifecycle import (  # noqa: F401
    _dispatch, _failing_move, _key, _only_chain, _refresh_in, _work_with_login, quiet_profiles)


class Killed(SystemExit):
    pass


def _stuck_rename(root, endpoint, monkeypatch):
    work, pair = _work_with_login(root, endpoint)
    cid, before = _only_chain(root)
    with monkeypatch.context() as m:
        _failing_move(m)
        with pytest.raises(ValueError):
            profiles.rename_profile("work", "play")
    (op_id, op), = registry(root)["ops"].items()
    assert op["status"] == "lifecycle-stuck"
    return work, pair, cid, before, op_id


def test_abandon_crash_before_decision2_leaves_the_op_unchanged(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, pair, cid, _before, op_id = _stuck_rename(root, endpoint, monkeypatch)
    snapshot = registry(root)
    with monkeypatch.context() as crash:
        crash.setattr(pops, "write_registry", lambda data: (_ for _ in ()).throw(Killed("before DECISION-2")))
        with pytest.raises(Killed):
            pops.recover_op(op_id, abandon=True)
    assert (root / "auth.ops" / f"{op_id}.abandon.json").exists(), "PAYLOAD-2 was written"
    assert registry(root) == snapshot, "nothing decided yet"
    for _ in range(3):
        _dispatch()  # the job alone: the stuck op is skipped, the orphan compensation archived
    after = registry(root)
    assert after == snapshot, "original op, reservations and every epoch preserved"
    assert after["ops"][op_id]["direction"] == "forward" and after["ops"][op_id]["status"] == "lifecycle-stuck"
    assert not (root / "auth.ops" / f"{op_id}.abandon.json").exists()
    assert list((root / "auth.ops" / "archive").glob(f"{op_id}.abandon.abandoned-compensation.*.json"))
    assert (root / "auth.ops" / f"{op_id}.json").exists(), "the original payload is kept"
    # The explicit retry writes a NEW compensation and completes.
    assert pops.recover_op(op_id, abandon=True) == "abandoned"
    chain = registry(root)["chains"][cid]
    assert chain["owner"] == _key(work / "auth.json") and chain["lifecycle"] is None
    assert chain["epoch"] == snapshot["chains"][cid]["epoch"] + 1
    assert _refresh_in(monkeypatch, work) and endpoint.calls == [pair[1]]


@pytest.mark.parametrize("boundary", ["apply-2", "commit-2"])
@pytest.mark.parametrize("recovered_by", ["job", "request"])
def test_abandon_crash_after_decision2_completes_with_recorded_epochs(codex_home, quiet_profiles, monkeypatch,
                                                                      boundary, recovered_by):
    root, endpoint = codex_home
    work, pair, cid, _before, op_id = _stuck_rename(root, endpoint, monkeypatch)
    with monkeypatch.context() as crash:
        name = "apply_payload" if boundary == "apply-2" else "commit_op"
        crash.setattr(pops, name, lambda *a, **k: (_ for _ in ()).throw(Killed(boundary)))
        with pytest.raises(Killed):
            pops.recover_op(op_id, abandon=True)
    decided = registry(root)
    op = decided["ops"][op_id]
    assert op["direction"] == "abandon" and op["status"] == "running"
    recorded = op["abandon_epochs"]
    assert decided["chains"][cid]["epoch"] == recorded[cid]
    # Negative continuation (RULE-G5): the old forward token is refused now.
    from hermes_cli.auth_codex_lifecycle import continuation
    from hermes_cli.auth_codex_registry import CodexAuthorityRefused, migration_lock
    with migration_lock(), pytest.raises(CodexAuthorityRefused):
        with continuation(op_id, "forward"):
            pass
    if recovered_by == "job":
        _dispatch()
    else:
        assert _refresh_in(monkeypatch, work)
    reg = registry(root)
    assert not reg["ops"], "abandonment completed"
    chain = reg["chains"][cid]
    assert chain["epoch"] == recorded[cid], "recovery never allocates new epochs"
    assert chain["owner"] == _key(work / "auth.json") and chain["lifecycle"] is None
    assert read_json(work / "auth.recovery" / f"{cid}.json")["epoch"] == recorded[cid]
    with migration_lock(), pytest.raises(CodexAuthorityRefused):
        with continuation(op_id, "abandon"):
            pass  # any token after the entry is removed
    if recovered_by == "job":
        assert _refresh_in(monkeypatch, work)
    assert endpoint.calls == [pair[1]]


def test_a_continuation_token_is_refused_after_commit(codex_home, quiet_profiles):
    from hermes_cli.auth_codex_lifecycle import continuation
    from hermes_cli.auth_codex_registry import CodexAuthorityRefused, migration_lock
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    seen = {}
    real = pops._finish_forward

    def spy(op_id, body):
        seen["op"] = op_id
        real(op_id, body)

    pops._finish_forward = spy
    try:
        profiles.rename_profile("work", "play")
    finally:
        pops._finish_forward = real
    with migration_lock(), pytest.raises(CodexAuthorityRefused):
        with continuation(seen["op"], "forward"):
            pass


def test_recovery_takes_store_locks_inside_its_own_moving_reservation(codex_home, quiet_profiles, monkeypatch):
    """The mover died right after the rename, before phase ``moved``: the op is in ``moving`` (a
    blocking phase for everybody else) and recovery must still lock the store to APPLY — with its
    continuation token (G5); an ordinary caller is refused meanwhile."""
    from hermes_cli.auth_codex_registry import migration_lock
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    real_update = pops._update_op
    # Simulate: phase "moving" recorded, directory renamed, death before "moved".
    with monkeypatch.context() as crash:
        crash.setattr(pops, "_update_op", lambda op_id, **f: (_ for _ in ()).throw(Killed("x"))
                      if f.get("phase") == pops.PHASE_MOVED else real_update(op_id, **f))
        crash.setattr(pops, "_abandon_unmoved", lambda *a: None)
        with pytest.raises(Killed):
            profiles.rename_profile("work", "play")
    (op_id, op), = registry(root)["ops"].items()
    play = root / "profiles" / "play"
    assert op["phase"] == "moving" and play.is_dir()
    with pytest.raises(AuthError) as err:  # an ordinary caller: the store is reserved (transient)
        with auth._auth_store_lock(target_path=play / "auth.json"):
            pass
    assert err.value.code == "profile_being_moved"
    with migration_lock():
        assert pops.recover_lifecycle_op(op_id, registry(root)["ops"][op_id],
                                         read_json(root / "auth.ops" / f"{op_id}.json")) == "rolled_forward"
    assert not registry(root)["ops"]
    assert _refresh_in(monkeypatch, play)


def test_writer_waiting_for_the_store_lock_does_not_deadlock_a_starting_move(codex_home, quiet_profiles, monkeypatch):
    """r7-1: a writer holds the lifecycle lock and waits for its store lock (held by a third party)
    while a rename starts: the mover waits on the lifecycle lock first, so the three finish in order."""
    root, endpoint = codex_home
    work, _pair = _work_with_login(root, endpoint)
    store = work / "auth.json"
    from hermes_cli.auth_codex_lifecycle import lifecycle_locks
    order, errors = [], []
    third_party_has_store = threading.Event()
    release_store = threading.Event()

    def third_party():
        # Holds only the kernel store lock (another process's view of it: same file, other handle).
        from hermes_cli.auth import _file_lock
        from hermes_cli.auth_credential_io import canonical_path
        with _file_lock(canonical_path(store).with_suffix(".lock"), threading.local(), 30, "t", create_parent=False):
            third_party_has_store.set()
            release_store.wait(10)
        order.append("third-party-released")

    def writer():
        try:
            with lifecycle_locks(["work"], 30):
                order.append("writer-has-lifecycle")
                with auth._auth_store_lock(target_path=store, timeout_seconds=30):
                    order.append("writer-has-store")
        except Exception as exc:  # pragma: no cover - reported
            errors.append(exc)

    t3 = threading.Thread(target=third_party)
    t3.start()
    third_party_has_store.wait(10)
    tw = threading.Thread(target=writer)
    tw.start()
    deadline = time.monotonic() + 10
    while "writer-has-lifecycle" not in order and time.monotonic() < deadline:
        time.sleep(0.01)
    mover_done = threading.Event()

    def mover():
        try:
            profiles.rename_profile("work", "play")
            order.append("moved")
        except Exception as exc:  # pragma: no cover - reported
            errors.append(exc)
        mover_done.set()

    tm = threading.Thread(target=mover)
    tm.start()
    time.sleep(0.5)
    assert "moved" not in order, "the mover must wait for the writer's lifecycle lock"
    release_store.set()
    for t in (t3, tw, tm):
        t.join(30)
    assert not errors and mover_done.is_set()
    assert order.index("writer-has-store") < order.index("moved")


def test_reader_during_a_decided_move_is_held_then_refused_and_recreates_nothing(codex_home, quiet_profiles,
                                                                                    monkeypatch):
    """r8-3: a lock-taking reader arriving after the decision is held at the lifecycle lock; once the
    directory moved it gets the transient error, and it never recreates the old directory. A
    read-only diagnostic creates no lock files."""
    root, endpoint = codex_home
    work, _pair = _work_with_login(root, endpoint)
    decided, go = threading.Event(), threading.Event()
    real_move = pops.ProfileRename.move

    def paused_move(self, fn):
        decided.set()
        go.wait(10)
        return real_move(self, fn)

    pops.ProfileRename.move = paused_move
    outcome = {}

    def reader():
        decided.wait(10)
        try:
            with auth._auth_store_lock(target_path=work / "auth.json", timeout_seconds=30):
                outcome["reader"] = "entered"
        except AuthError as exc:
            outcome["reader"] = exc.code

    try:
        tr = threading.Thread(target=reader)
        tr.start()
        tm = threading.Thread(target=lambda: profiles.rename_profile("work", "play"))
        tm.start()
        decided.wait(10)
        time.sleep(0.3)
        assert "reader" not in outcome, "the reader was not held at the lifecycle lock"
        go.set()
        tm.join(30)
        tr.join(30)
    finally:
        pops.ProfileRename.move = real_move
    assert outcome["reader"] in ("profile_store_missing", "profile_being_moved")
    assert not work.exists(), "the stale reader recreated the old profile directory"
    play = root / "profiles" / "play"
    (play / "auth.lock").unlink(missing_ok=True)
    locks_before = sorted(p.name for p in (root / "profiles" / ".locks").iterdir())
    monkeypatch.setenv("HERMES_HOME", str(play))
    assert auth.resolve_codex_runtime_credentials(read_only=True)["api_key"]
    from hermes_cli.auth_codex_recovery import list_recovery
    list_recovery()
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert sorted(p.name for p in (root / "profiles" / ".locks").iterdir()) == locks_before, "a diagnostic took a lock"
    assert not (play / "auth.lock").exists(), "a read-only diagnostic created a lock file"


def test_a_sign_in_between_payload_and_decision_waits_and_never_lands_in_the_moved_profile(
        codex_home, quiet_profiles, monkeypatch):
    """B4: competing admissions between an op's PAYLOAD and its DECISION. The sign-in needs the
    migration lock the rename holds: it waits, then finds the profile moved and writes nothing."""
    from hermes_cli import auth_codex_ops as ops
    root, endpoint = codex_home
    work, _pair = _work_with_login(root, endpoint)
    competing = {}
    real_write = ops.write_registry
    fresh = endpoint.issue()

    def sign_in_in_work():
        try:
            from hermes_cli.auth_codex_authority import sign_in
            from hermes_cli.auth_codex_registry import authority_scope
            with authority_scope(work):
                sign_in(*fresh)
            competing["outcome"] = "signed-in"
        except Exception as exc:
            competing["outcome"] = type(exc).__name__

    def write_registry(data):
        if any(op.get("kind") == pops.RENAME for op in data["ops"].values()) and "thread" not in competing:
            competing["thread"] = t = threading.Thread(target=sign_in_in_work)
            t.start()
            time.sleep(0.5)
            competing["waiting"] = "outcome" not in competing
        return real_write(data)

    monkeypatch.setattr(ops, "write_registry", write_registry)
    profiles.rename_profile("work", "play")
    competing["thread"].join(30)
    assert competing["waiting"], "the sign-in ran between the payload and the decision"
    assert competing["outcome"] != "signed-in"
    assert not work.exists()
    reg = registry(root)
    assert all(c["owner"] != _key(work / "auth.json") for c in reg["chains"].values())
    assert fresh[1] not in (root / "profiles" / "play" / "auth.json").read_text(encoding="utf-8")
