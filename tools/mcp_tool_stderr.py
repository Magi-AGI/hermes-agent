"""Bounded capture of stdio MCP servers' stderr into ``logs/mcp-stderr.log``.

Each server's stderr is a pipe, not the log file itself: a child that inherits the file handle
writes to it for as long as it (or any process it spawned) lives, so the file could never be rotated
on Windows and a looping server grew it without limit (a Unity bridge retrying ``connect ENOENT``
filled 101 MB). A drain thread only reads the pipe, tags lines with the server name and caps each
server's rate; it hands batches to a writer thread through a bounded queue and drops (and counts)
what does not fit, so neither a flood nor a slow disk ever stops the reads and backs the pipe up
into the server. The writer rotates the file by size and holds it only for the moment of a write;
profile shutdown closes the home's sinks and waits for an in-flight append, so the directory can be
renamed or deleted right after.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tools.mcp_tool")

LOG_NAME = "mcp-stderr.log"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3
# Per-server budget: a burst allowance refilled at a steady rate; everything past it is dropped.
_BURST_BYTES = 64 * 1024
_RATE_BYTES_PER_S = 8 * 1024
_READ_CHUNK = 64 * 1024

_append_lock = threading.Lock()


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.stat().st_size < _MAX_BYTES:
            return
    except OSError:
        return
    for index in range(_BACKUPS - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        if older.exists():
            try:
                os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
            except OSError:
                return  # another writer holds it this instant: rotate on a later write
    try:
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def append_to_log(path: Path, text: str) -> None:
    """Append *text*, rotating first when the file is over the cap. Never raises."""
    from hermes_constants import mkdir_under_hermes_home
    try:
        with _append_lock:
            mkdir_under_hermes_home(path.parent)  # never resurrects a profile deleted meanwhile
            _rotate_if_needed(path)
            with open(path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(text)
    except Exception:  # noqa: BLE001 -- a log write must never take the server's stderr drain down
        pass


# Batches waiting for the writer thread; past this the drain drops (and counts) instead of waiting.
_QUEUE_MAX = 256
# How long a profile shutdown waits for a sink's in-flight append before it gives up on it.
_CLOSE_JOIN_S = 5.0

_sinks_lock = threading.Lock()
_sinks: "dict[str, set]" = {}


class StderrSink:
    """One server's stderr pipe: a drain thread that only reads it, and a writer thread that does
    the disk I/O. The drain never waits on the log (a slow or locked append would otherwise stop
    the reads, fill the pipe and block the server's own stderr writes)."""

    def __init__(self, server_name: str, log_path: Path, scope: str):
        self.server_name = server_name
        self.log_path = log_path
        self.scope = scope
        read_fd, write_fd = os.pipe()
        self.writer = os.fdopen(write_fd, "w", encoding="utf-8", errors="replace")
        self._reader = os.fdopen(read_fd, "rb", buffering=0)
        self._tokens = float(_BURST_BYTES)
        self._refilled_at = time.monotonic()
        self._dropped = 0
        self._queue: "queue.Queue" = queue.Queue(maxsize=_QUEUE_MAX)
        self._closed = threading.Event()
        with _sinks_lock:
            _sinks.setdefault(scope, set()).add(self)
        self._enqueue(f"\n===== [{datetime.now():%Y-%m-%d %H:%M:%S}] starting MCP server '{server_name}' =====\n")
        self.writer_thread = threading.Thread(target=self._write_loop, name=f"mcp-stderr-log-{server_name}",
                                              daemon=True)
        self.writer_thread.start()
        self.thread = threading.Thread(target=self._drain, name=f"mcp-stderr-{server_name}", daemon=True)
        self.thread.start()

    def release_writer(self) -> None:
        """Drop this process's copy of the write end once the child holds its own, so the reader
        sees EOF when the server (and anything it spawned) is gone. Idempotent."""
        try:
            self.writer.close()
        except OSError:
            pass

    def close(self) -> bool:
        """Stop writing to the log and wait for an in-flight append to finish (profile shutdown:
        the directory may be renamed or deleted next). True when the writer has exited."""
        self._closed.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self.writer_thread.join(timeout=_CLOSE_JOIN_S)
        return not self.writer_thread.is_alive()

    def _admit(self, size: int) -> bool:
        now = time.monotonic()
        self._tokens = min(_BURST_BYTES, self._tokens + (now - self._refilled_at) * _RATE_BYTES_PER_S)
        self._refilled_at = now
        if size <= self._tokens:
            self._tokens -= size
            return True
        self._dropped += size
        return False

    def _enqueue(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            self._dropped += len(text)

    def _dropped_note(self) -> None:
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            self._enqueue(f"[{self.server_name}] ... {dropped} bytes of stderr dropped "
                          f"(over {_RATE_BYTES_PER_S // 1024} KB/s or the log fell behind) ...\n")

    def _drain(self) -> None:
        pending = b""
        try:
            while chunk := self._reader.read(_READ_CHUNK):
                *lines, pending = (pending + chunk).split(b"\n")
                if len(pending) > _READ_CHUNK:  # a runaway line without a newline
                    lines.append(pending)
                    pending = b""
                self._queue_lines([line for line in lines if self._admit(len(line) + 1)])
        except (OSError, ValueError):
            pass
        finally:
            if pending and self._admit(len(pending)):
                self._queue_lines([pending])
            self._dropped_note()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(None)
            with contextlib.suppress(OSError):
                self._reader.close()

    def _queue_lines(self, lines: list) -> None:
        if not lines:
            return
        if self._dropped and self._tokens >= _BURST_BYTES / 2:  # flow resumed: say what was lost
            self._dropped_note()
        prefix = f"[{self.server_name}] "
        self._enqueue("".join(prefix + line.decode("utf-8", "replace").rstrip("\r") + "\n" for line in lines))

    def _write_loop(self) -> None:
        try:
            while (text := self._queue.get()) is not None:
                if not self._closed.is_set():
                    append_to_log(self.log_path, text)
        finally:
            with _sinks_lock:
                live = _sinks.get(self.scope)
                if live is not None:
                    live.discard(self)
                    if not live:
                        del _sinks[self.scope]


def close_sinks(scope: Optional[str] = None) -> None:
    """Close every sink of profile home *scope* (all when None) and wait for their appends."""
    with _sinks_lock:
        targets = [sink for key, sinks in _sinks.items() if scope is None or key == scope for sink in sinks]
    for sink in targets:
        if not sink.close():
            logger.warning("MCP stderr log for '%s' did not close within %.0fs", sink.server_name, _CLOSE_JOIN_S)


def open_stderr_sink(server_name: str, home: Optional[Path] = None) -> StderrSink:
    """A sink logging under *home* (default: the calling profile's HERMES_HOME)."""
    from hermes_constants import get_hermes_home, hermes_home_key
    if home is None:
        home, scope = get_hermes_home(), hermes_home_key()
    else:
        scope = hermes_home_key(Path(home))
    return StderrSink(server_name, Path(home) / "logs" / LOG_NAME, scope)
