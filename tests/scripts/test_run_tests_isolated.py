"""scripts/run_tests_isolated.py and the HERMES_TEST_LIVE_ROOTS guard extension (Magi, 2026-09-22)."""

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hermes_state

_SPEC = importlib.util.spec_from_file_location(
    "run_tests_isolated", Path(__file__).resolve().parents[2] / "scripts" / "run_tests_isolated.py")
rti = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rti)


def test_exported_live_root_database_is_refused(tmp_path, monkeypatch):
    """Codex round 2 #1: with LOCALAPPDATA redirected, the platform root points at the sandbox; the
    REAL roots exported in HERMES_TEST_LIVE_ROOTS must still be refused (root and profile DBs)."""
    live = tmp_path / "live-root"
    (live / "profiles" / "p").mkdir(parents=True)
    monkeypatch.setenv("HERMES_TEST_LIVE_ROOTS", str(live))
    monkeypatch.setattr(hermes_state, "_STATE_DB_GUARD_BYPASS", False)
    for db in (live / "state.db", live / "profiles" / "p" / "state.db"):
        with pytest.raises(RuntimeError, match="live-system guard"):
            hermes_state._ensure_test_isolation(db)
    hermes_state._ensure_test_isolation(tmp_path / "elsewhere" / "state.db")  # not under a live root


def test_sandbox_env_exports_real_roots_and_drops_other_hermes_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_STATE_DB_GUARD_BYPASS", "1")
    monkeypatch.setenv("HERMES_TEST_WORKERS", "2")
    env = rti._sandbox_env(tmp_path / "box")
    assert "HERMES_KANBAN_HOME" not in env and "HERMES_STATE_DB_GUARD_BYPASS" not in env
    assert env["HERMES_TEST_WORKERS"] == "2"
    assert env["HERMES_HOME"].startswith(str(tmp_path / "box"))
    assert env["LOCALAPPDATA"].startswith(str(tmp_path / "box"))
    exported = env["HERMES_TEST_LIVE_ROOTS"].split(os.pathsep)
    assert str(rti.LIVE_ROOTS[0]) in exported and not any(str(tmp_path) in r for r in exported)
    assert env["PYTHONHASHSEED"] == "0" and env["TZ"] == "UTC"


def test_truncated_log_is_inconclusive_not_clean(tmp_path, monkeypatch):
    logs = tmp_path / "root" / "logs"
    logs.mkdir(parents=True)
    log = logs / "agent.log"
    log.write_text("x" * 100)
    monkeypatch.setattr(rti, "LIVE_ROOT", tmp_path / "root")
    before = rti._log_snapshot([])
    with open(log, "r+") as fh:  # truncate in place: same file identity, smaller size
        fh.truncate(10)
    hits, problems = rti._scan_appended(before)
    assert not hits and any("truncated" in p for p in problems)


def test_unreadable_log_is_inconclusive(tmp_path, monkeypatch):
    logs = tmp_path / "root" / "logs"
    logs.mkdir(parents=True)
    (logs / "agent.log").write_text("a\n")
    monkeypatch.setattr(rti, "LIVE_ROOT", tmp_path / "root")
    before = rti._log_snapshot([])
    (logs / "agent.log").write_text("a\nb\nc\n")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "locked"))
                        if str(a[0]).endswith("agent.log") else real_open(*a, **k))
    hits, problems = rti._scan_appended(before)
    assert not hits and problems


@pytest.mark.windows_only
def test_job_list_child_and_grandchild_die_when_the_job_closes(tmp_path):
    """Codex round 2 #2: creation is atomic with containment, so the grandchild is in the job too."""
    pidfile = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open(r'{pidfile}', 'w').write(str(p.pid))\n"
        "time.sleep(120)\n")
    job = rti._Job()
    try:
        job.spawn([sys.executable, "-c", script], env=dict(os.environ), cwd=str(tmp_path))
        deadline = time.time() + 30
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.2)
        gpid = int(pidfile.read_text())
    finally:
        job.close()
    time.sleep(1.0)
    out = subprocess.run(["tasklist", "/FI", f"PID eq {gpid}", "/NH"], capture_output=True, text=True).stdout
    assert str(gpid) not in out, "grandchild survived closing the job"


def test_unreadable_log_directory_is_inconclusive(tmp_path, monkeypatch):
    """Codex round 3 #3: Path.glob swallowed directory permission errors; scandir must report them."""
    (tmp_path / "root" / "logs").mkdir(parents=True)
    monkeypatch.setattr(rti, "LIVE_ROOT", tmp_path / "root")
    real_scandir = os.scandir

    def _deny(path):
        if str(path).endswith("logs"):
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr(rti.os, "scandir", _deny)
    problems = []
    assert rti._log_snapshot(problems) == {} and problems
    monkeypatch.setattr(rti.os, "scandir", real_scandir)
    problems = []
    rti._log_snapshot(problems)  # a MISSING profiles dir is not a problem
    assert problems == []


@pytest.mark.windows_only
def test_non_bmp_environment_value_starts(tmp_path):
    """Codex round 3 #2: the env block is built from UTF-16 code units, so non-BMP values work."""
    out = tmp_path / "seen.txt"
    job = rti._Job()
    try:
        proc = job.spawn([sys.executable, "-c", f"import os; open(r'{out}', 'w', encoding='utf-8').write(os.environ['X'])"],
                         env={**os.environ, "X": "path-\U0001F600-ok"}, cwd=str(tmp_path))
        assert proc.wait(timeout=30) == 0
    finally:
        job.close()
    assert out.read_text(encoding="utf-8") == "path-\U0001F600-ok"


@pytest.mark.windows_only
def test_wait_returns_promptly_on_timeout_with_short_slices(tmp_path):
    """Codex round 3 #1: waiting in slices keeps interrupts/timeouts responsive."""
    job = rti._Job()
    try:
        proc = job.spawn([sys.executable, "-c", "import time; time.sleep(30)"], env=dict(os.environ), cwd=str(tmp_path))
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.5)
        assert time.monotonic() - start < 2.0
    finally:
        job.close()


@pytest.mark.windows_only
def test_pending_interrupt_is_handled_during_an_untimed_wait(tmp_path):
    """Codex round 4 nit: with wait(timeout=None) the old INFINITE native wait held a pending Ctrl-C
    until the child exited; sliced waits deliver it promptly so job.close() is reached."""
    import _thread
    import threading

    job = rti._Job()
    try:
        proc = job.spawn([sys.executable, "-c", "import time; time.sleep(30)"], env=dict(os.environ), cwd=str(tmp_path))
        threading.Timer(0.3, _thread.interrupt_main).start()
        start = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            proc.wait()  # no timeout
        assert time.monotonic() - start < 3.0
    finally:
        job.close()
