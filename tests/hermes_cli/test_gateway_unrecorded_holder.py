"""A gateway of this profile that no record knows about is still found by start/stop/restart (deploy gate D1).

The 2026-08-25 squatter was a ``pythonw.exe ... --profile claudetriad gateway run`` started by an older
launcher, with no PID record of its own. ``gateway start`` must report it as running instead of
spawning a duplicate that cannot bind, and ``stop``/``restart`` must target it.
"""

from __future__ import annotations

import subprocess
import sys
import time

import psutil
import pytest

from hermes_cli.gateway import find_gateway_pids

_STAND_IN = "import time; print('up', flush=True); time.sleep(120)"


@pytest.mark.spawns_gateway_lookalike
def test_an_unrecorded_gateway_of_this_profile_is_found(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "t2probe"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    proc = subprocess.Popen(
        [sys.executable, "-c", _STAND_IN, "hermes_cli.main", "--profile", "t2probe", "gateway", "run"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "up"
        assert not (home / "gateway.pid").exists()
        # A venv interpreter on Windows is a launcher shim: the stand-in is its child.
        tree = {proc.pid, *(c.pid for c in psutil.Process(proc.pid).children(recursive=True))}
        deadline = time.monotonic() + 30
        found: list = []
        while not tree & set(found) and time.monotonic() < deadline:
            found = find_gateway_pids()
        assert tree & set(found)
    finally:
        for child in psutil.Process(proc.pid).children(recursive=True) if proc.poll() is None else []:
            try:  # the worker behind the venv launcher shim, which proc.kill() leaves running
                child.kill()
            except psutil.Error:
                pass
        proc.kill()
        proc.wait(timeout=10)
