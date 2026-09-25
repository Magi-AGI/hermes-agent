"""Bounded capture of stdio MCP servers' stderr into ``logs/mcp-stderr.log``.

Each server's stderr is a pipe drained by a reader thread, not the log file itself: a child that
inherits the file handle writes to it for as long as it (or any process it spawned) lives, so the
file could never be rotated on Windows and a looping server grew it without limit (a Unity bridge
retrying ``connect ENOENT`` filled 101 MB). The reader tags lines with the server name, caps each
server's rate (the excess is counted and reported, never buffered, so the pipe never backs up
into the server) and rotates the file by size. It holds the log only for the moment of a write,
so a profile directory can be renamed or deleted while servers run.
"""

from __future__ import annotations

import logging
import os
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


class StderrSink:
    """One server's stderr pipe plus the thread that drains it into the shared log."""

    def __init__(self, server_name: str, log_path: Path):
        self.server_name = server_name
        self.log_path = log_path
        read_fd, write_fd = os.pipe()
        self.writer = os.fdopen(write_fd, "w", encoding="utf-8", errors="replace")
        self._reader = os.fdopen(read_fd, "rb", buffering=0)
        self._tokens = float(_BURST_BYTES)
        self._refilled_at = time.monotonic()
        self._dropped = 0
        append_to_log(log_path, f"\n===== [{datetime.now():%Y-%m-%d %H:%M:%S}] starting MCP server "
                                f"'{server_name}' =====\n")
        self.thread = threading.Thread(target=self._drain, name=f"mcp-stderr-{server_name}", daemon=True)
        self.thread.start()

    def release_writer(self) -> None:
        """Drop this process's copy of the write end once the child holds its own, so the reader
        sees EOF when the server (and anything it spawned) is gone. Idempotent."""
        try:
            self.writer.close()
        except OSError:
            pass

    def _admit(self, size: int) -> bool:
        now = time.monotonic()
        self._tokens = min(_BURST_BYTES, self._tokens + (now - self._refilled_at) * _RATE_BYTES_PER_S)
        self._refilled_at = now
        if size <= self._tokens:
            self._tokens -= size
            return True
        self._dropped += size
        return False

    def _flush_dropped(self) -> None:
        if self._dropped:
            append_to_log(self.log_path, f"[{self.server_name}] ... {self._dropped} bytes of stderr "
                                         f"dropped (over {_RATE_BYTES_PER_S // 1024} KB/s) ...\n")
            self._dropped = 0

    def _drain(self) -> None:
        pending = b""
        try:
            while chunk := self._reader.read(_READ_CHUNK):
                *lines, pending = (pending + chunk).split(b"\n")
                if len(pending) > _READ_CHUNK:  # a runaway line without a newline
                    lines.append(pending)
                    pending = b""
                self._write([line for line in lines if self._admit(len(line) + 1)])
        except (OSError, ValueError):
            pass
        finally:
            if pending and self._admit(len(pending)):
                self._write([pending])
            self._flush_dropped()
            try:
                self._reader.close()
            except OSError:
                pass

    def _write(self, lines: list) -> None:
        if not lines:
            return
        prefix = f"[{self.server_name}] "
        if self._dropped and self._tokens >= _BURST_BYTES / 2:  # flow resumed: say what was lost
            self._flush_dropped()
        text = "".join(prefix + line.decode("utf-8", "replace").rstrip("\r") + "\n" for line in lines)
        append_to_log(self.log_path, text)


def open_stderr_sink(server_name: str, home: Optional[Path] = None) -> StderrSink:
    """A sink logging under *home* (default: the calling profile's HERMES_HOME)."""
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return StderrSink(server_name, Path(home) / "logs" / LOG_NAME)
