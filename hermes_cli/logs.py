"""``hermes logs`` — view and filter Hermes log files.

``hermes logs [name] [-n N] [-f] [--level L] [--session S] [--component C] [--since 1h]``;
``hermes logs list`` shows the available files. A log is read as one timeline merged from its
rotations, its live file and (Windows) its per-process segments — ``hermes_cli.logs_merge``.
"""

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence

from hermes_constants import get_hermes_home, display_hermes_home

# Known log files (name → filename)
LOG_FILES = {
    "agent": "agent.log",
    "errors": "errors.log",
    "gateway": "gateway.log",
    "gui": "gui.log",
    "desktop": "desktop.log",
    # Every stdio MCP subprocess's stderr (tools/mcp_tool.py redirects it
    # here, with per-server session markers) — the "MCP output channel".
    "mcp": "mcp-stderr.log",
}

# "2026-04-05 22:35:00[,123]" at the start of a line.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
# "agent.log.3": a rotated backup, merged into the timeline like the live file.
_ROTATION_NAME_RE = re.compile(r".+\.log\.\d+$")
_LEVEL_RE = re.compile(r"\s(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s")
# Logger name: the token before ":" after the level and optional "[session]" tag,
# e.g. "INFO gateway.run:" or "INFO [sess_abc] tools.terminal_tool:".
_LOGGER_NAME_RE = re.compile(r"\s(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)(?:\s+\[.*?\])?\s+(\S+):")
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


def _parse_since(since_str: str) -> Optional[datetime]:
    """Parse a relative time like '1h', '30m', '2d' into a cutoff; None if unparseable."""
    match = re.match(r"^(\d+)\s*([smhd])$", since_str.strip().lower())
    if not match:
        return None
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
    return datetime.now() - timedelta(**{unit: int(match.group(1))})


def _parse_line_timestamp(line: str) -> Optional[datetime]:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _extract_level(line: str) -> Optional[str]:
    m = _LEVEL_RE.search(line)
    return m.group(1) if m else None


def _extract_logger_name(line: str) -> Optional[str]:
    m = _LOGGER_NAME_RE.search(line)
    return m.group(1) if m else None


def _line_matches_component(line: str, prefixes: Sequence[str]) -> bool:
    name = _extract_logger_name(line)
    return name is not None and name.startswith(tuple(prefixes))


def _matches_filters(
    line: str,
    *,
    min_level: Optional[str] = None,
    session_filter: Optional[str] = None,
    since: Optional[datetime] = None,
    component_prefixes: Optional[Sequence[str]] = None,
) -> bool:
    """Whether a line passes all active filters (lines without a timestamp/level pass those)."""
    if since is not None:
        ts = _parse_line_timestamp(line)
        if ts is not None and ts < since:
            return False
    if min_level is not None:
        level = _extract_level(line)
        if level is not None and _LEVEL_ORDER.get(level, 0) < _LEVEL_ORDER.get(min_level, 0):
            return False
    if session_filter is not None and session_filter not in line:
        return False
    return component_prefixes is None or _line_matches_component(line, component_prefixes)


def tail_log(
    log_name: str = "agent",
    *,
    num_lines: int = 50,
    follow: bool = False,
    level: Optional[str] = None,
    session: Optional[str] = None,
    since: Optional[str] = None,
    component: Optional[str] = None,
) -> None:
    """Print the filtered tail of a log, optionally following in real time."""
    filename = LOG_FILES.get(log_name)
    if filename is None:
        print(f"Unknown log: {log_name!r}. Available: {', '.join(sorted(LOG_FILES))}")
        sys.exit(1)

    from hermes_cli.logs_merge import follow_merged, log_sources, read_merged_tail

    log_dir = get_hermes_home() / "logs"
    try:
        sources = log_sources(log_dir, filename)
    except PermissionError:
        print(f"Permission denied: {log_dir}")
        sys.exit(1)
    if not sources:
        if not follow:
            print(f"Log file not found: {log_dir / filename}")
            print("(Logs are created when Hermes runs — try 'hermes chat' first)")
            sys.exit(1)
        # A fresh Windows home has no file until the first record: wait for it.
        print(f"(no {filename} under {display_hermes_home()}/logs/ yet — waiting for the first write)")

    since_dt = None
    if since:
        since_dt = _parse_since(since)
        if since_dt is None:
            print(f"Invalid --since value: {since!r}. Use format like '1h', '30m', '2d'.")
            sys.exit(1)

    min_level = level.upper() if level else None
    if min_level and min_level not in _LEVEL_ORDER:
        print(f"Invalid --level: {level!r}. Use DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
        sys.exit(1)

    component_prefixes = None
    if component:
        from hermes_logging import COMPONENT_PREFIXES
        component_lower = component.lower()
        if component_lower not in COMPONENT_PREFIXES:
            available = ", ".join(sorted(COMPONENT_PREFIXES))
            print(f"Unknown component: {component!r}. Available: {available}")
            sys.exit(1)
        component_prefixes = COMPONENT_PREFIXES[component_lower]

    filters = dict(min_level=min_level, session_filter=session,
                   since=since_dt, component_prefixes=component_prefixes)
    has_filters = any(v is not None for v in filters.values())

    extents: dict = {}
    verdicts: dict = {}
    identities: dict = {}
    problems: list = []
    try:
        lines = read_merged_tail(log_dir, filename, num_lines, has_filters=has_filters, extents=extents,
                                 verdicts=verdicts, identities=identities, problems=problems, **filters)
    except PermissionError:
        print(f"Permission denied: {log_dir / filename}")
        sys.exit(1)
    for path, exc in problems:
        print(f"(skipped unreadable {path}: {exc})", file=sys.stderr)

    filter_parts = [
        f"{label}={value}" for label, value in
        (("level>", min_level), ("session", session), ("component", component), ("since", since))
        if value
    ]
    filter_desc = f" [{', '.join(filter_parts)}]" if filter_parts else ""
    mode = "Ctrl+C to stop" if follow else f"last {num_lines}"
    print(f"--- {display_hermes_home()}/logs/{filename}{filter_desc} ({mode}) ---")

    for line in lines:
        print(line, end="")

    if not follow:
        return
    try:
        follow_merged(log_dir, filename, lambda line: print(line, end="", flush=True),
                      start_positions=extents, start_passing=verdicts, start_identities=identities, **filters)
    except KeyboardInterrupt:
        print("\n--- stopped ---")


def _size_label(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _age_label(mtime: datetime) -> str:
    age_s = (datetime.now() - mtime).total_seconds()
    if age_s < 60:
        return "just now"
    if age_s < 3600:
        return f"{int(age_s / 60)}m ago"
    if age_s < 86400:
        return f"{int(age_s / 3600)}h ago"
    return mtime.strftime("%Y-%m-%d")


def list_logs() -> None:
    """Print available log files with sizes."""
    log_dir = get_hermes_home() / "logs"
    if not log_dir.exists():
        print(f"No logs directory at {display_hermes_home()}/logs/")
        return

    from hermes_logging_segments import SEGMENT_RE

    print(f"Log files in {display_hermes_home()}/logs/:\n")
    found = False
    for entry in sorted(log_dir.iterdir()):
        if entry.is_file() and (entry.suffix == ".log" or _ROTATION_NAME_RE.match(entry.name)):
            st = entry.stat()
            age_str = _age_label(datetime.fromtimestamp(st.st_mtime))
            print(f"  {entry.name:<25} {_size_label(st.st_size):>8}   {age_str}")
            found = True
        elif entry.is_dir() and entry.suffix == ".d":
            # Windows per-process segments: one row per log, summed. A segment pruned by another
            # process between the listing and the stat is simply not counted.
            stats = []
            for p in entry.iterdir():
                if not SEGMENT_RE.match(p.name):
                    continue
                try:
                    stats.append(p.stat())
                except OSError:
                    continue
            if not stats:
                continue
            age_str = _age_label(datetime.fromtimestamp(max(st.st_mtime for st in stats)))
            total = sum(st.st_size for st in stats)
            print(f"  {entry.name + '/':<25} {_size_label(total):>8}   {age_str}   "
                  f"({len(stats)} process segment{'s' if len(stats) != 1 else ''})")
            found = True

    if not found:
        print("  (no log files yet — run 'hermes chat' to generate logs)")
