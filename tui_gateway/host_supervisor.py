"""Supervisor for the dashboard compute-host child: with ``dashboard.turn_isolation``
agent turns run in one persistent ``python -m tui_gateway.compute_host`` child so heavy
agent threads do not contend with the serving process' event loop for the GIL."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from tools.environments.local import hermes_subprocess_env

logger = logging.getLogger(__name__)

MUTATOR_ROUTE_TABLE: dict[str, str] = {
    "prompt.submit": "turn-path", "session.interrupt": "turn-path", "reload.mcp": "run-concurrent",
    "session.save": "run-concurrent", "session.compress": "idle-gated",
    "prompt.submit.truncate": "idle-gated", "slash.model": "idle-gated",
    "slash.personality": "idle-gated", "slash.prompt": "idle-gated", "slash.compress": "idle-gated",
    "session.reset": "idle-gated", "session.history.reload": "idle-gated",
    "slash.retry": "idle-gated"}

_REGISTRY_NAME = "dashboard-compute-host.json"
_RESPAWN_WINDOW_SECS = 300.0
# The crash-loop latch clears this long after it was set (checked on the next start()); a
# permanent latch stranded every isolated session in the profile until a backend restart
# (2026-09-15). Worst case stays bounded: respawn_max crashes, then a pause.
_LATCH_COOLDOWN_SECS = _RESPAWN_WINDOW_SECS
_SHUTDOWN_TIMEOUT_SECS = 10.0
_ORPHAN_EXIT_CODE = 18  # must match compute_host._ORPHAN_EXIT_CODE
_STDIN_LOST_EXIT_CODE = 19  # must match compute_host._STDIN_LOST_EXIT_CODE
# The host's own parent is the venv launcher shim on Windows, not this process: it needs this pid
# to tell "the gateway is gone" from "the control pipe broke while the gateway is alive".
OWNER_PID_ENV = "HERMES_COMPUTE_HOST_OWNER_PID"
# Late control-ack handlers: a compress that outlives its RPC waiter can run for the full
# compression ceiling plus a stall-fallback retry, so keep registrations past that — bounded.
# See #97948.
_LATE_CONTROL_TTL_SECS = 1800.0
_LATE_CONTROL_MAX = 64
# Host frames whose ``request_id`` resolves a pending/late control waiter.
_CONTROL_REPLY_TYPES = frozenset({
    "control.ack", "control.error", "respond.ack", "respond.error", "interrupt.ack",
    "reload_mcp.ack", "shutdown.ack"})


def append_log_record(path: str | Path, record: str) -> None:
    """Append one log record using O_APPEND and exactly one os.write call."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    text = record if record.endswith("\n") else f"{record}\n"
    if sys.platform == "win32":
        # The CRT emulates O_APPEND as seek-then-write, so concurrent records overwrote each other
        # (26 of 32 survived). A handle with FILE_APPEND_DATA alone makes each write an atomic append.
        import _winapi
        import msvcrt
        handle = _winapi.CreateFile(str(path), 0x0004, 0x0007, 0, 4, 0x80, 0)  # APPEND_DATA, share all, OPEN_ALWAYS
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, text.encode("utf-8", errors="replace"))
    finally:
        os.close(fd)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _check_output(argv: list[str], *, cwd: str | None = None) -> str:
    """Stripped stdout of a short subprocess, or ``""`` on any failure. Bounded even when a descendant
    keeps the pipe open (see ``bounded_probe_run``): this runs on every host spawn and hello."""
    from hermes_cli._subprocess_compat import bounded_probe_run

    result = bounded_probe_run(argv, timeout=2, cwd=cwd)
    return result.stdout.strip() if result is not None and result.returncode == 0 else ""


def _build_sha() -> str:
    """HEAD sha or ``"unknown"``; shared with ``compute_host`` so the hello handshake agrees."""
    return _check_output(["git", "rev-parse", "HEAD"], cwd=str(_repo_root())) or "unknown"


def _call_logged(cb: Callable[[dict], None], frame: dict, failure: str) -> None:
    """Invoke a host-frame callback; a raising callback is logged, never propagated."""
    try:
        cb(frame)
    except Exception:
        logger.exception(failure)


def _pid_alive(pid: int) -> bool:
    # psutil, never os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT, so CPython calls
    # GenerateConsoleCtrlEvent on that pid's process group (bpo-14484) -- an interrupt, not a probe.
    if pid <= 0:
        return False
    import psutil

    return psutil.pid_exists(pid)


def _signal_pid(pid: int, sig: int, label: str) -> bool:
    """Send ``sig``; False when the pid is gone or the signal failed (logged)."""
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        logger.debug("failed to %s compute host pid=%s", label, pid, exc_info=True)
        return False


def _write_handle(proc: subprocess.Popen) -> int | None:
    """OS handle behind our end of the host's stdin pipe (Windows), to tell later whether it was
    closed or its fd reused elsewhere in this process; None where there is nothing to compare."""
    if sys.platform != "win32" or proc.stdin is None:
        return None
    with contextlib.suppress(Exception):
        import msvcrt
        return int(msvcrt.get_osfhandle(proc.stdin.fileno()))
    return None


def _pid_command(pid: int) -> str:
    if pid <= 0:
        return ""
    with contextlib.suppress(Exception):  # Linux fast path
        data = (Path("/proc") / str(pid) / "cmdline").read_bytes()
        if data:
            return data.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    return _check_output(["ps", "-p", str(pid), "-o", "command="])


def is_compute_host_identity(pid: int) -> bool:
    return "tui_gateway.compute_host" in _pid_command(pid)


class ComputeHostUnavailable(RuntimeError):
    """The compute host is paused after a crash loop; the message says when it retries."""


class HostSupervisor:
    """Own one persistent compute-host child and relay its frames."""

    def __init__(
        self, *, registry_path: str | Path | None = None, argv: list[str] | None = None,
        cwd: str | Path | None = None, env: dict[str, str] | None = None,
        rpc_sink: Callable[[dict], None] | None = None, respawn_max: int = 3,
        heartbeat_secs: int = 15, expected_build_sha: str | None = None,
        expected_hermes_home: str | None = None, autostart: bool = True,
        heartbeat_timeout_secs: float | None = None) -> None:
        self.registry_path = (
            Path(registry_path) if registry_path is not None
            else get_hermes_home() / "state" / _REGISTRY_NAME)
        self.argv = argv or [sys.executable, "-m", "tui_gateway.compute_host"]
        self.cwd = Path(cwd) if cwd is not None else _repo_root()
        self.env = env
        self.rpc_sink = rpc_sink or (lambda _obj: None)
        self.respawn_max = max(0, int(respawn_max))
        self.heartbeat_secs = max(1, int(heartbeat_secs))
        self.expected_build_sha = _build_sha() if expected_build_sha is None else expected_build_sha
        self.expected_hermes_home = (
            str(get_hermes_home()) if expected_hermes_home is None else expected_hermes_home)
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        # Popen -> (Windows job containing that host's tree, OS handle of our stdin write end).
        self._spawned: dict[subprocess.Popen, tuple[Any, int | None]] = {}
        self._hello_event = threading.Event()
        self._hello: dict[str, Any] = {}
        self._closing = False
        self._stopped_respawning = False
        self._latched_at = 0.0
        self._restart_times: list[float] = []
        self._pending_turns: dict[str, tuple[str, Callable[[dict], None] | None]] = {}
        # request_id -> the host process the frame was written to: a dying host fails only what it
        # owned, never the turns its replacement already took (see _wait_for_exit).
        self._request_hosts: dict[str, subprocess.Popen] = {}
        self._pending_controls: dict[str, queue.Queue[dict]] = {}
        # request_id -> (registered_at, handler) for control waiters that timed out while their
        # host work still runs, so the eventual control.ack is not silently dropped.
        # The host emits its control.ack whenever it finishes; without this the ack matched no queue and was
        # silently dropped. See #97948.
        self._late_control_handlers: dict[str, tuple[float, Callable[[dict], None]]] = {}
        self._stderr_tail: list[str] = []
        self._last_progress_counter = 0
        self._last_hb_monotonic: float = 0.0
        # The gateway always passes dashboard.compute_host_heartbeat_timeout_secs (default 120; 0 disables);
        # only a supervisor built without a value reads the upstream HERMES_HOST_HEARTBEAT_TIMEOUT_SECS knob.
        try:
            _cfg = float(heartbeat_timeout_secs if heartbeat_timeout_secs is not None
                         else os.environ.get("HERMES_HOST_HEARTBEAT_TIMEOUT_SECS") or "0")
        except (TypeError, ValueError):
            logger.warning("invalid HERMES_HOST_HEARTBEAT_TIMEOUT_SECS=%r; watchdog disabled",
                           os.environ.get("HERMES_HOST_HEARTBEAT_TIMEOUT_SECS"))
            _cfg = 0.0
        if _cfg > 0:
            self._hb_timeout_secs = max(_cfg, 3.0 * self.heartbeat_secs)
            if self._hb_timeout_secs != _cfg:
                logger.warning("hb-watchdog timeout raised %.1fs -> %.1fs (>= 3x heartbeat=%ss)",
                               _cfg, self._hb_timeout_secs, self.heartbeat_secs)
        else:
            self._hb_timeout_secs = 0.0
        if self._hb_timeout_secs > 0:
            threading.Thread(target=self._heartbeat_watchdog_loop,
                             name="compute-host-hb-watchdog", daemon=True).start()
        if autostart:
            try:
                self.start()
            except Exception:
                # Startup failed → the caller gets no object to shut down; _closing stops
                # the opt-in watchdog loop (`while not self._closing`) so it cannot leak.
                self._closing = True
                raise

    @property
    def pid(self) -> int:
        proc = self._proc
        return int(proc.pid or 0) if proc is not None else 0

    @property
    def hello(self) -> dict[str, Any]:
        return dict(self._hello)

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None and not self._stopped_respawning

    def respawn_blocked_for(self) -> float:
        """Seconds until the crash-loop latch clears, or 0.0 when spawning is allowed."""
        if not self._stopped_respawning:
            return 0.0
        return max(0.0, _LATCH_COOLDOWN_SECS - (time.monotonic() - self._latched_at))

    def start(self) -> None:
        with self._lock:
            if self._stopped_respawning and self.respawn_blocked_for() <= 0.0:
                self._stopped_respawning = False
                self._restart_times = []
                logger.warning("compute host crash-loop latch cleared after %.0fs; respawn re-enabled",
                               time.monotonic() - self._latched_at)
            if self.is_running():
                return
            self._closing = False
            self.reconcile_startup_orphan()
            self._spawn_locked(reason="startup")

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            proc = self._proc
        # Registry removal must run even on the proc-is-None path, so it lives in an
        # OUTER finally (an early return would skip it).
        try:
            if proc is not None:
                try:
                    if proc.poll() is None and proc.stdin is not None:
                        self._send_frame({"type": "shutdown", "request_id": f"shutdown-{uuid.uuid4().hex}"})
                        proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECS)
                except Exception:
                    self._terminate_process(proc)
        finally:
            self._remove_registry()

    def reconcile_startup_orphan(self) -> str:
        """Terminate a stale registered host, guarding against PID reuse."""
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "none"
        except Exception:
            data = None
        try:
            pid = int((data or {}).get("host_pid") or 0)
        except Exception:
            pid = 0
        if data is None:
            outcome = "invalid-registry"
        elif pid <= 0 or not _pid_alive(pid):
            outcome = "not-running"
        elif not self._pid_matches_compute_host(pid):
            outcome = "pid-reuse-ignored"  # PID reused by another process: never signal it
        else:
            self._terminate_pid(pid, timeout=_SHUTDOWN_TIMEOUT_SECS)
            outcome = "terminated"
        self._remove_registry()
        return outcome

    def submit_turn(self, frame: dict[str, Any], *, on_complete: Callable[[dict], None] | None = None) -> str:
        self.start()
        request_id = str(frame.get("request_id") or uuid.uuid4().hex)
        sid = str(frame.get("sid") or "")
        payload = {**frame, "type": "turn.start", "request_id": request_id}
        try:
            with self._lock:  # registered and sent under one hold: it belongs to the host it reached
                self._pending_turns[request_id] = (sid, on_complete)
                self._send_frame(payload)
        except Exception as exc:
            with self._lock:
                claimed = self._pending_turns.pop(request_id, None) is not None
                self._request_hosts.pop(request_id, None)
            if not claimed:
                # The host's exit won the race and already settled this turn (turn.error): one
                # completion only, and no dispatch error that would also start it inline.
                return request_id
            if on_complete is not None:
                on_complete({"type": "turn.error", "sid": sid, "request_id": request_id,
                             "reason": "send_failed", "message": str(exc)})
            raise
        return request_id

    def interrupt(self, sid: str, *, request_id: str | None = None, turn_id: str | None = None) -> None:
        """Stop the session in the host (Stop semantics: its queue generation advances). With ``turn_id``, only
        interrupt that turn's agent if it is still the one running (busy input): the queue is left alone."""
        self.start()
        self._send_frame({"type": "interrupt", "sid": sid, "request_id": request_id or uuid.uuid4().hex,
                          **({"turn_id": turn_id} if turn_id else {})})

    def _await_reply(self, frame: dict[str, Any], request_id: str, timeout: float,
                     on_late: Callable[[dict], None] | None = None) -> dict:
        """Send ``frame`` and block for the host reply carrying ``request_id``. At the deadline the
        waiter becomes ``on_late`` (when given) in the same lock hold that retires the queue, so a
        reply or a host exit landing at that moment is never lost between the two, and the request
        keeps its owning host (see _wait_for_exit)."""
        q: queue.Queue[dict] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending_controls[request_id] = q
            try:
                self._send_frame(frame)
            except BaseException:
                self._pending_controls.pop(request_id, None)
                raise
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending_controls.pop(request_id, None)
                try:
                    return q.get_nowait()  # it landed at the deadline (a reply, or the host-exit error)
                except queue.Empty:
                    pass
                if on_late is None:
                    self._request_hosts.pop(request_id, None)
                else:
                    self._register_late_control_handler_locked(request_id, on_late)
            raise
        finally:
            with self._lock:
                self._pending_controls.pop(request_id, None)

    def respond(self, sid: str, params: dict[str, Any], *, timeout: float = 15.0) -> dict:
        """Deliver an interactive prompt response to the host that owns it."""
        self.start()
        request_id = uuid.uuid4().hex
        frame = {"type": "respond", "sid": sid, "request_id": request_id, "params": dict(params)}
        return self._await_reply(frame, request_id, timeout)

    def reload_mcp(self, sid: str, *, request_id: str | None = None) -> dict:
        payload = {"type": "reload_mcp", "sid": sid, "request_id": request_id or uuid.uuid4().hex}
        return self.control(sid, route_name="reload.mcp", wait=True, payload=payload)

    def control(
        self, sid: str, *, route_name: str, payload: dict[str, Any] | None = None,
        wait: bool = True, timeout: float = 30.0, on_late_ack: Callable[[dict], None] | None = None,
    ) -> dict:
        """Send a control frame; with ``wait`` block up to ``timeout`` for its ack. ``on_late_ack``
        (only with ``wait``) keeps the request adoptable after the waiter gives up: the host's
        eventual ``control.ack``/``control.error``/``error`` fires it once (bounded by
        ``_LATE_CONTROL_TTL_SECS``/``_MAX``) instead of being dropped."""
        if route_name not in MUTATOR_ROUTE_TABLE:
            raise ValueError(f"unclassified host mutator route: {route_name}")
        self.start()
        payload = payload or {}
        request_id = str(payload.get("request_id") or uuid.uuid4().hex)
        frame = {"type": "control", **payload, "sid": sid, "route_name": route_name,
                 "request_id": request_id}
        if not wait:
            self._send_frame(frame)
            return {"status": "sent", "request_id": request_id}
        return self._await_reply(frame, request_id, timeout, on_late=on_late_ack)

    def _register_late_control_handler(self, request_id: str, handler: Callable[[dict], None]) -> None:
        with self._lock:
            self._register_late_control_handler_locked(request_id, handler)

    def _register_late_control_handler_locked(self, request_id: str, handler: Callable[[dict], None]) -> None:
        """Caller holds ``self._lock``."""
        now = time.monotonic()
        handlers = self._late_control_handlers
        for rid in [r for r, (at, _cb) in handlers.items() if now - at > _LATE_CONTROL_TTL_SECS]:
            handlers.pop(rid, None)
        while len(handlers) >= _LATE_CONTROL_MAX:
            handlers.pop(min(handlers, key=lambda rid: handlers[rid][0]), None)
        handlers[request_id] = (now, handler)

    def _deliver_control_frame(self, request_id: str, frame: dict[str, Any]) -> None:
        with self._lock:
            # Select and enqueue in one hold: the waiter's deadline retires its queue under this lock,
            # so a reply is either in the queue it rechecks or goes to the late handler -- never lost.
            q = self._pending_controls.get(request_id)
            late = None if q is not None else self._late_control_handlers.pop(request_id, None)
            if q is not None:
                with contextlib.suppress(queue.Full):
                    q.put_nowait(frame)
        if late is not None:
            _call_logged(late[1], frame, f"compute host late control ack handler failed (request_id={request_id})")

    def _spawn_locked(self, *, reason: str) -> None:
        if self._stopped_respawning:
            raise ComputeHostUnavailable(
                f"the isolated turn worker crashed {self.respawn_max} times within "
                f"{int(_RESPAWN_WINDOW_SECS)}s and is paused; the first message after "
                f"{int(self.respawn_blocked_for()) + 1}s starts it again")
        self._hello_event.clear()
        self._hello = {}
        self._last_hb_monotonic = 0.0
        env ={**hermes_subprocess_env(inherit_credentials=True), **os.environ, **(self.env or {})}
        env["HERMES_COMPUTE_HOST_HEARTBEAT_SECS"] = str(self.heartbeat_secs)
        env[OWNER_PID_ENV] = str(os.getpid())
        root = str(_repo_root())
        env.setdefault("PYTHONPATH", root)
        if root not in env["PYTHONPATH"].split(os.pathsep):
            env["PYTHONPATH"] = root + os.pathsep + env["PYTHONPATH"]
        # Windows: the whole host tree (venv launcher shim, worker, every tool process a turn starts)
        # lives in a kill-on-close job this process owns. taskkill /T walks LIVE parent links, so a
        # tool grandchild whose parent already exited (MSYS bash does this) outlived every host
        # kill, holding pipes and running on after the turn that started it was gone; and the
        # host itself outlived a dead backend by the parent-guard window. Closing the job ends all
        # of it; the kernel closes it when this process dies. No breakaway: MSYS takes any
        # breakaway the job allows (measured), which would let exactly those processes escape.
        from hermes_cli.local_runtime.processes import spawn_server
        # Lossy UTF-8 decode: a locale-mismatched byte must not raise inside the drain threads.
        proc, job = spawn_server(
            self.argv, cwd=str(self.cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
            start_new_session=True)
        try:
            # Everything from here owns the child: a failure anywhere (a monitor thread that will not
            # start included) must not leave it, its job or a registry entry behind.
            self._proc = proc
            self._spawned[proc] = (job, _write_handle(proc))
            for target, name in ((self._drain_stdout, "compute-host-stdout"),
                                 (self._drain_stderr, "compute-host-stderr"),
                                 (self._wait_for_exit, "compute-host-wait")):
                threading.Thread(target=target, args=(proc,), name=name, daemon=True).start()
            if not self._hello_event.wait(timeout=10.0):
                raise RuntimeError(f"compute host did not send hello; stderr={self._stderr_tail[-5:]}")
            self._validate_hello()
            self._persist_registry()
        except BaseException:
            # Nothing of a failed start may stay behind: not the process tree, not a "running" host,
            # not a registry entry. Detached first, so its exit is not a crash to respawn from.
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            self._terminate_process(proc)
            self._remove_registry()
            raise
        logger.info("compute host started pid=%s reason=%s", proc.pid, reason)

    def _validate_hello(self) -> None:
        hello = self._hello
        if not hello:
            raise RuntimeError("compute host missing hello")
        got_home = str(hello.get("hermes_home") or "")
        if got_home and got_home != self.expected_hermes_home:
            raise RuntimeError(
                f"compute host HERMES_HOME mismatch: {got_home} != {self.expected_hermes_home}")
        got_sha = str(hello.get("build_sha") or "")
        expected = self.expected_build_sha
        if expected != "unknown" and got_sha not in {"", "unknown", expected}:
            raise RuntimeError(f"compute host build mismatch: {got_sha} != {expected}")

    def _persist_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        payload = {"host_pid": self.pid, "boot_id": self._hello.get("boot_id") or "",
                   "build_sha": self._hello.get("build_sha") or "", "started_at": time.time(),
                   "argv": self.argv}
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(self.registry_path)

    def _remove_registry(self) -> None:
        try:
            self.registry_path.unlink(missing_ok=True)
        except Exception:
            logger.debug("failed to remove compute host registry", exc_info=True)

    def _send_frame(self, frame: dict[str, Any]) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise RuntimeError("compute host is not running")
            proc.stdin.write(json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n")
            proc.stdin.flush()
            if request_id := str(frame.get("request_id") or ""):
                self._request_hosts[request_id] = proc
            live = self._pending_turns.keys() | self._pending_controls.keys() | self._late_control_handlers.keys()
            for stale in [rid for rid in self._request_hosts if rid not in live]:
                del self._request_hosts[stale]

    def _drain_stdout(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("compute host emitted invalid json: %r", raw[:200])
                continue
            if isinstance(frame, dict):
                try:
                    self._handle_host_frame(frame)
                except Exception:
                    # INVARIANT: the drain thread must never die on a bad frame — a
                    # dead drain stops reading stdout and crashes the child.
                    logger.exception("compute-host frame handling failed; continuing drain")

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            if text := raw.rstrip("\n"):
                self._stderr_tail = (self._stderr_tail + [text])[-80:]
                logger.warning("compute host stderr: %s", text)

    def _handle_host_frame(self, frame: dict[str, Any]) -> None:
        ftype = str(frame.get("type") or "")
        request_id = str(frame.get("request_id") or "")
        if ftype in _CONTROL_REPLY_TYPES or (ftype == "error" and request_id):
            self._deliver_control_frame(request_id, frame)
        elif ftype == "hello":
            self._hello = dict(frame)
            self._last_hb_monotonic = time.monotonic()
            self._hello_event.set()
        elif ftype == "hb":
            self._last_progress_counter = int(frame.get("progress_counter") or self._last_progress_counter)
            self._last_hb_monotonic = time.monotonic()
            logger.debug("compute host heartbeat: %s", frame)
        elif ftype == "orphan":
            logger.warning("compute host reported orphan (parent gone): old_ppid=%s confirmations=%s pid=%s",
                           frame.get("old_ppid"), frame.get("confirmations"), self.pid)
        elif ftype == "rpc":
            if isinstance(frame.get("message"), dict):
                self.rpc_sink(frame["message"])
        elif ftype in ("turn.end", "turn.error"):
            with self._lock:
                pending = self._pending_turns.pop(request_id, None)
            if pending is not None and pending[1] is not None:
                _call_logged(pending[1], frame, "compute host turn completion callback failed")

    @staticmethod
    def _classify_exit(code: int) -> tuple[str, str]:
        # The message becomes the turn's error for the user. This process classifying the exit
        # proves the gateway is alive, so none of them may read as "the gateway is gone".
        again = "The gateway is running and restarts the worker; send your message again."
        if code == _ORPHAN_EXIT_CODE:
            return ("host_orphan_exit", f"The isolated turn worker exited believing the gateway was gone. {again}")
        if code == _STDIN_LOST_EXIT_CODE:
            return ("host_stdin_lost", f"The isolated turn worker lost its control pipe. {again}")
        if code == 0:
            return ("host_exit", f"The isolated turn worker exited (code 0). {again}")
        return ("crash", f"The isolated turn worker crashed (exit code {code}). {again}")

    def _release_host(self, proc: subprocess.Popen[str]) -> None:
        """End whatever that host left running (a tool grandchild, a worker whose shim died alone)."""
        with self._lock:
            job, _handle = self._spawned.pop(proc, (None, None))
        if job is not None:
            try:
                job.close()
            except Exception:
                logger.warning("closing the compute host job failed (pid=%s)", proc.pid, exc_info=True)

    def _stdin_write_end_state(self, proc: subprocess.Popen[str]) -> str:
        """Diagnostic for a host that lost its control pipe: was OUR end closed, or its fd reused?"""
        stdin = proc.stdin
        if stdin is None:
            return "no stdin pipe"
        if stdin.closed:
            return "closed by this process"
        try:
            fd = stdin.fileno()
            os.fstat(fd)
        except (OSError, ValueError) as exc:
            return f"fd invalid in this process ({exc})"
        with self._lock:
            recorded = self._spawned.get(proc, (None, None))[1]
        current = _write_handle(proc)
        if recorded is not None and current != recorded:
            return f"fd {fd} now maps to another handle ({recorded} -> {current}): closed and reused in this process"
        return f"intact in this process (fd {fd})"

    def _wait_for_exit(self, proc: subprocess.Popen[str]) -> None:
        code = proc.wait()
        stdin_state = self._stdin_write_end_state(proc) if code == _STDIN_LOST_EXIT_CODE else ""
        self._release_host(proc)
        if self._closing:
            return
        with self._lock:
            # Settle exactly what THIS host owned, in one hold: a replacement may already be running
            # (start() after poll() saw this one dead), and its turns must neither fail with our
            # reason nor be stranded because we returned early.
            current = self._proc is proc
            if current:
                self._proc = None
                # In this hold, before the empty slot is visible: a replacement publishes its own
                # registry under this lock, and a later removal would delete that one.
                self._remove_registry()

            def owned(rid: str) -> bool:  # an unrecorded request belongs to the host that was current
                return self._request_hosts.get(rid, proc if current else None) is proc

            turns = {rid: self._pending_turns.pop(rid) for rid in [r for r in self._pending_turns if owned(r)]}
            late = {rid: self._late_control_handlers.pop(rid)
                    for rid in [r for r in self._late_control_handlers if owned(r)]}
            controls = {rid: self._pending_controls.pop(rid)
                        for rid in [r for r in self._pending_controls if owned(r)]}
            for rid in (*turns, *late, *controls):
                self._request_hosts.pop(rid, None)
            reason, message = self._classify_exit(code)
            # A waiter still blocked on this host gets its terminal frame now, in this hold: its
            # deadline path checks the queue under the lock before it would turn into a late handler.
            for rid, q in controls.items():
                with contextlib.suppress(queue.Full):
                    q.put_nowait({"type": "control.error", "request_id": rid, "reason": reason, "message": message})
        if current:
            (logger.info if reason in ("host_exit", "host_orphan_exit") else logger.warning)(
                "compute host exited: code=%s reason=%s%s", code, reason,
                f" gateway stdin write end: {stdin_state}" if stdin_state else "")
        # Late control handlers are failed too: the host is gone whatever the reason, so no late
        # ack can arrive for host_exit / host_orphan_exit either.
        try:
            self._fail_pending_turns(reason=reason, message=message, turns=turns, late=late)
        finally:
            if current:  # failure reporting is best-effort; recovery is not
                self._maybe_respawn_after_crash()

    def _fail_pending_turns(self, *, reason: str, message: str, turns: dict | None = None,
                            late: dict | None = None) -> None:
        """Fail ``turns`` and ``late`` control waiters (default: every one pending)."""
        with self._lock:
            if turns is None:
                turns, self._pending_turns = self._pending_turns, {}
            if late is None:
                late, self._late_control_handlers = self._late_control_handlers, {}
        pending = turns
        failure = {"reason": reason, "message": message}
        for request_id, (sid, cb) in pending.items():
            # A raising sink (e.g. a broken client transport) must not strand the remaining turns,
            # the late-control drain or the respawn: the pending map is already emptied.
            _call_logged(self.rpc_sink, {"jsonrpc": "2.0", "method": "event",
                                         "params": {"type": "error", "session_id": sid, "payload": dict(failure)}},
                         "compute host error event delivery failed")
            if cb is not None:
                frame = {"type": "turn.error", "sid": sid, "request_id": request_id, **failure}
                _call_logged(cb, frame, "compute host error callback failed")
        # A crashed host never emits the late acks timed-out control waiters still expect; fail
        # them too so the client's "still running" notice can't hang.
        for request_id, (_registered_at, handler) in late.items():
            frame = {"type": "control.error", "request_id": request_id, **failure}
            _call_logged(handler, frame, "compute host late control error handler failed")

    def _maybe_respawn_after_crash(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return  # a replacement is already running: nothing to respawn, and never latch a live host
            self._restart_times = [t for t in self._restart_times if now - t <= _RESPAWN_WINDOW_SECS]
            if len(self._restart_times) >= self.respawn_max:
                self._stopped_respawning = True
                self._latched_at = now
                logger.error(
                    "compute host crash loop: max %s restarts per %.0fs reached; not respawning for %.0fs",
                    self.respawn_max, _RESPAWN_WINDOW_SECS, _LATCH_COOLDOWN_SECS)
                return
            self._restart_times.append(now)
        # Small bounded backoff; tests and first recovery stay quick.
        delay = min(5.0, 0.25 * (2 ** max(0, len(self._restart_times) - 1)))

        def _respawn() -> None:
            time.sleep(delay)
            with self._lock:
                if self._closing or self._stopped_respawning or self._proc is not None:
                    return
                try:
                    self._spawn_locked(reason="crash")
                except Exception:
                    logger.exception("compute host respawn failed")
        threading.Thread(target=_respawn, name="compute-host-respawn", daemon=True).start()

    _pid_matches_compute_host = staticmethod(is_compute_host_identity)

    def _terminate_pid(self, pid: int, *, timeout: float = _SHUTDOWN_TIMEOUT_SECS) -> None:
        if not _signal_pid(pid, signal.SIGTERM, "SIGTERM"):
            return
        deadline = time.monotonic() + timeout
        while _pid_alive(pid):
            if time.monotonic() >= deadline:
                # Windows has no SIGKILL (the taskkill fallback reaches here); SIGTERM there is TerminateProcess.
                _signal_pid(pid, getattr(signal, "SIGKILL", signal.SIGTERM), "SIGKILL")
                return
            time.sleep(0.05)

    def _heartbeat_watchdog_loop(self) -> None:
        """Recovery for a host ALIVE but no longer heart-beating (a half-finalized interpreter, or the
        whole-process freezes of 2026-09-14: every thread parked, heartbeat included). Off unless the
        timeout is > 0 (clamped >= 3x heartbeat)."""
        timeout = self._hb_timeout_secs
        if timeout <= 0:
            return
        poll = max(1.0, min(timeout / 2.0, 10.0))
        last_tick = time.monotonic()
        while not self._closing:
            time.sleep(poll)
            now = time.monotonic()
            unscheduled = now - last_tick > poll + max(poll, 5.0)
            last_tick = now
            if unscheduled:
                # This loop itself did not run (suspend/resume, or this process stalled): the silence it
                # would measure is not the host's. Give the host a full timeout from now.
                with self._lock:
                    if self._last_hb_monotonic > 0:
                        self._last_hb_monotonic = now
                continue
            self._watchdog_check_once(timeout)

    def _watchdog_check_once(self, timeout: float) -> None:
        with self._lock:
            proc = self._proc
            last = self._last_hb_monotonic
        if proc is None or proc.poll() is not None or last <= 0:
            return
        age = time.monotonic() - last
        if age < timeout:
            return
        # Reuse-proof identity guard: we hold this Popen handle, so
        # `self._proc is proc and proc.poll() is None` cannot be fooled by PID
        # reuse (no command-line lookup -- /proc//ps are absent on Windows).
        with self._lock:
            # A heartbeat that landed since the first look means the host is alive: recheck it here.
            if (self._proc is not proc or proc.poll() is not None
                    or time.monotonic() - self._last_hb_monotonic < timeout):
                return
            pid = proc.pid
        logger.error(
            "compute host heartbeat gap: no hb for %.1fs (timeout=%.1fs) pid=%s "
            "last_progress=%s stderr_tail=%r -- killing zombie host tree",
            age, timeout, pid, self._last_progress_counter, self._stderr_tail[-5:])
        self._kill_host_tree(proc)      # unblocks proc.wait() -> respawn

    def _kill_host_tree(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            contained = self._spawned.get(proc, (None, None))[0] is not None
        if contained:
            self._release_host(proc)
        else:
            self._kill_process_tree(proc.pid)

    def _kill_process_tree(self, pid: int) -> None:
        if sys.platform == "win32":
            try:
                r = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    check=False, capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode != 0:
                    logger.warning("taskkill pid=%s rc=%s out=%r err=%r", pid, r.returncode, r.stdout, r.stderr)
                    self._terminate_pid(pid)
            except subprocess.TimeoutExpired:
                logger.warning("taskkill pid=%s timed out; falling back", pid)
                self._terminate_pid(pid)
            except Exception:
                logger.debug("taskkill failed for pid=%s", pid, exc_info=True)
                self._terminate_pid(pid)
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)  # windows-footgun: ok — POSIX branch; child = session leader
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("killpg failed for pid=%s", pid, exc_info=True)
            self._terminate_pid(pid)

    def _terminate_process(self, proc: subprocess.Popen[str]) -> None:
        self._release_host(proc)  # Windows: the whole tree, not only the launcher shim
        if proc.poll() is not None:
            return
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECS)
            return
        for step in (proc.kill, lambda: proc.wait(timeout=2)):
            with contextlib.suppress(Exception):
                step()


__all__ = ["MUTATOR_ROUTE_TABLE", "ComputeHostUnavailable", "HostSupervisor", "append_log_record",
           "is_compute_host_identity"]
