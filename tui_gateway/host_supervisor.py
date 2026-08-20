"""Supervisor for the dashboard compute-host child process.

The dashboard process owns sockets and JSON-RPC dispatch.  When
``dashboard.turn_isolation`` is enabled, agent turns move behind one persistent
``python -m tui_gateway.compute_host`` child so compute-heavy agent threads do
not contend with the serving process' event loop for the same GIL.
"""

from __future__ import annotations

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
from tui_gateway.relay_buffer import RelayBuffer

logger = logging.getLogger(__name__)
_Thread = threading.Thread

MUTATOR_ROUTE_TABLE: dict[str, str] = {
    "prompt.submit": "turn-path",
    "session.interrupt": "turn-path",
    "reload.mcp": "run-concurrent",
    "session.save": "run-concurrent",
    "session.compress": "idle-gated",
    "prompt.submit.truncate": "idle-gated",
    "slash.model": "idle-gated",
    "slash.personality": "idle-gated",
    "slash.prompt": "idle-gated",
    "slash.compress": "idle-gated",
    "session.reset": "idle-gated",
    "session.history.reload": "idle-gated",
    "slash.retry": "idle-gated",
}

_REGISTRY_NAME = "dashboard-compute-host.json"
_RESPAWN_WINDOW_SECS = 300.0
_SHUTDOWN_TIMEOUT_SECS = 10.0
_ORPHAN_EXIT_CODE = 18  # must match compute_host._ORPHAN_EXIT_CODE


def append_log_record(path: str | Path, record: str) -> None:
    """Append one log record using O_APPEND and exactly one os.write call."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = record if record.endswith("\n") else f"{record}\n"
    data = text.encode("utf-8", errors="replace")
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _build_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_repo_root()),
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:
        return "unknown"


def _default_registry_path() -> Path:
    return get_hermes_home() / "state" / _REGISTRY_NAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _pid_command(pid: int) -> str:
    if pid <= 0:
        return ""
    # Linux fast path.
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        data = proc_cmdline.read_bytes()
        if data:
            return data.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).strip()
    except Exception:
        return ""


def is_compute_host_identity(pid: int) -> bool:
    cmd = _pid_command(pid)
    return "tui_gateway.compute_host" in cmd


class _SupervisorRelayDelivery:
    """Adapts HostSupervisor methods to the RelayDelivery Protocol (names differ):
    the supervisor has ``rpc_sink`` (matches) but ``_complete_turn`` /
    ``_fail_session_turns``, not the Protocol's ``complete_turn`` / ``fail_session``."""

    def __init__(self, sup: "HostSupervisor") -> None:
        self._sup = sup

    def rpc_sink(self, message: dict) -> None:
        self._sup.rpc_sink(message)

    def complete_turn(self, frame: dict) -> None:
        self._sup._complete_turn(frame)

    def fail_session(self, sid: str, reason: str, message: str) -> None:
        self._sup._fail_session_turns(sid, reason, message)


class HostSupervisor:
    """Own one persistent compute-host child and relay its frames."""

    def __init__(
        self,
        *,
        registry_path: str | Path | None = None,
        argv: list[str] | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        rpc_sink: Callable[[dict], None] | None = None,
        respawn_max: int = 3,
        heartbeat_secs: int = 15,
        expected_build_sha: str | None = None,
        expected_hermes_home: str | None = None,
        autostart: bool = True,
    ) -> None:
        self.registry_path = Path(registry_path) if registry_path is not None else _default_registry_path()
        self.argv = argv or [sys.executable, "-m", "tui_gateway.compute_host"]
        self.cwd = Path(cwd) if cwd is not None else _repo_root()
        self.env = env
        self.rpc_sink = rpc_sink or (lambda _obj: None)
        self.respawn_max = max(0, int(respawn_max))
        self.heartbeat_secs = max(1, int(heartbeat_secs))
        self.expected_build_sha = expected_build_sha if expected_build_sha is not None else _build_sha()
        self.expected_hermes_home = expected_hermes_home if expected_hermes_home is not None else str(get_hermes_home())

        self._lock = threading.RLock()
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None
        self._hello_event = threading.Event()
        self._hello: dict[str, Any] = {}
        self._closing = False
        self._stopped_respawning = False
        self._restart_times: list[float] = []
        self._pending_turns: dict[str, tuple[str, Callable[[dict], None] | None]] = {}
        self._pending_controls: dict[str, queue.Queue[dict]] = {}
        self._stderr_tail: list[str] = []
        self._last_progress_counter = 0
        self._last_hb_monotonic: float = 0.0
        try:
            _cfg = float(os.environ.get("HERMES_HOST_HEARTBEAT_TIMEOUT_SECS") or "0")
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

        # H1: off-drain-thread WS delivery. Constructed BEFORE start()/autostart so
        # the drain thread always has a relay to enqueue into. The supervisor is
        # adapted to the RelayDelivery Protocol (its method names differ).
        self._relay = RelayBuffer(_SupervisorRelayDelivery(self))

        if self._hb_timeout_secs > 0:
            _Thread(target=self._heartbeat_watchdog_loop,
                    name="compute-host-hb-watchdog", daemon=True).start()
        if autostart:
            try:
                self.start()
            except Exception:
                # Startup failed → the caller gets no object to shut down, so the
                # threads we already started (relay + opt-in watchdog) would leak.
                # Tear them down before re-raising: _closing stops the watchdog loop
                # (`while not self._closing`) and relay.stop() joins the relay thread.
                self._closing = True
                try:
                    self._relay.stop()
                except Exception:
                    logger.exception("relay stop during failed startup failed")
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

    def start(self) -> None:
        with self._lock:
            if self.is_running():
                return
            self._closing = False
            self.reconcile_startup_orphan()
            self._spawn_locked(reason="startup")

    def shutdown(self) -> None:
        with self._lock:
            self._closing = True
            proc = self._proc
        # H4/R4: registry removal AND relay stop must run even on the proc-is-None
        # path, so both live in an OUTER finally (the old early-return skipped them).
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
            self._relay.stop()

    def reconcile_startup_orphan(self) -> str:
        """Terminate a stale registered host, guarding against PID reuse."""
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "none"
        except Exception:
            self._remove_registry()
            return "invalid-registry"

        try:
            pid = int(data.get("host_pid") or 0)
        except Exception:
            pid = 0
        if pid <= 0 or not _pid_alive(pid):
            self._remove_registry()
            return "not-running"
        if not self._pid_matches_compute_host(pid):
            # PID was reused by another process. Never signal it.
            self._remove_registry()
            return "pid-reuse-ignored"

        self._terminate_pid(pid, timeout=_SHUTDOWN_TIMEOUT_SECS)
        self._remove_registry()
        return "terminated"

    def submit_turn(
        self,
        frame: dict[str, Any],
        *,
        on_complete: Callable[[dict], None] | None = None,
    ) -> str:
        self.start()
        request_id = str(frame.get("request_id") or uuid.uuid4().hex)
        sid = str(frame.get("sid") or "")
        payload = dict(frame)
        payload["type"] = "turn.start"
        payload["request_id"] = request_id
        with self._lock:
            self._pending_turns[request_id] = (sid, on_complete)
        try:
            self._send_frame(payload)
        except Exception as exc:
            with self._lock:
                self._pending_turns.pop(request_id, None)
            err = {
                "type": "turn.error",
                "sid": sid,
                "request_id": request_id,
                "reason": "send_failed",
                "message": str(exc),
            }
            if on_complete is not None:
                on_complete(err)
            raise
        return request_id

    def interrupt(self, sid: str, *, request_id: str | None = None) -> None:
        self.start()
        self._send_frame({"type": "interrupt", "sid": sid, "request_id": request_id or uuid.uuid4().hex})

    def reload_mcp(self, sid: str, *, request_id: str | None = None) -> dict:
        return self.control(
            sid,
            route_name="reload.mcp",
            payload={"type": "reload_mcp", "sid": sid, "request_id": request_id or uuid.uuid4().hex},
            wait=True,
        )

    def control(
        self,
        sid: str,
        *,
        route_name: str,
        payload: dict[str, Any] | None = None,
        wait: bool = True,
        timeout: float = 30.0,
    ) -> dict:
        if route_name not in MUTATOR_ROUTE_TABLE:
            raise ValueError(f"unclassified host mutator route: {route_name}")
        self.start()
        request_id = str((payload or {}).get("request_id") or uuid.uuid4().hex)
        frame = dict(payload or {})
        frame.setdefault("type", "control")
        frame["sid"] = sid
        frame["route_name"] = route_name
        frame["request_id"] = request_id
        q: queue.Queue[dict] | None = None
        if wait:
            q = queue.Queue(maxsize=1)
            with self._lock:
                self._pending_controls[request_id] = q
        self._send_frame(frame)
        if not wait or q is None:
            return {"status": "sent", "request_id": request_id}
        try:
            return q.get(timeout=timeout)
        finally:
            with self._lock:
                self._pending_controls.pop(request_id, None)

    def _spawn_locked(self, *, reason: str) -> None:
        if self._stopped_respawning:
            raise RuntimeError("compute host respawn disabled after crash loop")
        self._hello_event.clear()
        self._hello = {}
        self._last_hb_monotonic = 0.0   # a dead host's timestamp must not arm the watchdog against the fresh one
        env = hermes_subprocess_env(inherit_credentials=True)
        env.update(os.environ)
        if self.env:
            env.update(self.env)
        env["HERMES_COMPUTE_HOST_HEARTBEAT_SECS"] = str(self.heartbeat_secs)
        env.setdefault("PYTHONPATH", str(_repo_root()))
        if str(_repo_root()) not in env["PYTHONPATH"].split(os.pathsep):
            env["PYTHONPATH"] = str(_repo_root()) + os.pathsep + env["PYTHONPATH"]
        proc = subprocess.Popen(
            self.argv,
            cwd=str(self.cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Lossy UTF-8 decode — the compute host emits UTF-8; a
            # locale-mismatched byte must not raise inside the drain
            # threads and kill the supervisor (#52649).
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        self._proc = proc
        self._stdout_thread = _Thread(target=self._drain_stdout, args=(proc,), name="compute-host-stdout", daemon=True)
        self._stderr_thread = _Thread(target=self._drain_stderr, args=(proc,), name="compute-host-stderr", daemon=True)
        self._wait_thread = _Thread(target=self._wait_for_exit, args=(proc,), name="compute-host-wait", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._wait_thread.start()
        if not self._hello_event.wait(timeout=10.0):
            self._terminate_process(proc)
            raise RuntimeError(f"compute host did not send hello; stderr={self._stderr_tail[-5:]}")
        self._validate_hello()
        self._persist_registry()
        logger.info("compute host started pid=%s reason=%s", proc.pid, reason)

    def _validate_hello(self) -> None:
        hello = self._hello
        if not hello:
            raise RuntimeError("compute host missing hello")
        got_home = str(hello.get("hermes_home") or "")
        if got_home and got_home != self.expected_hermes_home:
            raise RuntimeError(f"compute host HERMES_HOME mismatch: {got_home} != {self.expected_hermes_home}")
        got_sha = str(hello.get("build_sha") or "")
        if self.expected_build_sha != "unknown" and got_sha not in {"", "unknown", self.expected_build_sha}:
            raise RuntimeError(f"compute host build mismatch: {got_sha} != {self.expected_build_sha}")

    def _persist_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        payload = {
            "host_pid": self.pid,
            "boot_id": self._hello.get("boot_id") or "",
            "build_sha": self._hello.get("build_sha") or "",
            "started_at": time.time(),
            "argv": self.argv,
        }
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(self.registry_path)

    def _remove_registry(self) -> None:
        try:
            self.registry_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            logger.debug("failed to remove compute host registry", exc_info=True)

    def _send_frame(self, frame: dict[str, Any]) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise RuntimeError("compute host is not running")
            proc.stdin.write(json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n")
            proc.stdin.flush()

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
            text = raw.rstrip("\n")
            if text:
                self._stderr_tail = (self._stderr_tail + [text])[-80:]
                logger.warning("compute host stderr: %s", text)

    def _handle_host_frame(self, frame: dict[str, Any]) -> None:
        ftype = str(frame.get("type") or "")
        if ftype == "hello":
            self._hello = dict(frame)
            self._last_hb_monotonic = time.monotonic()
            self._hello_event.set()
            return
        if ftype == "hb":
            self._last_progress_counter = int(frame.get("progress_counter") or self._last_progress_counter)
            self._last_hb_monotonic = time.monotonic()
            logger.debug("compute host heartbeat: %s", frame)
            return
        if ftype == "orphan":
            logger.warning("compute host reported orphan (parent gone): old_ppid=%s confirmations=%s pid=%s",
                           frame.get("old_ppid"), frame.get("confirmations"), self.pid)
            return
        if ftype == "rpc":
            # H2: WS-bound → enqueue for the relay thread. NEVER call rpc_sink here —
            # a blocking/throwing WS write on the drain thread wedges stdout drain.
            self._relay.enqueue("rpc", frame)
            return
        if ftype in {"turn.end", "turn.error"}:
            # _complete_turn → on_complete emits message.complete / session.info
            # (WS-bound). Route through the relay so it never blocks the drain and
            # stays ordered behind this session's already-queued rpc frames.
            self._relay.enqueue("turn_complete", frame)
            return
        if ftype in {"control.ack", "control.error", "interrupt.ack", "reload_mcp.ack", "shutdown.ack"}:
            request_id = str(frame.get("request_id") or "")
            with self._lock:
                q = self._pending_controls.get(request_id)
            if q is not None:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass
            return
        if ftype == "error" and frame.get("request_id"):
            request_id = str(frame.get("request_id") or "")
            with self._lock:
                q = self._pending_controls.get(request_id)
            if q is not None:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass

    def _complete_turn(self, frame: dict[str, Any]) -> None:
        request_id = str(frame.get("request_id") or "")
        with self._lock:
            pending = self._pending_turns.pop(request_id, None)
        if pending is None:
            return
        _sid, cb = pending
        if cb is not None:
            try:
                cb(frame)
            except Exception:
                logger.exception("compute host turn completion callback failed")

    @staticmethod
    def _classify_exit(code: int) -> tuple[str, str]:
        if code == _ORPHAN_EXIT_CODE:
            return ("host_orphan_exit", "compute host self-exited: parent (gateway) confirmed gone")
        if code == 0:
            return ("host_exit", "compute host exited cleanly (code 0)")
        return ("crash", f"compute host exited with code {code}")

    def _wait_for_exit(self, proc: subprocess.Popen[str]) -> None:
        code = proc.wait()
        if self._closing:
            return
        with self._lock:
            if self._proc is not proc:
                return
            self._proc = None
        self._remove_registry()
        reason, message = self._classify_exit(code)  # KEEP: 18=orphan / 0=clean / else=crash
        (logger.warning if reason == "crash" else logger.info)(
            "compute host exited: code=%s reason=%s", code, reason)
        # R5: snapshot+clear BEFORE anything else so turns submitted to a fresh host
        # cannot be mis-failed.
        with self._lock:
            pending = self._pending_turns
            self._pending_turns = {}
        self._notify_failed_turns(pending, reason=reason, message=message)

    def _notify_failed_turns(
        self,
        pending: dict[str, tuple[str, Callable[[dict], None] | None]],
        *,
        reason: str,
        message: str,
    ) -> None:
        # R5 three-pass ordering (guarantees old-turn errors precede any fresh-host
        # frame on the WS, and no callback can stall recovery):
        # (2) enqueue ALL old-turn error WS events FIRST — non-blocking, ahead of any
        #     fresh-host frame in each session's relay FIFO.
        for request_id, (sid, cb) in pending.items():
            self._relay.enqueue(
                "rpc",
                {
                    "type": "rpc",
                    "sid": sid,
                    "message": {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "error",
                            "session_id": sid,
                            "payload": {"message": message, "reason": reason},
                        },
                    },
                },
            )
        # (3) schedule respawn BEFORE any (possibly blocking) callback can matter.
        self._maybe_respawn_after_crash()
        # (4) invoke completion callbacks after respawn is scheduled.
        for request_id, (sid, cb) in pending.items():
            if cb is not None:
                try:
                    cb({
                        "type": "turn.error",
                        "sid": sid,
                        "request_id": request_id,
                        "reason": reason,
                        "message": message,
                    })
                except Exception:
                    logger.exception("compute host error callback failed")

    def _fail_session_turns(self, sid: str, reason: str, message: str) -> None:
        """Fail one session's pending turns (relay-driven overflow / delivery failure).
        Serves as the RelayDelivery.fail_session sink via _SupervisorRelayDelivery."""
        with self._lock:
            matching = {rid: v for rid, v in self._pending_turns.items() if v[0] == sid}
            for rid in matching:
                self._pending_turns.pop(rid, None)
        for request_id, (msid, cb) in matching.items():
            if cb is not None:
                try:
                    cb({
                        "type": "turn.error",
                        "sid": msid,
                        "request_id": request_id,
                        "reason": reason,
                        "message": message,
                    })
                except Exception:
                    logger.exception("compute host session-fail callback failed")

    def _maybe_respawn_after_crash(self) -> None:
        now = time.monotonic()
        self._restart_times = [t for t in self._restart_times if now - t <= _RESPAWN_WINDOW_SECS]
        if len(self._restart_times) >= self.respawn_max:
            self._stopped_respawning = True
            logger.error("compute host crash loop: max %s restarts per 5min reached; not respawning", self.respawn_max)
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

        _Thread(target=_respawn, name="compute-host-respawn", daemon=True).start()

    def _pid_matches_compute_host(self, pid: int) -> bool:
        return is_compute_host_identity(pid)

    def _terminate_pid(self, pid: int, *, timeout: float = _SHUTDOWN_TIMEOUT_SECS) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("failed to SIGTERM compute host pid=%s", pid, exc_info=True)
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("failed to SIGKILL compute host pid=%s", pid, exc_info=True)

    def _heartbeat_watchdog_loop(self) -> None:
        """Opt-in recovery for a host ALIVE but no longer heart-beating (half-
        finalized interpreter: proc.wait() never returns -> crash/respawn starved).
        Off unless HERMES_HOST_HEARTBEAT_TIMEOUT_SECS > 0 (clamped >= 3x heartbeat)."""
        timeout = self._hb_timeout_secs
        if timeout <= 0:
            return
        poll = max(1.0, min(timeout / 2.0, 10.0))
        while not self._closing:
            time.sleep(poll)
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
            if self._proc is not proc or proc.poll() is not None:
                return
            pid = proc.pid
        logger.error(
            "compute host heartbeat gap: no hb for %.1fs (timeout=%.1fs) pid=%s "
            "last_progress=%s stderr_tail=%r -- killing zombie host tree",
            age, timeout, pid, self._last_progress_counter, self._stderr_tail[-5:],
        )
        self._kill_process_tree(pid)      # unblocks proc.wait() -> respawn

    def _kill_process_tree(self, pid: int) -> None:
        if sys.platform == "win32":
            try:
                r = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    check=False, capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
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
            os.killpg(os.getpgid(pid), signal.SIGKILL)   # child = session leader (start_new_session=True)
        except ProcessLookupError:
            return
        except Exception:
            logger.debug("killpg failed for pid=%s", pid, exc_info=True)
            self._terminate_pid(pid)

    def _terminate_process(self, proc: subprocess.Popen[str]) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=_SHUTDOWN_TIMEOUT_SECS)
            return
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


__all__ = [
    "MUTATOR_ROUTE_TABLE",
    "HostSupervisor",
    "append_log_record",
    "is_compute_host_identity",
]
