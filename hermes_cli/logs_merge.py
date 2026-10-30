"""Layout-agnostic readers for the Hermes log files (gate H4 writer layout).

A log stem (``agent``, ``errors``, ``gateway``, ``gui``) can be one shared rotating file — POSIX,
and every pre-H4 install — and/or per-process segments under ``<stem>.d/`` (Windows,
``hermes_logging_segments``). Every reader goes through here — ``hermes logs``, the dashboard
``/api/logs``, ``hermes debug share`` — and gets ONE timeline merged by record timestamp. A
record is a stamped line plus its unstamped continuation lines (tracebacks), so records from
different processes never interleave inside each other, and filters decide per record. Same-stamp
records keep source order (rotations, live file, segments by process start): deterministic, not
causal. Bounded reads spend ``READ_BUDGET_BYTES`` newest-first across the sources (a busy Windows
home holds hundreds of dead-process segments); a non-empty source left outside the budget is still
accounted for — its extent for a follow, a truncation flag for a snapshot.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from hermes_logging_segments import SEGMENT_RE, segment_dir

# ``%(asctime)s``: "2026-10-30 11:05:12,345"; the fraction is optional so hand-written or
# foreign lines still sort by the second.
_STAMP_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[,.](\d{1,6}))?")
_ROTATION_RE = re.compile(r"^\.(\d+)$")
_SMALL_FILE_BYTES = 1_048_576
READ_BUDGET_BYTES = 16 * 1_048_576
GROWTH_CHUNK_BYTES = 4 * 1_048_576
# Logs ``hermes_logging`` writes: every record starts with ``%(asctime)s``. Anything else
# (``desktop.log`` from Electron, ``mcp-stderr.log`` from subprocess stderr) is judged line by line.
STAMPED_LOGS = frozenset({"agent.log", "errors.log", "gateway.log", "gui.log"})


def stamped_by_format(filename: str) -> bool:
    """Whether *filename* (a stem file; its rotations and segments share it) is a stamped log."""
    return filename in STAMPED_LOGS


def _filename_of(path: Path) -> str:
    """The stem filename a source belongs to: ``agent.d/<seg>.log`` → ``agent.log``,
    ``agent.log.3`` → ``agent.log``, ``agent.log`` → itself."""
    if path.parent.name.endswith(".d") and SEGMENT_RE.match(path.name):
        return path.parent.name[:-2] + ".log"
    name = path.name
    while True:
        base, dot, tail = name.rpartition(".")
        if dot and tail.isdigit():
            name = base
            continue
        return name


def _stamp_key(line: str) -> Optional[tuple]:
    match = _STAMP_RE.match(line)
    if not match:
        return None
    fraction = (match.group(7) or "0").ljust(6, "0")
    return tuple(int(x) for x in match.groups()[:6]) + (int(fraction),)


def log_sources(log_dir, filename: str) -> list[Path]:
    """Existing files that make up *filename*'s timeline, oldest first: ``<filename>.N`` (largest
    N first), ``<filename>``, then ``<stem>.d/`` segments by process start, pid, seq."""
    log_dir = Path(log_dir)
    rotations = []
    for path in log_dir.glob(f"{filename}.*"):
        match = _ROTATION_RE.match(path.name[len(filename):])
        if match and path.is_file():
            rotations.append((int(match.group(1)), path))
    sources = [path for _, path in sorted(rotations, key=lambda item: -item[0])]
    live = log_dir / filename
    if live.is_file():
        sources.append(live)
    seg_dir = segment_dir(live)
    segments = []
    if seg_dir.is_dir():
        for path in seg_dir.iterdir():
            match = SEGMENT_RE.match(path.name)
            if match and path.is_file():
                segments.append(((match.group("start"), int(match.group("pid")), int(match.group("seq"))), path))
    sources.extend(path for _, path in sorted(segments))
    return sources


def _within_budget(sources: list[Path], per_source_cap: int) -> tuple[list[Path], dict[Path, int], list[Path]]:
    """Order the sources newest-first (by mtime, then source order — a copied directory has equal
    mtimes) for a read that spends ``READ_BUDGET_BYTES`` on bytes actually read, each source
    capped at *per_source_cap*. Returns ``(selected, dropped, order)``: *order* is the full
    newest-first read order; *selected*/*dropped* are the selection-time estimate from sizes seen
    now (what would fit if nothing grew) — advisory for callers that only want the shape; the
    readers decide what was really read and what was left outside the budget."""
    sized = []
    for index, path in enumerate(sources):
        try:
            st = path.stat()
        except OSError:
            continue
        sized.append((st.st_mtime, index, st.st_size, path))
    order = [path for _m, _i, _s, path in sorted(sized, key=lambda item: item[:2], reverse=True)]
    selected: set[Path] = set()
    spent = 0
    for _mtime, _index, size, path in sorted(sized, key=lambda item: item[:2], reverse=True):
        cost = min(size, per_source_cap)
        if selected and spent + cost > READ_BUDGET_BYTES:
            break
        selected.add(path)
        spent += cost
    dropped = {path: size for _m, _i, size, path in sized if path not in selected and size > 0}
    return [path for path in sources if path in selected], dropped, order


def _records(lines: Iterable[str], *, stamped: bool = True) -> list[tuple[tuple, list[str]]]:
    """Group lines into ``(stamp, lines)`` records. In a stamped log, unstamped lines continue the
    record before them; unstamped lines at the very start (a tail cut inside a traceback) form a
    stampless record that sorts before everything — they are the oldest lines that source still
    holds. An unstamped log (``mcp-stderr.log``) is one record per line, so per-line filters keep
    working for it; what a window happens to contain never changes that."""
    if not stamped:
        return [((), [line]) for line in lines]
    records: list[tuple[tuple, list[str]]] = []
    for line in lines:
        key = _stamp_key(line)
        if key is None and records:
            records[-1][1].append(line)
        else:
            records.append((key or (), [line]))
    return records


def _merge_records(per_source: list[list[str]], *, stamped: bool = True) -> list[list[str]]:
    """Every source's records in one timeline: by stamp, then source order, then file order."""
    keyed = []
    for index, lines in enumerate(per_source):
        for order, (key, record) in enumerate(_records(lines, stamped=stamped)):
            keyed.append((key, index, order, record))
    keyed.sort(key=lambda item: item[:3])
    return [record for _, _, _, record in keyed]


def _merge(per_source: list[list[str]], *, stamped: bool = True) -> list[str]:
    return [line for record in _merge_records(per_source, stamped=stamped) for line in record]


def _active(filters: dict, search: Optional[str]) -> bool:
    return search is not None or any(value is not None for value in filters.values())


def _record_matches(record: list[str], search: Optional[str], filters: dict, *, stamped_source: bool = True) -> bool:
    """Filters decide on the whole entry: level / session / component / since on its stamped
    header, free-text *search* anywhere in the entry. A headerless record of a stamped source
    (continuation lines cut off from their header) has nothing to vouch for it, so it fails every
    header filter and only *search* can select it; lines of an unstamped source are judged one by
    one exactly as a single-file tail always judged them."""
    from hermes_cli.logs import _matches_filters

    header = record[0]
    if stamped_source and _stamp_key(header) is None:
        if any(filters.get(key) is not None for key in ("min_level", "since", "session_filter", "component_prefixes")):
            return False
        return search is None or search.lower() in "".join(record).lower()
    if not _matches_filters(header, **filters):
        return False
    return search is None or search.lower() in "".join(record).lower()


def _complete_lines(raw: bytes) -> tuple[list[str], int]:
    """LF-normalised complete lines in *raw* and the byte length they span; a trailing partial
    line (no newline yet) is left out so a follow can finish it."""
    end = raw.rfind(b"\n")
    if end < 0:
        return [], 0
    complete = raw[: end + 1]
    text = complete.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return text.splitlines(keepends=True), len(complete)


def _read_backward(handle, size: int, *, want_lines: int, max_bytes: int, min_bytes: int = 0) -> tuple[bytes, int, bool]:
    """Read *handle* backwards from *size* in growing chunks until more than *want_lines*
    newlines are in hand AND at least *min_bytes* were read, the whole file is, or *max_bytes*
    have been read (the ceiling a bounded read is charged for — a file of giant records is never
    read whole to find one newline). Returns ``(raw, start_offset, hit_ceiling)``."""
    chunk = 65536
    pos = size
    parts: list[bytes] = []
    total = 0
    newlines = 0
    while pos > 0 and (newlines <= want_lines + 1 or total < min_bytes) and total < max_bytes:
        step = min(chunk, pos, max_bytes - total)
        pos -= step
        handle.seek(pos)
        part = handle.read(step)
        parts.insert(0, part)
        total += len(part)
        newlines += part.count(b"\n")
        chunk = min(chunk * 2, _SMALL_FILE_BYTES)
    return b"".join(parts), pos, pos > 0 and newlines <= want_lines + 1


def _identity(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _tail_lines(path: Path, num_lines: int, max_bytes: int = _SMALL_FILE_BYTES, meta: Optional[dict] = None) -> tuple[list[str], int]:
    """The last *num_lines* complete lines of *path* and the byte extent they end at (the offset
    a follow resumes from), reading at most about *max_bytes* from the end. Files within the
    ceiling are read whole; larger ones backwards in growing chunks, dropping the partial first
    line of the window. A final record bigger than the window is shown as the window's tail (its
    extent is the file end); a window holding only an UNFINISHED line reports the extent at the
    window start, inside that line, so a follow still emits it once its newline lands. *meta*,
    when given, receives ``identity`` and ``size`` from the same open handle as the extent."""
    with open(path, "rb") as handle:
        st = os.fstat(handle.fileno())
        size = st.st_size
        if meta is not None:
            meta["identity"] = (st.st_dev, st.st_ino)
            meta["size"] = size
        if size <= max_bytes:
            raw = handle.read(size)
            start = 0
        else:
            raw, start, _ = _read_backward(handle, size, want_lines=num_lines, max_bytes=max_bytes)
        if meta is not None:
            meta["read"] = len(raw)
        if size > max_bytes:
            if start > 0:
                cut = raw.find(b"\n")
                if cut < 0:
                    return [], start  # one unfinished giant line fills the window: resume inside it
                if cut == len(raw) - 1:
                    # The final complete record is bigger than the window: show its tail, bounded.
                    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
                    return [text[-(max_bytes + 1):]], start + len(raw)
                raw = raw[cut + 1:]
                start += cut + 1
    lines, span = _complete_lines(raw)
    return lines[-num_lines:], start + span


def read_merged_tail(
    log_dir, filename: str, num_lines: int, *, has_filters: bool = False, search: Optional[str] = None,
    extents: Optional[dict] = None, verdicts: Optional[dict] = None, problems: Optional[list] = None,
    identities: Optional[dict] = None, **filters,
) -> list[str]:
    """The last *num_lines* lines of the merged timeline (``<= 0`` → none: the "-n 0 -f" idiom).
    *filters* are ``_matches_filters`` kwargs and *search* a case-insensitive substring; both select
    whole entries (header + continuation lines), with the same over-read as a single file. The line
    count is still applied to lines, so the first entry shown may be a partial one — exactly as a
    single-file tail always behaved. *extents*, when given, receives ``{source: byte offset consumed}``
    for EVERY existing source (a source outside the read budget, or one that failed to open, at its
    size; ``None`` when even its size is unknown) so a follow resumes losslessly, and *identities*
    the ``(dev, ino)`` of each — taken from the same open handle as the extent — so the follow can
    tell a rotation from growth; *verdicts* receives ``{source: whether its last entry passed
    the filters}`` for a stamped log so the follow can finish that entry's continuation lines;
    *problems* receives ``(source, error)`` for sources that could not be read. Raises
    ``PermissionError`` only when no source could be read and at least one refused."""
    filtering = has_filters or _active(filters, search)
    stamped = stamped_by_format(filename)
    budget = max(num_lines, 1) if not filtering else max(num_lines * 20, 2000)
    sources = log_sources(log_dir, filename)
    _selected, _dropped, order = _within_budget(sources, _SMALL_FILE_BYTES)
    read_lines: dict[Path, list[str]] = {}
    denied: Optional[PermissionError] = None
    remaining = READ_BUDGET_BYTES
    for path in order:
        meta: dict = {}
        if remaining <= 0:
            # The aggregate is spent on bytes actually read: account for the rest, never open it.
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            if extents is not None:
                extents[path] = size
            if identities is not None:
                identities[path] = _identity(path)
            continue
        try:
            lines, extent = _tail_lines(path, budget, max_bytes=min(_SMALL_FILE_BYTES, remaining), meta=meta)
            remaining -= meta.get("read", 0)
        except OSError as exc:
            if isinstance(exc, PermissionError):
                denied = exc
            if problems is not None:
                problems.append((path, exc))
            if extents is not None:
                try:
                    extents[path] = path.stat().st_size
                except OSError:
                    extents[path] = None  # unknown: the follow starts at the end, never at 0
            if identities is not None:
                identities[path] = _identity(path)
            continue
        if extents is not None:
            extents[path] = extent
        if identities is not None:
            identities[path] = meta.get("identity")
        read_lines[path] = lines
    # Back to source order for the merge (rotations oldest-first, live file, segments by start).
    per_source: list[tuple[Path, list[str]]] = [(path, read_lines[path]) for path in sources if path in read_lines]
    if denied is not None and not per_source:
        raise denied
    if verdicts is not None and stamped:
        # The follow inherits a verdict across continuation lines of a stamped log only.
        for path, lines in per_source:
            tail = _records(lines)
            if tail:
                verdicts[path] = _record_matches(tail[-1][1], search, filters) if filtering else True
    if filtering:
        kept: list[tuple[Path, list[str]]] = []
        for path, lines in per_source:
            kept.append((path, [line for key, record in _records(lines, stamped=stamped)
                               if _record_matches(record, search, filters, stamped_source=stamped)
                               for line in record]))
        per_source = kept
    flat = _merge([lines for _, lines in per_source], stamped=stamped)
    return flat[-num_lines:] if num_lines > 0 else []


def _read_growth(path: Path, position: int, max_bytes: int = GROWTH_CHUNK_BYTES) -> tuple[list[str], int]:
    """Complete lines appended to *path* since *position*, at most *max_bytes* per call →
    ``(lines, new_position)``; what did not fit is read on the next call. A trailing partial line
    stays unread until its newline arrives, unless it alone fills the whole chunk (one record
    larger than the chunk), in which case it is passed on as it is so the read keeps moving."""
    with open(path, "rb") as handle:
        handle.seek(position)
        chunk = handle.read(max_bytes)
    lines, span = _complete_lines(chunk)
    if span == 0 and len(chunk) >= max_bytes:
        text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return [text], position + len(chunk)
    return lines, position + span


def _open_shared_read(path: Path):
    """Open *path* for reading without blocking a rename of it. Python's ``open`` on Windows omits
    ``FILE_SHARE_DELETE``, which would make a held handle refuse the very rotation it exists to
    follow (and pin the file against anything else); POSIX needs nothing special."""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    GENERIC_READ = 0x80000000
    SHARE_ALL = 0x1 | 0x2 | 0x4  # read | write | delete
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle == INVALID_HANDLE_VALUE or handle is None:
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {path}")
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    return os.fdopen(fd, "rb")


class _LiveFile:
    """The live stem file (``<log_dir>/<filename>``) followed through ONE held handle.

    Everything else a follow watches is a per-process segment or a foreign file that is never
    renamed, so polling its size is enough. The live stem file on POSIX is rotated by rename, and
    a handle is the only thing that follows the bytes wherever the name goes: the lines the old
    file received after the last poll are read through the handle (even across two rollovers in
    one poll), then the new file at the name is opened at 0. Identity ``(dev, ino)`` of the handle
    vs the name says when that happened. A partial trailing line is buffered until its newline;
    one bigger than ``GROWTH_CHUNK_BYTES`` is passed on as it is so the read keeps moving.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None
        self.buffer = b""
        self.identity: Optional[tuple] = None

    def open(self, at: int) -> bool:
        self.close()
        try:
            handle = _open_shared_read(self.path)
            st = os.fstat(handle.fileno())
            handle.seek(min(at, st.st_size))
        except OSError:
            return False
        self.handle = handle
        self.identity = (st.st_dev, st.st_ino)
        self.buffer = b""
        return True

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
        self.handle = None
        self.buffer = b""

    def read_lines(self) -> list[str]:
        """Complete lines newly available through the handle (bounded per call)."""
        if self.handle is None:
            return []
        try:
            st = os.fstat(self.handle.fileno())
            if st.st_size < self.handle.tell():
                self.handle.seek(0)  # truncated in place
                self.buffer = b""
            chunk = self.handle.read(GROWTH_CHUNK_BYTES)
        except OSError:
            return []
        if not chunk:
            return []
        data = self.buffer + chunk
        lines, span = _complete_lines(data)
        self.buffer = data[span:]
        if span == 0 and len(self.buffer) >= GROWTH_CHUNK_BYTES:
            text = self.buffer.decode("utf-8", errors="replace").replace("\r\n", "\n")
            self.buffer = b""
            return [text]
        return lines

    def rotated(self) -> bool:
        """The name now points at a different file (or none) than the handle."""
        return self.handle is not None and _identity(self.path) != self.identity

    def drain(self) -> list[str]:
        """Everything still unread through the handle, to its end."""
        out: list[str] = []
        while True:
            lines = self.read_lines()
            if not lines:
                break
            out.extend(lines)
        if self.buffer:
            out.append(self.buffer.decode("utf-8", errors="replace").replace("\r\n", "\n"))
            self.buffer = b""
        return out


def follow_merged(
    log_dir, filename: str, emit: Callable[[str], None], *,
    stop=None, poll_s: float = 0.3, rescan_s: float = 2.0, start_positions: Optional[dict] = None,
    start_passing: Optional[dict] = None, start_identities: Optional[dict] = None,
    search: Optional[str] = None, **filters,
) -> None:
    """Print lines appended to any source after the follow started, in arrival order.

    Per-process segments and foreign files are never renamed, so they are polled by size with no
    handle held (a busy Windows home holds hundreds within retention, and Windows refuses to
    unlink a file a handle holds open). The live stem file is the one file a POSIX writer rotates
    by rename, and it is followed through ONE held handle (``_LiveFile``): its unread suffix is
    read wherever the name went, then the replacement is opened at 0 — even when the replacement
    already outgrew the old offset, and even across two rollovers in one poll. *start_positions*
    (the extents an initial tail consumed) and *start_identities* make the handoff from
    ``read_merged_tail`` lossless: a source absent from them was born after the tail and is read
    from 0, one whose extent is unknown (``None``) is followed from its end, and a live file whose
    identity already changed since the tail has its old incarnation finished from ``<filename>.1``
    first. Filters decide on an entry's stamped header and its continuation lines inherit the
    verdict (*search* on the header, since lines arrive one at a time); under any filter,
    continuation lines of a header the follow never saw are dropped unless *start_passing* carries
    the tail's verdict for that source. An unstamped log (``mcp-stderr.log``, by the writer's
    format — never a guess from the lines seen) is judged line by line. Per-file order is kept;
    cross-file order is arrival order (recorded limit). *stop* is an optional ``threading.Event``.
    """
    log_dir = Path(log_dir)
    filtering = _active(filters, search)
    stamped = stamped_by_format(filename)
    live_path = log_dir / filename

    def _is_rotation(path: Path) -> bool:
        return (path.parent == log_dir and path.name.startswith(filename)
                and bool(_ROTATION_RE.match(path.name[len(filename):])))

    def _judge(path: Path, line: str) -> bool:
        if not stamped:
            passing[path] = _record_matches([line], search, filters, stamped_source=False)
        elif _stamp_key(line) is not None:
            passing[path] = _record_matches([line], search, filters)
        return passing.get(path, not filtering)

    def _emit_all(path: Path, lines: list[str]) -> None:
        for line in lines:
            if _judge(path, line):
                emit(line)

    def _start_offset(path: Path) -> Optional[int]:
        """Where to begin on *path* at follow start: the tail's extent, 0 for a source born after
        the tail, or the current end (``None`` extent, or no tail at all)."""
        if start_positions is None or start_positions.get(path, 0) is None:
            try:
                return path.stat().st_size
            except OSError:
                return None
        return start_positions.get(path, 0)

    passing: dict[Path, bool] = dict(start_passing or {})
    positions: dict[Path, int] = {}
    live = _LiveFile(live_path)

    def _rotations_by_age() -> list[tuple[int, Path]]:
        found = []
        for candidate in log_dir.glob(f"{filename}.*"):
            if _is_rotation(candidate):
                found.append((int(candidate.name[len(filename) + 1:]), candidate))
        return sorted(found)

    def _read_incarnations_between(old_identity: Optional[tuple]) -> None:
        """After the inode the handle followed was rotated to ``.k``, every rotation ``.k-1`` …
        ``.1`` is a later incarnation that lived and rolled over between two polls: read each
        whole, oldest first. If the followed inode is gone (beyond backupCount) nothing older
        than the live file is replayed."""
        rotations = _rotations_by_age()
        held_at = next((n for n, candidate in rotations if _identity(candidate) == old_identity), None)
        if held_at is None:
            return
        for n, candidate in reversed(rotations):
            if 0 < n < held_at:
                between = _LiveFile(candidate)
                if between.open(0):
                    _emit_all(live_path, between.drain())
                    between.close()

    def _open_live_at_start() -> None:
        at = _start_offset(live_path)
        if at is None or not live.open(at):
            return
        expected = (start_identities or {}).get(live_path)
        if expected is not None and live.identity != expected:
            # Rotated between the tail and now: finish the tail's incarnation from its new name,
            # then any incarnation that came and went in between, then start this one at 0.
            for candidate in log_dir.glob(f"{filename}.*"):
                if _is_rotation(candidate) and _identity(candidate) == expected:
                    old = _LiveFile(candidate)
                    if old.open(at):
                        _emit_all(live_path, old.drain())
                        old.close()
                    break
            _read_incarnations_between(expected)
            passing.pop(live_path, None)
            live.open(0)

    for path in log_sources(log_dir, filename):
        if _is_rotation(path):
            continue
        if path == live_path:
            _open_live_at_start()
            continue
        at = _start_offset(path)
        if at is not None:
            positions[path] = at

    last_scan = time.monotonic()
    try:
        while stop is None or not stop.is_set():
            progressed = False
            # The live stem file: through its handle, rotation-aware.
            if live.handle is not None:
                lines = live.read_lines()
                if lines:
                    progressed = True
                    _emit_all(live_path, lines)
                if live.rotated():
                    _emit_all(live_path, live.drain())
                    _read_incarnations_between(live.identity)
                    live.close()
                    passing.pop(live_path, None)
                    live.open(0)
                    progressed = True
            elif live_path.is_file():
                live.open(0)  # born (or recreated) after the follow started
            # Everything else: never renamed, polled by size.
            for path in list(positions):
                try:
                    size = path.stat().st_size
                except OSError:
                    del positions[path]
                    passing.pop(path, None)
                    continue
                position = positions[path]
                if size < position:
                    position = 0  # truncated underneath us
                    passing.pop(path, None)
                if size > position:
                    try:
                        lines, position = _read_growth(path, position)
                    except OSError:
                        continue
                    progressed = progressed or bool(lines)
                    _emit_all(path, lines)
                positions[path] = position
            now = time.monotonic()
            if now - last_scan >= rescan_s:
                last_scan = now
                for path in log_sources(log_dir, filename):
                    if path in positions or path == live_path or _is_rotation(path):
                        continue
                    positions[path] = 0  # born after the follow started: read from its start
            if not progressed:
                time.sleep(poll_s)
    finally:
        live.close()


def _tail_bytes(path: Path, max_bytes: int, tail_lines: int, allowance: Optional[int] = None) -> tuple[bytes, int, bool, int]:
    """Whole file, or its newest ``max_bytes`` cut on a line boundary (the partial first line is
    dropped only when the cut lands mid-line — decided by the byte before the window), widened up
    to 4× *max_bytes* while fewer than *tail_lines* lines came back, never beyond *allowance*
    (what this source may still spend of the aggregate budget); each byte is read once. A window
    holding no complete line keeps its last ``max_bytes`` bytes (one giant record). Returns
    ``(raw, size, cut, bytes_read)``; *cut* says leading bytes were dropped."""
    ceiling = 4 * max_bytes if allowance is None else min(4 * max_bytes, allowance)
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if ceiling <= 0:
            return b"", size, size > 0, 0
        if size <= min(max_bytes, ceiling):
            raw = handle.read(size)
            return raw, size, False, len(raw)
        window, start, _ = _read_backward(
            handle, size, want_lines=tail_lines, max_bytes=ceiling, min_bytes=min(max_bytes, ceiling))
        read = len(window)
        if start == 0:
            return window, size, False, read
        handle.seek(start - 1)
        on_boundary = handle.read(1) == b"\n"
        read += 1
    if on_boundary:
        raw = window
    else:
        newline = window.find(b"\n")
        raw = window[newline + 1:] if newline >= 0 else b""
    if not raw:
        raw = window[-max_bytes:]
    return raw, size, True, read


def read_merged_snapshot(sources: Iterable[Path], *, max_bytes: int, tail_lines: int) -> tuple[str, str, bool]:
    """One read of every source's tail, merged → ``(all_text, full_text, truncated)``.

    ``full_text`` is the newest ≤ *max_bytes* of the merged timeline cut on a line boundary;
    ``all_text`` is the merged over-read the summary tail is taken from (≥ *tail_lines* lines
    when the sources hold them); ``truncated`` is True only when bytes were actually dropped —
    from a source's head, from the merged text, or a whole non-empty source left outside the read
    budget — never because CRLF files shrank to LF.
    """
    ordered_sources = [Path(p) for p in sources]
    _selected, _dropped, order = _within_budget(ordered_sources, 4 * max_bytes)
    texts: dict[Path, list[str]] = {}
    cut_any = False
    remaining = READ_BUDGET_BYTES
    for path in order:
        if remaining <= 0:
            # The aggregate is spent: a non-empty source left unread is truncation.
            try:
                cut_any = cut_any or path.stat().st_size > 0
            except OSError:
                pass
            continue
        try:
            raw, _size, cut, read = _tail_bytes(Path(path), max_bytes, tail_lines, allowance=remaining)
        except OSError:
            continue
        remaining -= read
        cut_any = cut_any or cut
        # Text-mode writers on Windows end lines with CRLF; the merged snapshot is LF so the
        # summary tail and the upload read the same on every host.
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        texts[path] = text.splitlines(keepends=True)
    per_source = [texts[path] for path in ordered_sources if path in texts]
    stamped = stamped_by_format(_filename_of(ordered_sources[0])) if ordered_sources else True
    all_text = "".join(_merge(per_source, stamped=stamped))
    data = all_text.encode("utf-8")
    full_text = all_text
    over = len(data) > max_bytes
    if over:
        cut = len(data) - max_bytes
        chunk = data[cut:]
        if data[cut - 1:cut] != b"\n" and b"\n" in chunk:
            chunk = chunk.split(b"\n", 1)[1]
        if not chunk.strip():
            chunk = data[-max_bytes:]
        full_text = chunk.decode("utf-8", errors="replace")
    return all_text, full_text, cut_any or over
