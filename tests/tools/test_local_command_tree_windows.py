"""A terminal command's process tree ends with the command's kill on Windows (deploy gate D2).

Git Bash (MSYS) lets a command's forked parent exit once the command runs, so the command itself
has a dead parent and ``taskkill /T`` from bash never reaches it: a timed-out or interrupted
command reported "timed out" and kept running (a build, a server, a hung git), holding its files
and pipes. Each command now runs in its own job, terminated on the kill path and merely released
on a normal finish.
"""

from __future__ import annotations

import sys
import time

import psutil
import pytest

pytestmark = pytest.mark.windows_only


def _sleeps(seconds: str) -> list[psutil.Process]:
    """Live MSYS ``sleep <seconds>`` processes (the argument makes them this test's own)."""
    found = []
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() != "sleep.exe":
            continue
        try:
            if proc.cmdline()[-1:] == [seconds]:
                found.append(proc)
        except psutil.Error:
            continue
    return found


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


@pytest.fixture()
def local_env(tmp_path):
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=60)
    yield env
    env.cleanup()


def test_a_timed_out_command_does_not_keep_running(local_env):
    try:
        result = local_env.execute("sleep 317", timeout=3)
        assert result["returncode"] == 124
        assert _wait_until(lambda: not _sleeps("317")), "the timed-out command is still running"
    finally:
        for proc in _sleeps("317"):
            proc.kill()


def test_a_backgrounded_process_outlives_a_normal_finish(local_env):
    """The kill is for timeouts and /stop only: what a command deliberately starts in the background
    keeps running after the command itself finishes, as before."""
    try:
        result = local_env.execute("(sleep 318 &); echo started", timeout=30)
        assert result["returncode"] == 0 and "started" in result["output"]
        time.sleep(1.0)
        assert _sleeps("318"), "a normal finish killed the backgrounded process"
    finally:
        for proc in _sleeps("318"):
            proc.kill()


def test_the_aslr_probe_returns_while_a_descendant_holds_its_pipe(tmp_path, monkeypatch):
    """It runs under the terminal's per-task creation lock once every Git Bash candidate failed; a
    PowerShell descendant holding the pipe kept subprocess.run's Windows cleanup waiting unbounded."""
    from tools.environments import local_gitbash_probe as probe

    holder = tmp_path / "powershell.cmd"
    holder.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" -c "import subprocess, sys, time; '
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(45)']); time.sleep(40)\"\r\n",
        encoding="utf-8")
    monkeypatch.setattr(probe.shutil, "which", lambda _name: str(holder))
    monkeypatch.setattr(probe, "_mandatory_aslr_enabled_cache", None)
    monkeypatch.setattr(probe, "_mandatory_aslr_probed", False, raising=False)

    started = time.monotonic()
    assert probe._mandatory_aslr_enabled() is None
    assert time.monotonic() - started < 25
    started = time.monotonic()
    assert probe._mandatory_aslr_enabled() is None
    assert time.monotonic() - started < 1, "an unknown answer is asked again on every call"


def test_a_killed_background_command_takes_its_whole_tree(tmp_path):
    """A background command whose own child outlives its parent (``(x &)``): the registry's
    ``taskkill /T`` never reached that orphan, so a killed dev server kept running."""
    from tools.process_registry import process_registry

    session = process_registry.spawn_local("(sleep 322 &); sleep 324", cwd=str(tmp_path), task_id="t2-bg")
    try:
        assert _wait_until(lambda: bool(_sleeps("322")) and bool(_sleeps("324"))), "never started"
        process_registry.kill_process(session.id)
        assert _wait_until(lambda: not _sleeps("322") and not _sleeps("324")), "part of the tree survived"
    finally:
        for proc in [*_sleeps("322"), *_sleeps("324")]:
            proc.kill()


def test_a_yielded_command_is_still_killed_whole(local_env, tmp_path):
    """A foreground command handed to the process registry mid-run (the user sent a message) must
    stay killable as a tree: releasing its job at the handoff left the registry only taskkill /T."""
    import threading

    from tools.interrupt import request_yield
    from tools.process_registry import process_registry
    from tools.terminal_tool_background import yield_to_background_handler

    handler = yield_to_background_handler(
        command="sleep 323", env_type="local", cwd=str(tmp_path), effective_task_id="t2-yield",
        task_id="t2-yield", session_key="")
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.update(local_env.execute("sleep 323", timeout=60, yield_handler=handler)))
    try:
        worker.start()
        assert _wait_until(lambda: bool(_sleeps("323"))), "the command never started"
        request_yield(worker.ident)
        worker.join(timeout=20)
        assert result.get("yielded_session_id"), result
        process_registry.kill_process(result["yielded_session_id"])
        assert _wait_until(lambda: not _sleeps("323")), "the yielded command survived its kill"
    finally:
        for proc in _sleeps("323"):
            proc.kill()
