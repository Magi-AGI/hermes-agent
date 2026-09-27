"""``bounded_probe_run`` — deadlock-safe capture for fail-open probes (#87134).

On Windows, ``subprocess.run(..., capture_output=True, timeout=N)`` can hang
FOREVER after its timeout fires: run()'s cleanup kills the direct child and
then joins the pipe reader threads with an unbounded ``communicate()``.  A
descendant (``conhost.exe`` under wmic/powershell, ``git.exe`` under a
launcher shim) holding duplicated pipe handles keeps the pipes from EOF and
the join never returns.  ``hermes update`` wedged exactly there inside
``_scan_gateway_pids`` on machines where the full ``Win32_Process`` scan
exceeds its budget.

``bounded_probe_run`` is the shared, generalized form of the fix that
``bounded_git_probe`` already proved for git probes (#68609 / #66037):
explicit ``communicate(timeout)``, tree-kill on failure, bounded 1s drain,
then abandon.

These tests use REAL subprocesses (no mocks) for the semantics: a mock cannot
reproduce pipe-handle inheritance or timeout behavior.  The POSIX-only
descendant-survival cases live in ``test_git_probe_tree_kill.py`` and now
exercise the same code path through the ``bounded_git_probe`` delegation.
"""

import json
import subprocess
import sys
import threading
import time

import pytest

from hermes_cli._subprocess_compat import bounded_git_probe, bounded_probe_run

_PY = sys.executable


def test_success_returns_completed_process():
    result = bounded_probe_run([_PY, "-c", "print('ok'); import sys; sys.exit(0)"], timeout=30)
    assert result is not None
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_nonzero_exit_is_returned_not_swallowed():
    """Unlike bounded_git_probe, callers see the real returncode — the gateway
    scan branches on ``returncode != 0`` to trip the wmic→powershell fallback."""
    result = bounded_probe_run([_PY, "-c", "print('partial'); import sys; sys.exit(3)"], timeout=30)
    assert result is not None
    assert result.returncode == 3
    assert result.stdout.strip() == "partial"


def test_spawn_failure_returns_none():
    result = bounded_probe_run(["definitely-not-a-real-binary-87134"], timeout=5)
    assert result is None


def test_timeout_returns_none_within_bounded_time():
    """A child that sleeps past the timeout must produce ``None`` promptly —
    timeout + tree-kill + 1s bounded drain, not an unbounded join."""
    start = time.monotonic()
    result = bounded_probe_run(
        [_PY, "-c", "import time; time.sleep(300)"],
        timeout=1.0,
    )
    elapsed = time.monotonic() - start
    assert result is None
    # 1s timeout + tree-kill + 1s drain + slack.  The pre-fix failure mode is
    # an indefinite hang, so any bound proves the property; keep it loose for
    # slow CI runners.
    assert elapsed < 30


def test_decode_errors_configurable():
    """The process scans pass errors='ignore' (wmic emits system code page);
    undecodable bytes must not raise or None out stdout (#17049 class)."""
    result = bounded_probe_run(
        [_PY, "-c", "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfe')"],
        timeout=30,
        errors="ignore",
    )
    assert result is not None
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_stdin_is_devnull_not_inherited():
    """A probe must never block reading the caller's stdin."""
    result = bounded_probe_run(
        [_PY, "-c", "import sys; print(repr(sys.stdin.read()))"],
        timeout=30,
    )
    assert result is not None
    assert result.returncode == 0
    assert result.stdout.strip() == "''"


def test_bounded_git_probe_delegates_same_contract():
    """The historical git-probe wrapper keeps its exact contract on top of
    bounded_probe_run: stripped stdout on rc==0, '' on any failure."""
    assert bounded_git_probe([_PY, "-c", "print('  x  ')"], timeout=30) == "x"
    assert bounded_git_probe([_PY, "-c", "import sys; sys.exit(1)"], timeout=30) == ""
    assert bounded_git_probe(["definitely-not-a-real-binary-87134"], timeout=5) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group check")
def test_posix_child_gets_own_process_group():
    """POSIX spawns use process_group=0 so timeout cleanup can killpg the
    whole tree (same contract bounded_git_probe had)."""
    result = bounded_probe_run(
        [_PY, "-c", "import os; print(os.getpgid(0) == os.getpid())"],
        timeout=30,
    )
    assert result is not None
    assert result.stdout.strip() == "True"


# A sacrificial child runs one git probe with the Nth thread started during it failing: refused
# (start raises) or dying before it runs (a bootstrap MemoryError under commit exhaustion). Every
# thread start goes through both _thread and threading's own binding, so the fault lands the same
# way whichever kind of reader the helper uses. The child reports what it got back and what it
# left behind; a child still blocked when the test's timeout fires is the stranded-caller failure.
_FAULT_CHILD = r'''
import _thread, json, subprocess, sys, threading, time
from hermes_cli import _subprocess_compat as sc

repo, cache, fault, target = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
if cache == "warm":
    sc.noninteractive_git_env()
launched, ran, finished, children = [], set(), set(), []
real_init = subprocess.Popen.__init__

def init(self, *args, **kwargs):
    real_init(self, *args, **kwargs)
    children.append(self)

def failing(real):
    def start(fn, args=(), *rest):
        launched.append(fn)
        n = len(launched)
        if n == target:
            if fault == "refused":
                raise RuntimeError("can't start new thread")
            def dies(*_):
                raise MemoryError("reader died before running")
            return real(dies, ())
        ran.add(n)
        def run(*a):
            try:
                fn(*a)
            finally:
                finished.add(n)
        return real(run, args, *rest)
    return start

subprocess.Popen.__init__ = init
before = set(threading.enumerate())
_thread.start_new_thread = failing(_thread.start_new_thread)
threading._start_new_thread = failing(threading._start_new_thread)
started = time.monotonic()
out = sc.bounded_git_probe(["git", "-C", repo, "status", "--porcelain=v2", "--branch"], timeout=5.0)
seconds = time.monotonic() - started
settle = time.monotonic() + 15
while time.monotonic() < settle and (ran - finished or any(c.poll() is None for c in children)):
    time.sleep(0.05)
print(json.dumps({
    "fired": len(launched) >= target, "out": out, "seconds": seconds,
    "readers_left": sorted(ran - finished),
    "children_left": [c.args for c in children if c.poll() is None],
    "records": sorted(t.name for t in set(threading.enumerate()) - before),
}))
'''


@pytest.mark.windows_only
@pytest.mark.parametrize("cache, fault, target", [
    ("warm", "dies", 1), ("warm", "dies", 2), ("warm", "refused", 1), ("warm", "refused", 2),
    ("cold", "dies", 1), ("cold", "refused", 1),
])
def test_a_pipe_reader_that_cannot_start_leaves_git_probe_bounded(tmp_path, cache, fault, target):
    """A reader thread that is refused or dies in bootstrap must not strand the caller: communicate()'s
    Thread.start() waits for its new thread with no bound. Warm: the probe's own reader fails, so the
    probe fails (""). Cold: the safe.directory read in front of the probe fails, which is best-effort, so
    the probe still answers. Either way the call returns, raises nothing and leaves no reader or child."""
    from pathlib import Path

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    root = str(Path(__file__).resolve().parents[2])
    try:
        child = subprocess.run([_PY, "-c", _FAULT_CHILD, str(repo), cache, fault, str(target)], cwd=root,
                               capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        child = None
    assert child is not None, "bounded_git_probe never returned: the caller is stranded on a reader that never started"
    assert child.returncode == 0, child.stderr  # an exception escaped bounded_git_probe
    row = json.loads(child.stdout.splitlines()[-1])
    assert row["fired"], row
    if cache == "warm":
        assert row["out"] == "", row
    else:
        assert row["out"].startswith("# branch.oid"), row
    assert row["seconds"] < 60, row  # probe 5 s (+ 5 s per cold config read) + tree-kill + 1 s drain
    assert not row["readers_left"] and not row["children_left"] and not row["records"], row


class _GatedPipe:
    """A pipe whose read() returns *text* only once the test opens its gate."""

    def __init__(self, text=""):
        self.text, self.gate, self.closed = text, threading.Event(), False

    def read(self):
        self.gate.wait(30)
        return self.text

    def close(self):
        self.closed = True


def test_cleanup_sees_every_reader_that_started_and_every_one_that_finished(monkeypatch):
    """The post-kill drain reuses the probe's readers, so its view of them must be complete: a
    reader still reading when the next launch is refused is waited for, and readers that already
    finished stay finished for a later drain (not only for the first wait that saw them)."""
    from types import SimpleNamespace

    from hermes_cli import _subprocess_compat as sc

    def proc(out, err):
        return SimpleNamespace(stdout=out, stderr=err, args=["probe"], wait=lambda timeout=None: 0)

    out, err = _GatedPipe("o"), _GatedPipe("e")
    out.gate.set()
    err.gate.set()
    finished = sc._PipeReaders(proc(out, err))
    assert finished.communicate(30) == ("o", "e")
    assert finished.drain(0) is True

    real_start = sc._thread.start_new_thread
    launches = []

    def refuse_second_reader(fn, args):
        if fn is sc._read_pipe:
            launches.append(args)
            if len(launches) == 2:
                raise RuntimeError("can't start new thread")
        return real_start(fn, args)

    monkeypatch.setattr(sc._thread, "start_new_thread", refuse_second_reader)
    out, err = _GatedPipe("o"), _GatedPipe()
    partial = sc._PipeReaders(proc(out, err))
    with pytest.raises(RuntimeError):
        partial.communicate(30)
    assert err.closed and not out.closed  # no reader holds the refused pipe; the live one keeps its own
    assert partial.drain(0) is False  # the first reader is still reading
    out.gate.set()
    assert partial.drain(30) is True and out.closed
