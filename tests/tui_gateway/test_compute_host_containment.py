"""The compute host's process tree never outlives it (deploy gate A12, D2).

Parent death during an isolated turn used to leave the turn's TOOL running: transcript fencing
stops an orphaned host's writes, not its processes. On Windows ``taskkill /T`` follows LIVE parent
links only, and MSYS bash lets the forked parent of a command exit, so a tool grandchild with a
dead parent survived every host kill and backend death, holding its pipes (the 420 s terminal
hang recurred across host respawns). The supervisor now owns a kill-on-close job holding the
whole host tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from tui_gateway.host_supervisor import HostSupervisor

pytestmark = pytest.mark.windows_only

_REPO = Path(__file__).resolve().parents[2]

# A host that starts a "tool" whose own parent exits at once (the orphaned-grandchild shape), says
# hello, then either crashes shortly after or stays up until its control pipe closes.
_FAKE_HOST = r'''
import json, os, subprocess, sys, time
pid_file, crash = sys.argv[1], sys.argv[2] == "crash"
tool = "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); open(sys.argv[1], 'w').write(str(p.pid))"
subprocess.Popen([sys.executable, "-c", tool, pid_file]).wait()
print(json.dumps({"type": "hello", "boot_id": "b", "host_pid": os.getpid()}), flush=True)
if crash:
    time.sleep(0.5)
    sys.exit(1)
sys.stdin.read()
'''


def _wait_until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _reap(*pids: int) -> None:
    """Test hygiene only: kill what THIS test started, by exact pid, if it is still there."""
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass


def _tool_pid(pid_file: Path) -> int:
    assert _wait_until(lambda: pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()), "tool never started"
    return int(pid_file.read_text(encoding="utf-8"))


def test_a_crashed_host_takes_its_orphaned_tool_process_with_it(tmp_path):
    pid_file = tmp_path / "tool.pid"
    sup = HostSupervisor(
        registry_path=tmp_path / "host.json", argv=[sys.executable, "-c", _FAKE_HOST, str(pid_file), "crash"],
        cwd=tmp_path, respawn_max=0, expected_build_sha="test", expected_hermes_home="", autostart=False)
    tool = 0
    try:
        sup.start()
        tool = _tool_pid(pid_file)
        assert _alive(tool)
        assert _wait_until(lambda: not _alive(tool), timeout=20), "the tool outlived the host that ran it"
    finally:
        sup.shutdown()
        _reap(tool)


_OWNER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from tui_gateway.host_supervisor import HostSupervisor
sup = HostSupervisor(registry_path=sys.argv[2], argv=[sys.executable, "-c", sys.argv[3], sys.argv[4], "stay"],
                     cwd=sys.argv[5], expected_build_sha="test", expected_hermes_home="")
print(json.dumps({"worker": sup.hello["host_pid"]}), flush=True)
sys.stdin.read()
'''


def test_a_dead_gateway_leaves_no_host_or_tool_running(tmp_path):
    pid_file = tmp_path / "tool.pid"
    env = {**os.environ, "HERMES_HOME": str(tmp_path / "home")}
    owner = subprocess.Popen(
        [sys.executable, "-c", _OWNER, str(_REPO), str(tmp_path / "host.json"), _FAKE_HOST, str(pid_file),
         str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    worker = tool = 0
    try:
        worker = json.loads(owner.stdout.readline())["worker"]
        tool = _tool_pid(pid_file)
        assert _alive(worker) and _alive(tool)

        owner.kill()  # the gateway dies without any cleanup of its own
        owner.wait(timeout=10)

        assert _wait_until(lambda: not _alive(worker), timeout=10), "the host outlived its gateway"
        assert _wait_until(lambda: not _alive(tool), timeout=10), "the turn's tool outlived its gateway"
    finally:
        if owner.poll() is None:
            owner.kill()
        _reap(worker, tool)


def test_a_host_probe_returns_while_a_descendant_holds_its_pipe():
    """Every host spawn and hello runs a short probe (git rev-parse). subprocess's Windows cleanup after a
    timeout waited, unbounded, for pipes a surviving descendant still held (2026-09-14: one hung thread
    and one orphaned git.exe per compute host)."""
    from tui_gateway.host_supervisor import _check_output

    holder = ("import subprocess, sys, time; "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)']); time.sleep(30)")
    started = time.monotonic()
    assert _check_output([sys.executable, "-c", holder]) == ""
    assert time.monotonic() - started < 20


def test_the_liveness_probe_reports_a_live_process_alive_and_leaves_it_running():
    """os.kill(pid, 0) is no probe on Windows: signal 0 is CTRL_C_EVENT, so CPython calls
    GenerateConsoleCtrlEvent for that pid (bpo-14484) -- an error ("dead") for a live process that
    leads no console group, or an interrupt delivered into one that does."""
    from tui_gateway.host_supervisor import _pid_alive

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert _pid_alive(child.pid) is True
        time.sleep(0.5)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=10)
    assert _pid_alive(child.pid) is False



def test_a_process_launched_outside_the_job_survives_the_job():
    """What a deliberately detached launch from inside a compute host (the messaging gateway) relies
    on: the host's job forbids breakaway, yet the WMI-created process is not in it."""
    from hermes_cli._subprocess_compat import launch_outside_job
    from hermes_cli.local_runtime.processes import spawn_server

    marker = f"t2-outside-job-{os.getpid()}"
    launcher = ("import subprocess, sys; from hermes_cli._subprocess_compat import launch_outside_job; "
                f"print(launch_outside_job(subprocess.list2cmdline([sys.executable, '-c', "
                f"'import time; time.sleep(120)', {marker!r}])), flush=True); time.sleep(60)")
    proc, job = spawn_server([sys.executable, "-c", "import time; " + launcher],
                             stdout=subprocess.PIPE, text=True, cwd=str(_REPO), env={**os.environ, "PYTHONPATH": str(_REPO)})
    survivor = 0
    try:
        survivor = int(proc.stdout.readline())
        assert _alive(survivor)
        job.close()  # kill-on-close, no breakaway: everything in the job dies here
        assert _wait_until(lambda: proc.poll() is not None, timeout=10)
        time.sleep(1.0)
        assert _alive(survivor), "the process launched outside the job died with it"
        assert marker in " ".join(psutil.Process(survivor).cmdline())
    finally:
        _reap(survivor)
        for p in psutil.process_iter():  # the venv launcher's worker child, if any
            try:
                if marker in " ".join(p.cmdline()):
                    p.kill()
            except psutil.Error:
                pass
