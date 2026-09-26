"""A claim waits out a busy active-session registry lock instead of refusing at Windows' 10 s cliff.

Found by the shared-backend concurrency harness (2026-09-25): 12 windows starting their first turn at
once on one backend. ``msvcrt.LK_LOCK`` retries ten times a second apart and then raises; the holder,
starved of the GIL by concurrent agent builds, kept the registry lock past that, and every waiting
claim failed CLOSED as "Hermes could not safely reserve this session" (4090) in the same millisecond.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

HOLD_S = 12.0  # past the old 10 x 1 s LK_LOCK retry budget

_HOLDER = """
import msvcrt, sys, time
with open(sys.argv[1], "a+b") as fh:
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
"""


def _claim(home, session_id: str):
    from hermes_cli.active_sessions import try_acquire_active_session

    return try_acquire_active_session(session_id=session_id, surface="desktop", config={},
                                      registry_home=home, metadata={"live_session_id": session_id},
                                      track_liveness=True)


@pytest.mark.windows_only
def test_a_claim_outwaits_another_process_holding_the_registry_lock(tmp_path):
    from hermes_cli.active_sessions import _lease_paths

    home = tmp_path / "home"
    _state, lock_path = _lease_paths(registry_home=home)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock_path), str(HOLD_S)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        lease, refusal = _claim(home, "s-cross-process")
        waited = time.monotonic() - started
    finally:
        holder.wait(timeout=60)

    assert refusal is None and lease is not None, f"claim refused after {waited:.1f}s: {refusal}"
    assert waited >= HOLD_S - 2.0


def test_a_dozen_claims_queue_behind_a_long_holder_and_all_succeed(tmp_path):
    from hermes_cli.active_sessions import _FileLock, _lease_paths

    home = tmp_path / "home"
    _state, lock_path = _lease_paths(registry_home=home)
    holding, results = threading.Event(), {}

    def hold():
        with _FileLock(lock_path):
            holding.set()
            time.sleep(HOLD_S)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=10)
    claimants = [threading.Thread(target=lambda i=i: results.__setitem__(i, _claim(home, f"s-{i}")))
                 for i in range(12)]
    for t in claimants:
        t.start()
    for t in (holder, *claimants):
        t.join(timeout=60)

    refused = {i: r[1] for i, r in results.items() if r[1] is not None}
    assert len(results) == 12, f"only {len(results)} of 12 claims returned behind a busy lock"
    assert not refused, f"claims refused behind a busy lock: {refused}"


_HOLD_FOREVER = """
import os, sys, time
with open(sys.argv[1], "a+b") as fh:
    fh.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    print("held", flush=True)
    time.sleep(60)
"""


def test_a_holder_that_never_lets_go_makes_waiters_fail_closed_at_the_budget(tmp_path, monkeypatch):
    """Same contract on every OS: the claim fails CLOSED at the budget, never blocks forever
    (POSIX flock(LOCK_EX) used to wait unbounded) and never gives up early."""
    from hermes_cli import active_sessions

    home = tmp_path / "home"
    _state, lock_path = active_sessions._lease_paths(registry_home=home)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(active_sessions, "_LOCK_WAIT_S", 2.0)
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_FOREVER, str(lock_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="lock unavailable"):
            with active_sessions._FileLock(lock_path):
                pass
        waited = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait(timeout=10)

    assert 1.5 <= waited < 10.0, f"gave up after {waited:.1f}s against a 2 s budget"
