"""Per-process segmented log files — the Windows writer behind ``hermes_logging`` (gate H4).

Every Hermes process on Windows used to append to ONE ``agent.log`` through
concurrent-log-handler, whose cross-process lock (taken and released per record) let the single
queue-listener writer in each process fall ~30 min behind the clock under a 20–40 window convoy
(2026-10-28). Windows also refuses to rename a file another process holds open, which is why the
shared file needed that lock at all. Here a process owns its segment files outright: nothing is
shared, nothing is locked, nothing is renamed, and rollover just opens the next segment. Readers
merge segments by timestamp (``hermes_cli/logs_merge.py``).

Layout: ``<home>/logs/<stem>.d/<pid>-<start>.<seq>.log`` — ``<start>`` is the process start time
(PIDs are recycled), ``<seq>`` advances on size rollover.
"""

import logging
import math
import os
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

DEFAULT_RETENTION_DAYS = 7

from hermes_constants import mkdir_under_hermes_home

SEGMENT_RE = re.compile(r"^(?P<pid>\d+)-(?P<start>\d{8}-\d{6})\.(?P<seq>\d+)\.log$")

_IMPORT_TIME = time.time()
_OWNER: Optional[tuple[int, str]] = None


def _process_start_token(pid: int) -> str:
    try:
        import psutil
        started = psutil.Process(pid).create_time()
    except Exception:
        started = _IMPORT_TIME
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(started))


def process_owner() -> tuple[int, str]:
    """``(pid, start token)`` of this process — the segment-name prefix it owns."""
    global _OWNER
    pid = os.getpid()
    if _OWNER is None or _OWNER[0] != pid:
        _OWNER = (pid, _process_start_token(pid))
    return _OWNER


def segment_dir(stem_path) -> Path:
    """``…/logs/agent.log`` → ``…/logs/agent.d``."""
    stem_path = Path(stem_path)
    return stem_path.parent / f"{stem_path.stem}.d"


def prune_stale_segments(log_dir, *, retention_days: float, now: Optional[float] = None) -> list[Path]:
    """Unlink segments under ``<log_dir>/*.d/`` whose mtime is older than *retention_days*.

    Only names matching the segment pattern are candidates; legacy ``<stem>.log*`` files and
    anything else are never touched. A segment a live process still holds open cannot be
    unlinked on Windows (Python opens without ``FILE_SHARE_DELETE``), which is the liveness
    test — no process scan. ``retention_days <= 0`` (or a non-finite value) disables pruning.
    """
    removed: list[Path] = []
    if math.isnan(retention_days) or math.isinf(retention_days) or retention_days <= 0:
        return removed  # NaN / infinite / non-positive = never prune
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    try:
        dirs = [d for d in os.scandir(log_dir) if d.name.endswith(".d") and d.is_dir()]
    except OSError:
        return removed
    for d in dirs:
        try:
            files = list(os.scandir(d.path))
        except OSError:
            continue
        for f in files:
            if not SEGMENT_RE.match(f.name):
                continue
            try:
                if f.stat().st_mtime >= cutoff:
                    continue
                os.unlink(f.path)
            except OSError:
                continue  # held open by a live writer, or already gone
            removed.append(Path(f.path))
    return removed


class _SegmentedFileHandler(RotatingFileHandler):
    """A rotating handler whose files belong to this process alone.

    ``baseFilename`` stays the stem path (``…/logs/agent.log``) so handler identity, profile routing
    and home discovery in ``hermes_logging`` are unchanged; that path is never opened. *owner* is
    injectable so the contract is testable on any host.
    """

    def __init__(self, filename, maxBytes=0, backupCount=0, encoding=None, delay=True, *, owner=None):
        self._owner = tuple(owner) if owner else process_owner()
        self._dir = segment_dir(filename)
        # A handler rebuilt mid-process (profile routing) appends to the newest own segment.
        self._seq = self._highest_own_seq() or 1
        self._unavailable_reported = False
        self._last_emit_errored = False
        super().__init__(filename, maxBytes=maxBytes, backupCount=backupCount, encoding=encoding, delay=delay)

    def _own_prefix(self) -> str:
        return f"{self._owner[0]}-{self._owner[1]}."

    def _segment_path(self, seq: int) -> Path:
        return self._dir / f"{self._own_prefix()}{seq}.log"

    def _own_seqs(self) -> list[int]:
        prefix = self._own_prefix()
        try:
            names = os.listdir(self._dir)
        except OSError:
            return []
        seqs = []
        for name in names:
            match = SEGMENT_RE.match(name)
            if match and name.startswith(prefix):
                seqs.append(int(match.group("seq")))
        return seqs

    def _highest_own_seq(self) -> int:
        return max(self._own_seqs(), default=0)

    @property
    def current_segment(self) -> Path:
        return self._segment_path(self._seq)

    def _open(self):
        mkdir_under_hermes_home(self._dir)
        return open(self.current_segment, self.mode, encoding=self.encoding, errors=self.errors)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        self._seq += 1
        if self.backupCount > 0:
            # Keep ``backupCount`` finished segments behind the current one; never rename.
            for seq in self._own_seqs():
                if seq <= self._seq - self.backupCount - 1:
                    try:
                        os.unlink(self._segment_path(seq))
                    except OSError:
                        pass
        if not self.delay:
            self.stream = self._open()

    def emit(self, record: logging.LogRecord) -> None:
        self._last_emit_errored = False
        super().emit(record)
        if self.stream is not None:
            self._unavailable_reported = False
        if not self._last_emit_errored:
            from hermes_logging import _note_write_lag
            _note_write_lag(record)

    def handleError(self, record: logging.LogRecord) -> None:
        from hermes_logging import _handle_file_handler_error
        _handle_file_handler_error(self, record, path=str(self.current_segment))
