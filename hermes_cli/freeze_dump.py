"""Native minidumps of a frozen backend (Windows), written before the desktop restarts it.

The 2026-09-07 backend freeze left two identical py-spy dumps and nothing else: a Python-level dump
cannot tell a thread parked in the interpreter from one blocked in the kernel, and an idle event
loop looks the same as a wedged one. When the desktop's liveness supervisor gives up on a backend
it runs ``python -m hermes_cli.freeze_dump <pid> <dir>`` first, so the next freeze leaves the
native stack, thread state and handle table of the backend and every descendant (the compute
host) behind. Needs no debugger or extra install: ``dbghelp.MiniDumpWriteDump`` via ctypes.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

# Thread stacks are in every minidump; these add what a freeze diagnosis needs: which handles each
# thread waits on, per-thread state and times, the memory map, and 1 KiB around every pointer found
# on a stack (enough to follow into the interpreter's frame objects) without a full-memory dump.
_MINIDUMP_FLAGS = (
    0x00000004  # MiniDumpWithHandleData
    | 0x00000020  # MiniDumpWithUnloadedModules
    | 0x00000040  # MiniDumpWithIndirectlyReferencedMemory
    | 0x00000100  # MiniDumpWithProcessThreadData
    | 0x00000800  # MiniDumpWithFullMemoryInfo
    | 0x00001000  # MiniDumpWithThreadInfo
)
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010
_PROCESS_DUP_HANDLE = 0x0040
#: Freeze incidents kept per directory. An incident is every dump of one capture (one file per
#: process of the tree); pruning never splits one, and never touches the incident just written.
KEEP_INCIDENTS = 3


def write_minidump(pid: int, path: Path) -> Path:
    """Write a minidump of process *pid* to *path*. Raises OSError on failure."""
    if sys.platform != "win32":
        raise OSError("native minidumps are Windows-only")
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    dbghelp.MiniDumpWriteDump.restype = wintypes.BOOL
    dbghelp.MiniDumpWriteDump.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

    process = kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ | _PROCESS_DUP_HANDLE, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "wb") as out:
            ok = dbghelp.MiniDumpWriteDump(process, pid, msvcrt.get_osfhandle(out.fileno()), _MINIDUMP_FLAGS,
                                           None, None, None)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        kernel32.CloseHandle(process)
    return path


def dump_process_tree(pid: int, out_dir: Path, *, label: str = "backend") -> list[Path]:
    """Dump *pid* and every live descendant into *out_dir*; keep :data:`KEEP_INCIDENTS` incidents.

    A process that cannot be dumped (exited meanwhile, access denied) is skipped and reported on
    stderr; the others are still written."""
    import psutil

    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        root = psutil.Process(pid)
        tree = [root, *root.children(recursive=True)]
    except psutil.Error as exc:
        raise OSError(f"process {pid} is not inspectable: {exc}") from exc
    incident = f"{label}-{stamp}-{pid}"
    written: list[Path] = []
    for proc in tree:
        target = out_dir / f"{incident}-{proc.pid}.dmp"
        try:
            written.append(write_minidump(proc.pid, target))
        except OSError as exc:
            print(f"freeze_dump: skipped pid {proc.pid}: {exc}", file=sys.stderr)
    _prune(out_dir, keep=incident)
    return written


def _prune(out_dir: Path, *, keep: str) -> None:
    """Delete whole incidents beyond the newest :data:`KEEP_INCIDENTS`, *keep* always included."""
    incidents: dict[str, list[Path]] = {}
    for dump in out_dir.glob("*.dmp"):
        incidents.setdefault(dump.stem.rsplit("-", 1)[0], []).append(dump)
    newest_first = sorted((name for name in incidents if name != keep),
                          key=lambda name: max(p.stat().st_mtime for p in incidents[name]), reverse=True)
    for stale in newest_first[KEEP_INCIDENTS - 1:]:
        for dump in incidents[stale]:
            dump.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hermes_cli.freeze_dump", description=__doc__.splitlines()[0])
    parser.add_argument("pid", type=int)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--label", default="backend")
    args = parser.parse_args(argv)
    try:
        written = dump_process_tree(args.pid, args.out_dir, label=args.label)
    except OSError as exc:
        print(f"freeze_dump: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
