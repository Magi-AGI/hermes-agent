"""Curator run hygiene: what a pass costs on disk and who gets to run it.

A weekly pass on a 1.9 GB skills tree (97% curator backups + ledger) held the CLI prompt for
six minutes; two CLIs launched 12 s apart both ran it.
"""

import importlib
import threading
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    import tools.skill_usage as usage
    import agent.curator as curator
    import agent.curator_backup as cb
    for m in (usage, cb, curator):
        importlib.reload(m)
    monkeypatch.setattr(curator, "_load_config", lambda: {})
    monkeypatch.setattr(curator, "_run_llm_review", lambda prompt: "llm-stub")
    yield {"home": home, "curator": curator, "cb": cb}
    for t in threading.enumerate():
        if t.name == "curator-review" and t.is_alive():
            t.join(timeout=10.0)


def _claim_held(curator) -> bool:
    """True when another holder has this home's review claim (probe = try to take it)."""
    probe = curator._claim_run()
    if probe is None:
        return True
    probe.release()
    return False


def _snapshots(home: Path):
    d = home / "skills" / ".curator_backups"
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []


def test_prune_only_pass_takes_no_snapshot_but_still_ages_old_ones_out(env, monkeypatch):
    cb, curator, home = env["cb"], env["curator"], env["home"]
    (home / "skills" / "alpha").mkdir()
    (home / "skills" / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\n", encoding="utf-8")
    monkeypatch.setattr(cb, "get_keep", lambda: 5)
    for _ in range(3):
        assert cb.snapshot_skills(reason="old") is not None
    assert len(_snapshots(home)) == 3
    monkeypatch.setattr(cb, "get_keep", lambda: 1)

    curator.run_curator_review(synchronous=True, consolidate=False)
    survivors = _snapshots(home)
    assert len(survivors) == 1, "prune-only pass must apply retention without adding a snapshot"

    curator.run_curator_review(synchronous=True, consolidate=True)
    reasons = [r.get("reason") for r in cb.list_backups()]
    assert "pre-curator-run" in reasons, "consolidation still snapshots first"
    assert len(reasons) <= 2, "and retention still applies (the new snapshot never prunes itself)"


def test_only_one_process_claims_a_due_pass(env, monkeypatch):
    curator, home = env["curator"], env["home"]
    monkeypatch.setattr(curator, "should_run_now", lambda now=None: True)
    started, release = threading.Event(), threading.Event()
    runs = []

    def _slow_review(**kw):
        runs.append(kw)
        started.set()
        release.wait(10)
        kw["on_finished"]()  # like the real run_curator_review, once its (sync here) pass ends
        return {}

    monkeypatch.setattr(curator, "run_curator_review", _slow_review)
    holder = threading.Thread(target=curator.maybe_run_curator, daemon=True)
    holder.start()
    assert started.wait(5)
    try:
        assert curator.maybe_run_curator() is None, "a second launch must not run the pass concurrently"
        assert len(runs) == 1
    finally:
        release.set()
        holder.join(5)
    assert not _claim_held(curator), "claim released after the pass"
    assert curator.maybe_run_curator() is not None, "and the next due pass can claim again"


def test_claim_is_held_until_an_async_review_finishes(env, monkeypatch):
    """Review (Codex): the claim used to be released as soon as the review STARTED its async fork,
    so the next scheduled tick could overlap a still-running review."""
    curator = env["curator"]
    monkeypatch.setattr(curator, "should_run_now", lambda now=None: True)
    release = threading.Event()
    finishers = []

    def _async_review(**kw):
        def _fork():
            release.wait(10)
            kw["on_finished"]()
        t = threading.Thread(target=_fork, daemon=True)
        finishers.append(t)
        t.start()
        return {"started": True}

    monkeypatch.setattr(curator, "run_curator_review", _async_review)
    assert curator.maybe_run_curator() == {"started": True}
    assert _claim_held(curator), "claim held while the review's fork still runs"
    assert curator.maybe_run_curator() is None, "no second review while the first is running"
    release.set()
    finishers[0].join(5)
    assert not _claim_held(curator)


def test_claim_is_released_when_the_review_fails_to_start(env, monkeypatch):
    curator = env["curator"]
    monkeypatch.setattr(curator, "should_run_now", lambda now=None: True)

    def _boom(**_kw):
        raise RuntimeError("could not start")

    monkeypatch.setattr(curator, "run_curator_review", _boom)
    assert curator.maybe_run_curator() is None
    assert not _claim_held(curator)


def test_a_failed_state_lock_saves_nothing(env, monkeypatch):
    """Review (Codex): the lock fails closed; an unlocked read-modify-write is never saved."""
    import contextlib
    curator = env["curator"]
    curator.update_state(lambda s: s.__setitem__("paused", False))
    before = curator._state_file().read_text(encoding="utf-8")

    @contextlib.contextmanager
    def _no_lock():
        raise curator.CuratorStateLockError("simulated")
        yield  # pragma: no cover

    monkeypatch.setattr(curator, "_state_lock", _no_lock)
    with pytest.raises(curator.CuratorStateLockError):
        curator.update_state(lambda s: s.__setitem__("paused", True))
    assert curator._state_file().read_text(encoding="utf-8") == before


def test_update_notice_keeps_a_flag_set_after_it_loaded_the_state(env, monkeypatch):
    """Review (Codex): the once-per-run notice saved its whole earlier snapshot, erasing a
    consolidation flag a background review set in between."""
    curator = env["curator"]
    from hermes_cli import update_cmd_maint
    curator.update_state(lambda s: s.update(last_run_at="2026-09-20T00:00:00+00:00",
                                            last_run_summary="auto: one\nold -> new"))
    real_load = curator.load_state
    flagged = []

    def _load_then_flag():
        state = real_load()
        if not flagged:
            flagged.append(True)
            curator.flag_consolidation_recommended("late-skill", size_bytes=45_000, ref_count=1)
        return state

    monkeypatch.setattr(curator, "load_state", _load_then_flag)
    update_cmd_maint._print_curator_recent_run_notice()
    monkeypatch.setattr(curator, "load_state", real_load)

    state = curator.load_state()
    assert state["last_run_summary_shown_at"] == "2026-09-20T00:00:00+00:00"
    assert "late-skill" in (state.get("_ceiling_flags") or {})


def test_claim_contention_admits_exactly_one_holder(env):
    """Review (Codex): age-based takeover of an O_EXCL claim let two contenders both "take over"
    one expired claim. The claim is now an OS lock held for the review's lifetime."""
    curator = env["curator"]
    first = curator._claim_run()
    assert first is not None
    try:
        assert curator._claim_run() is None, "a second contender must be refused while held"
    finally:
        first.release()
    second = curator._claim_run()
    assert second is not None, "released claims are reacquirable"
    second.release()
    second.release()  # idempotent


def test_a_crashed_holders_claim_is_released_by_the_os(env, tmp_path):
    """No staleness heuristic: a holder that dies loses its lock, and a live holder in another
    process blocks this one."""
    import subprocess
    import sys
    import textwrap
    curator = env["curator"]
    script = textwrap.dedent("""
        import sys, time
        from agent import curator
        claim = curator._claim_run()
        print("held" if claim else "refused", flush=True)
        if sys.argv[1] == "hold":
            sys.stdin.readline()
        # exit WITHOUT release: the OS must drop the lock
    """)
    repo = Path(__file__).resolve().parents[2]
    holder = subprocess.Popen([sys.executable, "-c", script, "hold"], cwd=repo, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert curator._claim_run() is None, "a live holder in another process blocks this one"
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(20)
    claim = curator._claim_run()
    assert claim is not None, "the dead holder's lock was released by the OS"
    claim.release()


def test_a_claim_that_cannot_be_created_refuses_admission(env, monkeypatch):
    curator = env["curator"]
    blocker = env["home"] / "skills" / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(curator, "_run_claim_path", lambda: blocker / "curator-run")
    assert curator._claim_run() is None
    monkeypatch.setattr(curator, "should_run_now", lambda now=None: True)
    started = []
    monkeypatch.setattr(curator, "run_curator_review", lambda **kw: started.append(kw) or {})
    assert curator.maybe_run_curator() is None and started == []


def test_review_worker_keeps_the_profile_context_that_started_it(env, tmp_path):
    """Review (Codex): the async review thread and the claim ran in the LAUNCH profile's context,
    so a review started for profile B wrote A's state and released A's claim. Uses a context
    override (not HERMES_HOME) for B, as the gateway's profile-scoped tick does."""
    import threading as _threading
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    curator = env["curator"]
    home_a = env["home"]
    home_b = tmp_path / "profile-b"
    (home_b / "skills").mkdir(parents=True)

    token = set_hermes_home_override(home_b)
    try:
        claim = curator._claim_run()
        assert claim is not None and home_b in claim.path.parents
        curator.run_curator_review(consolidate=False, on_finished=claim.release)
    finally:
        reset_hermes_home_override(token)
    for t in _threading.enumerate():
        if t.name == "curator-review" and t.is_alive():
            t.join(timeout=10.0)

    token = set_hermes_home_override(home_b)
    try:
        state_b = curator.load_state()
        assert not _claim_held(curator), "B's claim released by the review's completion"
    finally:
        reset_hermes_home_override(token)
    assert state_b.get("last_run_duration_seconds") is not None, "the fork's final write landed in B"
    assert curator.load_state().get("last_run_duration_seconds") is None, "nothing written to A"
    assert not (home_a / "skills" / ".curator_state").exists()


def test_manual_run_refuses_while_a_review_is_running(env, monkeypatch, capsys):
    """Review (Codex): an explicit `hermes curator run` bypasses the idle/interval gates, not the
    per-home exclusion."""
    from types import SimpleNamespace
    from hermes_cli import curator as curator_cli
    curator = env["curator"]
    monkeypatch.setattr(curator, "is_enabled", lambda: True)
    held = curator._claim_run()
    try:
        assert curator_cli._cmd_run(SimpleNamespace(dry_run=True)) == 1
        assert "already running" in capsys.readouterr().out
    finally:
        held.release()
