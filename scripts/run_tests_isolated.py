"""Contained test runner for this Windows machine (Magi, 2026-09-22; revised after two Codex reviews).

Why: HERMES_HOME in the user environment points at the live Windows root, and running
``python -m pytest`` directly inherits it. This checkout's conftest only recognised POSIX ``~/.hermes``
as production, so such runs wrote into the live logs, and an update test left a real ``gateway run``
behind. (``scripts/run_tests.sh`` strips the environment and avoids the first leak; upstream
v2026.9.21 fixes both in conftest: #112692, ``ca16cafee4``.)

What this runner guarantees, and what it does not:
1. PATH RESOLUTION ISOLATION. Whitelist-only environment (like run_tests.sh's ``env -i``) with the
   canonical deterministic settings (TZ, LANG, LC_ALL, PYTHONHASHSEED). HERMES_HOME, LOCALAPPDATA,
   APPDATA and TEMP point into a throwaway sandbox, so default-root resolution lands in the sandbox
   even in a child that drops HERMES_HOME. Every other HERMES_* variable and every credential is
   dropped. NOT a filesystem permission boundary.
2. LIVE-DATA GUARDS STAY POINTED AT THE REAL ROOTS. Redirecting LOCALAPPDATA would move the
   state.db / kanban guards' "real root" into the sandbox, so the real roots are captured BEFORE the
   redirect and exported as HERMES_TEST_LIVE_ROOTS, which hermes_state and tests/conftest.py add to
   their deny lists (inherited by every child).
3. PROCESS LIFETIME. The canonical per-file runner is created with CreateProcessW +
   PROC_THREAD_ATTRIBUTE_JOB_LIST, i.e. already inside a Job Object with KILL_ON_JOB_CLOSE and no
   breakaway: creation and containment are atomic, so no process of the run ever exists outside the
   job, even if this runner is itself killed. Closing the job on any exit kills every descendant.
   Launches brokered by WMI are not job-contained.
4. DIAGNOSTIC ONLY: afterwards, lines appended to the live logs (incl. rotated files, tracked by file
   identity, identity re-verified at open) are scanned for test markers. A hit fails the run (exit 3);
   any enumeration/stat/read failure, or a file truncated in place, makes the result INCONCLUSIVE
   (exit 4). A clean scan is not proof that nothing was written.

Usage: python scripts/run_tests_isolated.py [--timeout SECONDS] [--] [run_tests_parallel.py args]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEAK_MARKERS = re.compile(r"pytest-of-|hermes_test|hermes-test-home-|isolated-run-")
_FORWARD = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERPROFILE",
            "HOMEDRIVE", "HOMEPATH", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
_FORWARD_PREFIX = ("HERMES_TEST_",)  # the runner's own documented knobs (workers, paths, timeouts)
_DETERMINISTIC = {"TZ": "UTC", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "0"}


def _real_live_roots() -> list[Path]:
    """Every root the live install could be under, captured BEFORE any redirect."""
    roots = []
    base = os.environ.get("LOCALAPPDATA", "").strip()
    roots.append(Path(base) / "hermes" if base else Path.home() / "AppData" / "Local" / "hermes")
    roots.append(Path.home() / ".hermes")
    if os.environ.get("HERMES_HOME", "").strip():
        roots.append(Path(os.environ["HERMES_HOME"]))
    out = []
    for r in roots:
        try:
            r = r.expanduser().resolve()
        except OSError:
            continue
        if r not in out:
            out.append(r)
    return out


LIVE_ROOTS = _real_live_roots()
LIVE_ROOT = LIVE_ROOTS[0]


def _sandbox_env(box: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k in _FORWARD or k.startswith(_FORWARD_PREFIX)}
    for name in ("home", "localappdata", "appdata", "tmp"):
        (box / name).mkdir(parents=True, exist_ok=True)
    env.update(_DETERMINISTIC)
    env.update(HERMES_HOME=str(box / "home"), LOCALAPPDATA=str(box / "localappdata"),
               APPDATA=str(box / "appdata"), TEMP=str(box / "tmp"), TMP=str(box / "tmp"),
               HERMES_TEST_LIVE_ROOTS=os.pathsep.join(str(r) for r in LIVE_ROOTS),
               PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def _log_snapshot(problems: list[str]) -> dict:
    """{(st_dev, st_ino): (path, size)} for every live log incl. rotated ones; failures -> problems."""
    snap = {}

    def _entries(d: Path, *, dirs: bool) -> list[Path]:
        # Explicit scandir: Path.glob() swallows directory permission errors. Missing != unreadable.
        try:
            with os.scandir(d) as it:
                return [Path(e.path) for e in it if (e.is_dir() if dirs else e.is_file())]
        except FileNotFoundError:
            return []
        except OSError as exc:
            problems.append(f"cannot enumerate {d}: {exc}")
            return []

    log_dirs = [LIVE_ROOT / "logs"] + [p / "logs" for p in _entries(LIVE_ROOT / "profiles", dirs=True)]
    paths = [p for d in log_dirs for p in _entries(d, dirs=False) if ".log" in p.name]
    for p in paths:
        try:
            st = p.stat()
            snap[(st.st_dev, st.st_ino)] = (p, st.st_size)
        except OSError as exc:
            problems.append(f"cannot stat {p}: {exc}")
    return snap


def _scan_appended(before: dict) -> tuple[list[str], list[str]]:
    hits, problems = [], []
    for ident, (p, size) in _log_snapshot(problems).items():
        prior = before.get(ident)
        start = prior[1] if prior else 0  # a file identity not seen before is read from byte 0
        if prior and size < start:
            problems.append(f"{p}: truncated in place ({start} -> {size} bytes); appended content unknowable")
            start = 0
        if size <= start:
            continue
        try:
            with open(p, "rb") as fh:
                st = os.fstat(fh.fileno())
                if (st.st_dev, st.st_ino) != ident:  # rotated between snapshot and open
                    problems.append(f"{p}: replaced during scan")
                    start = 0
                fh.seek(start)
                text = fh.read().decode("utf-8", "replace")
        except OSError as exc:
            problems.append(f"{p}: {exc}")
            continue
        hits += [f"{p}: {line[:160]}" for line in text.splitlines() if LEAK_MARKERS.search(line)]
    return hits, problems


class _Job:
    """Job Object (KILL_ON_JOB_CLOSE, no breakaway) whose child is CREATED inside it (JOB_LIST)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.c, self.w = ctypes, w
        k = self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = w.HANDLE
        k.GetStdHandle.restype = w.HANDLE
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", ctypes.c_ulonglong * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.Basic.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; BREAKAWAY_OK deliberately not set
        if not k.SetInformationJobObject(w.HANDLE(self.handle), 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def spawn(self, cmd: list[str], env: dict, cwd: str) -> "_Proc":
        c, w, k = self.c, self.w, self.k32

        class STARTUPINFOW(c.Structure):
            _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                        ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                        ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                        ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                        ("lpReserved2", c.c_void_p), ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                        ("hStdError", w.HANDLE)]

        class STARTUPINFOEXW(c.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", c.c_void_p)]

        class PROCESS_INFORMATION(c.Structure):
            _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

        size = c.c_size_t(0)
        k.InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
        attrs = c.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(attrs, 1, 0, c.byref(size)):
            raise c.WinError(c.get_last_error())
        try:
            jobs = (w.HANDLE * 1)(self.handle)
            PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
            if not k.UpdateProcThreadAttribute(attrs, 0, c.c_size_t(PROC_THREAD_ATTRIBUTE_JOB_LIST), jobs,
                                               c.c_size_t(c.sizeof(jobs)), None, None):
                raise c.WinError(c.get_last_error())
            si = STARTUPINFOEXW()
            si.StartupInfo.cb = c.sizeof(STARTUPINFOEXW)
            si.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES: keep this runner's stdout/stderr
            std = [k.GetStdHandle(w.DWORD(n & 0xFFFFFFFF)) for n in (-10, -11, -12)]
            for h in std:  # a pipe handle from our parent may not be inheritable; the child needs it
                if h:
                    k.SetHandleInformation(w.HANDLE(h), 1, 1)  # HANDLE_FLAG_INHERIT
            si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = std
            si.lpAttributeList = c.cast(attrs, c.c_void_p)
            block = "".join(f"{key}={val}\0" for key, val in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
            # UTF-16 code units, not Python characters: a non-BMP char is two units. Embedded NULs and
            # the double-NUL terminator survive the encode.
            env_buf = c.create_string_buffer(block.encode("utf-16-le"))
            cmdline = c.create_unicode_buffer(subprocess.list2cmdline(cmd))
            pi = PROCESS_INFORMATION()
            flags = 0x00080000 | 0x00000400  # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
            if not k.CreateProcessW(None, cmdline, None, None, True, flags, env_buf, cwd,
                                    c.byref(si), c.byref(pi)):
                raise c.WinError(c.get_last_error())
        finally:
            k.DeleteProcThreadAttributeList(attrs)
        k.CloseHandle(pi.hThread)
        return _Proc(k, pi.hProcess)

    def close(self):
        if self.handle:
            self.k32.CloseHandle(self.w.HANDLE(self.handle))  # kills every process still in the job
            self.handle = None


class _Proc:
    def __init__(self, k32, handle):
        self.k32, self.handle = k32, handle

    def wait(self, timeout=None) -> int:
        import time
        from ctypes import byref, wintypes as w
        # Short native waits so a pending Ctrl-C is handled between slices (Python runs signal
        # handlers only between bytecodes, never inside a blocking native call).
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.k32.WaitForSingleObject(w.HANDLE(self.handle), w.DWORD(200)) == 0x102:  # WAIT_TIMEOUT
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("run_tests_parallel.py", timeout)
        code = w.DWORD()
        self.k32.GetExitCodeProcess(w.HANDLE(self.handle), byref(code))
        self.k32.CloseHandle(w.HANDLE(self.handle))
        return int(code.value)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("runner_args", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    args = [x for x in a.runner_args if x != "--"]
    if os.name != "nt":
        sys.exit("run_tests_isolated.py is for native Windows; use scripts/run_tests.sh elsewhere")

    box = Path(tempfile.mkdtemp(prefix="isolated-run-"))
    problems_before: list[str] = []
    before = _log_snapshot(problems_before)
    job = _Job()
    rc = 1
    try:
        proc = job.spawn([sys.executable, str(REPO / "scripts" / "run_tests_parallel.py"), *args],
                         env=_sandbox_env(box), cwd=str(REPO))
        rc = proc.wait(timeout=a.timeout)
    except subprocess.TimeoutExpired:
        print(f"[isolated-run] timeout after {a.timeout}s; killing the job", file=sys.stderr)
        rc = 124
    except KeyboardInterrupt:
        rc = 130
    finally:
        job.close()
        shutil.rmtree(box, ignore_errors=True)

    hits, problems = _scan_appended(before)
    problems = problems_before + problems
    if hits:
        print(f"[isolated-run] LIVE-LOG LEAK: {len(hits)} test-marked line(s) in the live logs:", file=sys.stderr)
        for line in hits[:20]:
            print("  " + line, file=sys.stderr)
        return rc or 3
    if problems:
        print("[isolated-run] INCONCLUSIVE log diagnostic: " + "; ".join(problems[:5]), file=sys.stderr)
        return rc or 4
    print("[isolated-run] diagnostic: no test-marked lines appended to the live logs "
          "(not proof of no writes); job closed, all child processes terminated", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
