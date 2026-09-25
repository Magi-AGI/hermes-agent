"""A frozen backend's process tree yields native minidumps (Windows).

The 2026-09-07 freeze left only two identical py-spy dumps: Python-level evidence cannot tell an idle
loop from a thread blocked in the kernel. The desktop supervisor runs ``hermes_cli.freeze_dump``
before restarting a backend that stopped answering; this proves it captures a real, fully suspended
process tree (the 09-07 signature: every thread stopped, zero CPU), children included.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time

import pytest

_TREE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _suspend(pid: int) -> None:
    ntdll = ctypes.WinDLL("ntdll")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    assert handle, "could not open the test process to suspend it"
    try:
        assert ntdll.NtSuspendProcess(ctypes.c_void_p(handle)) == 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


@pytest.mark.windows_only
def test_a_suspended_process_tree_is_dumped_with_its_children(tmp_path):
    import psutil

    from hermes_cli.freeze_dump import dump_process_tree

    parent = subprocess.Popen([sys.executable, "-c", _TREE], stdout=subprocess.PIPE, text=True)
    try:
        child_pid = int(parent.stdout.readline())
        time.sleep(0.2)
        # Under a venv each interpreter is a launcher + worker pair; freeze the whole tree.
        tree = {parent.pid, *(p.pid for p in psutil.Process(parent.pid).children(recursive=True))}
        for pid in tree:
            _suspend(pid)

        written = dump_process_tree(parent.pid, tmp_path / "freeze-dumps")

        dumped = {int(p.stem.rsplit("-", 1)[1]) for p in written}
        assert child_pid in tree
        assert dumped == tree, "every process of the frozen tree (backend + compute host) must be dumped"
        for path in written:
            assert path.read_bytes()[:4] == b"MDMP", f"{path.name} is not a minidump"
    finally:
        subprocess.run(["taskkill", "/PID", str(parent.pid), "/T", "/F"], capture_output=True)
        parent.wait(timeout=10)
