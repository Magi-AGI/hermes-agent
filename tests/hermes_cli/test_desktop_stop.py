"""``hermes desktop stop`` (gate H3's escape hatch): fail-closed identity, exact-PID-only kills.

agy R4-1/R4-2/R4-3/R4-4 contract: a PID is signalled only while pinned to the ledger's
create_time (unverifiable identity is reported and skipped, never killed); no tree kills — the
targets are the mains, their serve/dashboard backends, and helper rows those backends registered,
each an exact verified PID; a desktop update holding the shared lock makes the command refuse;
terminate -> grace -> kill outcomes are verified and anything skipped or surviving exits 1; with
no owned rows the command names the uncovered modes instead of pretending success. The gateway,
a no-spawner dashboard (the relay), and anything outside the ledger are never touched.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from hermes_cli.main_desktop_stop import cmd_desktop_stop, desktop_stop_plan


def _entry(pid, purpose, spawner=None, create=1.0, spawner_create=2.0, profile=""):
    return {"pid": pid, "create_time": create, "purpose": purpose,
            "spawner_pid": spawner, "spawner_create": spawner_create if spawner else None,
            "profile": profile}


class _OpenLock:
    def __init__(self):
        self.released = False

    def acquire(self):
        return True

    def release(self):
        self.released = True


class _HeldLock(_OpenLock):
    holder = None

    def acquire(self):
        return False


def test_plan_selects_backends_their_helpers_and_separates_unverified_mains():
    entries = [
        _entry(100, "serve", spawner=50),                        # desktop-owned
        _entry(101, "dashboard", spawner=50),                    # same main
        _entry(102, "dashboard", spawner=None),                  # the relay: no spawner — excluded
        _entry(103, "gateway", spawner=100),                     # gateway: never, even as a backend child
        _entry(104, "serve", spawner=60, spawner_create=None),   # main 60 has no recorded start time
        _entry(105, "mcp-helper", spawner=100),                  # helper registered by backend 100
        _entry(106, "mcp-helper", spawner=999),                  # helper of something not ours — excluded
    ]
    plan = desktop_stop_plan(entries)
    assert plan.mains == [(50, 2.0)]
    assert plan.unverified_mains == [60], "a main without spawner_create must never be killable"
    assert sorted(e["pid"] for e in plan.backends) == [100, 101, 104]
    assert [e["pid"] for e in plan.helpers] == [105]


def test_update_lock_held_refuses_and_kills_nothing():
    out = io.StringIO()
    code = cmd_desktop_stop(
        entries_fn=lambda: [_entry(100, "serve", spawner=50)],
        stop_pinned=lambda *a, **k: (_ for _ in ()).throw(AssertionError("killed under an update")),
        update_lock_factory=_HeldLock,
        grace_seconds=0.0, sleep=lambda s: None, out=out)
    assert code == 1
    assert "update is in progress" in out.getvalue()


def test_unverifiable_identity_is_skipped_and_fails_the_exit_code():
    out = io.StringIO()
    outcomes = {50: "unverifiable", 100: "stopped"}
    killed = []

    def stop_pinned(pid, create, **kw):
        killed.append(pid)
        return outcomes[pid]

    calls = {"n": 0}

    def entries_fn():
        calls["n"] += 1
        # The backend self-exits once the main is gone; later ledger reads no longer show it.
        return [_entry(100, "serve", spawner=50)] if calls["n"] == 1 else []

    code = cmd_desktop_stop(
        entries_fn=entries_fn, stop_pinned=stop_pinned, update_lock_factory=_OpenLock,
        grace_seconds=1.0, sleep=lambda s: None, out=out)
    assert code == 1, "an unverifiable main must fail the exit code"
    assert killed == [50], "the self-exited backend must not be signalled"
    assert "unverifiable" in out.getvalue()


# (S8's test_main_without_recorded_start_time_is_reported_not_killed asserted the backends of a
# skipped main are still stopped; agy R5-4 superseded that contract - killing a backend under a
# live main only triggers a respawn - and the two round-5 linkage tests below pin the
# replacement behavior for both outcomes.)


def test_backends_that_self_exit_in_the_grace_window_are_not_signalled():
    out = io.StringIO()
    killed = []
    calls = {"n": 0}

    def entries_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            return [_entry(100, "serve", spawner=50), _entry(101, "dashboard", spawner=50)]
        return [_entry(101, "dashboard", spawner=50)]  # 100 self-exited after the main died

    code = cmd_desktop_stop(
        entries_fn=entries_fn,
        stop_pinned=lambda pid, create, **kw: killed.append(pid) or "stopped",
        update_lock_factory=_OpenLock, grace_seconds=1.0, sleep=lambda s: None, out=out)
    assert code == 0
    assert killed[0] == 50, "the desktop main must die before its backends"
    assert 100 not in killed, "a backend that self-exited in the grace window was still signalled"
    assert 101 in killed


def test_survivor_after_kill_is_named_and_fails_the_exit_code():
    out = io.StringIO()
    code = cmd_desktop_stop(
        entries_fn=lambda: [_entry(100, "serve", spawner=50)],
        stop_pinned=lambda pid, create, **kw: {50: "stopped", 100: "survived"}[pid],
        update_lock_factory=_OpenLock, grace_seconds=0.0, sleep=lambda s: None, out=out)
    assert code == 1
    assert "survived" in out.getvalue()


def test_no_owned_rows_names_the_uncovered_modes():
    out = io.StringIO()
    code = cmd_desktop_stop(
        entries_fn=lambda: [_entry(102, "dashboard", spawner=None), _entry(103, "gateway", spawner=50)],
        stop_pinned=lambda *a, **k: (_ for _ in ()).throw(AssertionError("killed")),
        update_lock_factory=_OpenLock, grace_seconds=0.0, sleep=lambda s: None, out=out)
    assert code == 1, "stopping nothing is not success (agy R5-6)"
    text = out.getvalue()
    assert "No desktop-owned" in text
    assert "attach-first" in text, "the uncovered wedge modes must be named, not implied"


def test_parent_linkage_verifies_a_main_without_recorded_start_time():
    """agy R5-4: POSIX spawns record no spawner_create. A live backend whose CURRENT parent pid
    equals the recorded spawner IS positive identity for the main - the linkage is live, so no
    recycled PID can satisfy it. The main must then be stoppable."""
    out = io.StringIO()
    killed = []
    code = cmd_desktop_stop(
        entries_fn=lambda: [_entry(104, "serve", spawner=60, spawner_create=None)],
        stop_pinned=lambda pid, create, **kw: killed.append((pid, create)) or "stopped",
        parent_linkage=lambda main_pid, backend_rows: 777.5 if main_pid == 60 else None,
        update_lock_factory=_OpenLock, grace_seconds=0.0, sleep=lambda s: None, out=out)
    assert (60, 777.5) in killed, (
        "the main must be stopped PINNED to the create_time captured at linkage time - a "
        "kill-time self-comparison would accept a recycled PID (agy round 6, R5-4a)")
    assert killed[0][0] == 60, "the main still dies before its backends"
    assert code == 0


def test_unverifiable_main_skips_its_backends_to_avoid_respawn_churn():
    """agy R5-4: killing a backend under a main we cannot stop just makes the main respawn it.
    When a main stays unverifiable, its backends are reported and left alone."""
    out = io.StringIO()
    killed = []
    code = cmd_desktop_stop(
        entries_fn=lambda: [_entry(104, "serve", spawner=60, spawner_create=None)],
        stop_pinned=lambda pid, create, **kw: killed.append(pid) or "stopped",
        parent_linkage=lambda main_pid, backend_rows: None,
        update_lock_factory=_OpenLock, grace_seconds=0.0, sleep=lambda s: None, out=out)
    assert code == 1
    assert killed == [], "backends of an unstoppable main must not be killed (respawn churn)"
    assert "respawn" in out.getvalue()


def test_update_lock_defaults_to_the_machine_root_marker(monkeypatch):
    """agy R5-5: the desktop updater writes ~/.hermes/.hermes-update-in-progress at the MACHINE
    root; a profile-scoped marker is one the updater never holds. The default lock must point
    at the root marker."""
    import hermes_cli.main_desktop_stop as mod
    seen = {}

    class _Lock:
        def __init__(self, *, path=None):
            seen["path"] = path

        def acquire(self):
            return False

        def release(self):
            pass

        holder = None

    monkeypatch.setattr("hermes_cli.update_lock.UpdateLock", _Lock)
    out = io.StringIO()
    mod.cmd_desktop_stop(entries_fn=lambda: [], grace_seconds=0.0, sleep=lambda s: None, out=out)
    from hermes_constants import get_default_hermes_root
    from hermes_cli.update_lock import MARKER_NAME
    assert seen["path"] == get_default_hermes_root() / MARKER_NAME, (
        "the lock must target the root marker the updater actually holds")


def test_no_owned_rows_exits_nonzero():
    """agy R5-6: 'I stopped nothing' must not read as shell success - the command exists to
    stop a wedged desktop, and scripts branch on the exit code."""
    out = io.StringIO()
    code = cmd_desktop_stop(
        entries_fn=lambda: [],
        stop_pinned=lambda *a, **k: (_ for _ in ()).throw(AssertionError("killed")),
        update_lock_factory=_OpenLock, grace_seconds=0.0, sleep=lambda s: None, out=out)
    assert code == 1
    assert "No desktop-owned" in out.getvalue()


def test_stop_rejects_build_and_launch_flags():
    """agy R5-8: 'hermes desktop stop --build-only' (or --source / --setup-tcc-identity) is a
    contradiction; silently ignoring the flag does whichever half the user did not mean."""
    import argparse
    import pytest

    from hermes_cli.main_desktop import cmd_gui

    args = argparse.Namespace(action="stop", source=True, build_only=False, fake_boot=False,
                              ignore_existing=False, hermes_root=None, cwd=None, skip_build=False,
                              local=False, force_build=False, setup_tcc_identity=False,
                              identity="x")
    with pytest.raises(SystemExit) as exc:
        cmd_gui(args)
    assert exc.value.code == 2


def test_helpers_of_skipped_backends_are_skipped_too():
    """agy round 6 (R5-4b): a helper (kernel/MCP child) of a backend we left alive must not be
    killed out from under it."""
    out = io.StringIO()
    killed = []
    code = cmd_desktop_stop(
        entries_fn=lambda: [_entry(104, "serve", spawner=60, spawner_create=None),
                            _entry(105, "mcp-helper", spawner=104)],
        stop_pinned=lambda pid, create, **kw: killed.append(pid) or "stopped",
        parent_linkage=lambda main_pid, backend_rows: None,
        update_lock_factory=_OpenLock, grace_seconds=0.0, sleep=lambda s: None, out=out)
    assert code == 1
    assert killed == [], "the live backend's helper was killed out from under it"


def test_parent_linkage_requires_creation_ordering_and_recorded_child_times(monkeypatch):
    """Codex HS-C1: Windows keeps a child's original parent pid after that parent exits, so a
    newcomer on the recycled pid passes a bare ppid check. The spawner must PREDATE the child
    it spawned, and a backend row without a recorded create_time proves nothing (fail closed)."""
    import hermes_cli.main_desktop_stop as mod

    class _Proc:
        def __init__(self, pid):
            self._pid = pid

        def create_time(self):
            return {50: 20.0, 100: 10.0}[self._pid]  # "parent" born AFTER its child: recycled

        def ppid(self):
            return 50

    monkeypatch.setitem(__import__("sys").modules, "psutil",
                        SimpleNamespace(Process=_Proc, NoSuchProcess=Exception,
                                        TimeoutExpired=Exception))
    row = {"pid": 100, "create_time": 10.0, "spawner_pid": 50}
    assert mod._parent_linkage(50, [row]) is None, (
        "a spawner younger than its child is a recycled pid, not the spawner")
    row_no_create = {"pid": 100, "create_time": None, "spawner_pid": 50}
    assert mod._parent_linkage(50, [row_no_create]) is None, (
        "a backend row without a recorded create_time cannot verify anything")


def test_dependents_of_a_main_that_did_not_stop_are_left_alone():
    """Codex HS-C2: a VERIFIED main that survives (or turns unverifiable at kill time) is still
    alive to respawn - its backends and helpers must be skipped exactly like those of an
    unverifiable main."""
    out = io.StringIO()
    killed = []
    code = cmd_desktop_stop(
        entries_fn=lambda: [_entry(100, "serve", spawner=50),
                            _entry(105, "mcp-helper", spawner=100)],
        stop_pinned=lambda pid, create, **kw: killed.append(pid) or "survived",
        update_lock_factory=_OpenLock, grace_seconds=0.0, sleep=lambda s: None, out=out)
    assert code == 1
    assert killed == [50], "dependents of a still-live main were signalled (respawn churn)"
