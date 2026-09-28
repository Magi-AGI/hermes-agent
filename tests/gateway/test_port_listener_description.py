"""A port conflict names its holder, and tells this profile's gateway from anything else (deploy gate D1).

2026-07-26 and 2026-08-25: a surviving ``pythonw.exe -m hermes_cli.main --profile claudetriad gateway
run`` held :8642 after the desktop app quit, and the fresh gateway could only say "already in use".
"""

from __future__ import annotations

import socket
import subprocess
import sys

import psutil
import pytest

from gateway.status import describe_port_listener

# A listener whose command line reads like a gateway of profile t2probe; it runs no gateway code.
_STAND_IN = (
    "import socket, sys, time; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1); "
    "print(s.getsockname()[1], flush=True); time.sleep(120)")


@pytest.fixture()
def stand_in_gateway():
    proc = subprocess.Popen(
        [sys.executable, "-c", _STAND_IN, "hermes_cli.main", "--profile", "t2probe", "gateway", "run"],
        stdout=subprocess.PIPE, text=True)
    tree: set = set()
    try:
        port = int(proc.stdout.readline())
        # A venv interpreter on Windows is a launcher shim: the listener is its child.
        tree = {proc.pid, *(c.pid for c in psutil.Process(proc.pid).children(recursive=True))}
        yield tree, port
    finally:
        for pid in tree - {proc.pid}:  # the worker behind the shim, which proc.kill() leaves running
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.spawns_gateway_lookalike
def test_a_gateway_holding_the_port_is_named_with_its_profile(stand_in_gateway, tmp_path, monkeypatch):
    pids, port = stand_in_gateway
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "t2probe"))
    assert any(describe_port_listener(port).startswith(f"PID {pid}, a Hermes gateway of this profile by its command line")
               for pid in pids)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "other"))
    assert any(describe_port_listener(port).startswith(f"PID {pid}, a Hermes gateway of profile 't2probe' by its command line")
               for pid in pids)


def test_a_foreign_listener_is_named_as_not_a_gateway():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        described = describe_port_listener(sock.getsockname()[1])
    assert described is not None and "not a Hermes gateway" in described
