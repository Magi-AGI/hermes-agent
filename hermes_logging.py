"""Centralized logging setup for Hermes Agent.

Log files: agent.log (INFO+, everything), errors.log (WARNING+), gateway.log (INFO+,
gateway components; ``mode="gateway"``), gui.log (INFO+, dashboard/TUI-gateway;
``mode="gui"``). All are rotating files driven through one async queue and formatted
with ``RedactingFormatter`` so secrets never reach disk. On Windows each process writes
its own segments under ``<stem>.d/`` instead of the shared file (``hermes_logging_segments``).
"""

import atexit
import copy
import io
import logging
import math
import os
import re
import queue
import sys
import threading
import time
from logging.handlers import BaseRotatingHandler, QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Optional, Sequence

# Windows: stdlib ``RotatingFileHandler.doRollover()`` renames, which fails with
# ``PermissionError [WinError 32]`` whenever another process holds a handle on the shared
# file — essentially always in Hermes (TUI, gateway, MCP servers, CLI commands all log). The
# former fix, concurrent-log-handler's cross-process lock (#44873), made every writer wait on
# every other one and put agent.log ~30 min behind the clock under convoys (gate H4). Windows
# now gets per-process segment files (``hermes_logging_segments``): nothing shared, nothing
# renamed. POSIX keeps stdlib on the shared file: renames of open files work, and managed mode
# (NixOS) relies on stdlib's exact ``_open()``/``doRollover()`` lifecycle for the 0660 chmod.

from hermes_constants import get_config_path, get_hermes_home, mkdir_under_hermes_home

# setup_logging() is idempotent: a second call is a no-op unless ``force=True``.
_logging_initialized = False

# Thread-local per-conversation session context.
_session_context = threading.local()

# ``%(session_tag)s`` exists on every LogRecord via _install_session_record_factory().
_LOG_FORMAT = "%(asctime)s %(levelname)s%(session_tag)s %(name)s: %(message)s"
_LOG_FORMAT_VERBOSE = "%(asctime)s - %(name)s - %(levelname)s%(session_tag)s - %(message)s"


def _safe_stderr():  # type: ignore[return]
    """Return a stderr stream that tolerates Unicode on all platforms.

    Wraps ``sys.stderr`` with ``errors='replace'`` so un-encodable characters become
    ``?`` instead of crashing the process.
    """
    stream = sys.stderr
    encoding = getattr(stream, "encoding", None) or "utf-8"
    if encoding.lower().replace("-", "") in ("utf8", "utf8surrogateescape"):
        return stream
    try:
        wrapped = io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        # Prevent the wrapper from closing the underlying buffer when garbage-collected.
        wrapped.close = lambda: None  # type: ignore[assignment]
        return wrapped
    except Exception:
        return stream  # best-effort: no buffer / wrapping failed -> original stream


def _is_unavailable_log_stream(exc: BaseException | None) -> bool:
    """True when a file handler lost its backing stream during teardown or I/O."""
    return (
        (isinstance(exc, OSError) and exc.errno == 5)
        or (isinstance(exc, ValueError) and "closed file" in str(exc).lower())
    )


# Third-party loggers that are noisy at DEBUG/INFO level.
_NOISY_LOGGERS = (
    "openai", "openai._base_client", "httpx", "httpcore", "asyncio", "hpack", "hpack.hpack",
    "grpc", "modal", "urllib3", "urllib3.connectionpool", "websockets", "charset_normalizer",
    "markdown_it",
)


def _quiet_noisy_loggers() -> None:
    """Pin noisy third-party loggers at WARNING."""
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def set_session_context(session_id: str) -> None:
    """Set the session ID for the current thread."""
    _session_context.session_id = session_id


def clear_session_context() -> None:
    """Clear the session ID for the current thread."""
    _session_context.session_id = None


def _install_session_record_factory() -> None:
    """Replace the global LogRecord factory with one that adds ``session_tag``.

    Unlike a Filter, the record factory runs for EVERY record in the process (propagated
    and third-party-handled ones included), so ``%(session_tag)s`` never KeyErrors.
    Idempotent via a marker attribute.
    """
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_hermes_session_injector", False):
        return

    def _session_record_factory(*args, **kwargs):
        record = current_factory(*args, **kwargs)
        sid = getattr(_session_context, "session_id", None)
        record.session_tag = f" [{sid}]" if sid else ""  # type: ignore[attr-defined]
        # QueueListener formats on its own thread, after the profile-scoped
        # ContextVar is gone; keep the resolved home on the record so a
        # multiplex desktop ticker can route to the job owner's files (#97489).
        try:
            record.hermes_home = str(get_hermes_home().resolve())  # type: ignore[attr-defined]
        except Exception:
            record.hermes_home = ""  # type: ignore[attr-defined]
        return record
    _session_record_factory._hermes_session_injector = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(_session_record_factory)


# Install on import so session_tag exists on all records even before setup_logging().
_install_session_record_factory()


class _ComponentFilter(logging.Filter):
    """Only pass records whose logger name starts with one of *prefixes*."""

    def __init__(self, prefixes: Sequence[str]) -> None:
        super().__init__()
        self._prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self._prefixes)


# Logger name prefixes per component; used by _ComponentFilter and ``hermes logs --component``.
COMPONENT_PREFIXES = {
    # ``plugins.platforms``: messaging adapters that migrated out of
    # ``gateway/platforms/`` into bundled plugins (#41112) are still gateway
    # components and belong in gateway.log.
    "gateway": ("gateway", "hermes_plugins", "plugins.platforms"),
    "agent": ("agent", "run_agent", "model_tools", "batch_runner"),
    "tools": ("tools",),
    "cli": ("hermes_cli", "cli"),
    "cron": ("cron",),
    "gui": ("hermes_cli.web_server", "hermes_cli.pty_bridge", "hermes_cli.desktop", "tui_gateway", "uvicorn"),
}


def _known_log_homes() -> set[Path]:
    """Homes the queued file handlers already serve: static handlers by their file, routers by
    their default home plus every profile home they route. Caller holds ``_queue_state_lock``."""
    homes: set[Path] = set()
    for handler in _queued_file_handlers:
        if isinstance(handler, _ProfileRoutingFileHandler):
            homes.add(handler._default_home)
            homes.update(handler._profile_homes)
        elif isinstance(handler, BaseRotatingHandler):
            try:
                homes.add(Path(handler.baseFilename).resolve().parent.parent)
            except (TypeError, ValueError, OSError):
                continue
    return homes


def _adopt_secondary_home(home: Path) -> bool:
    """Route *home*'s records to its own files when this process already logs for another home.
    Enables profile routing for the union of homes (or widens the live routers); False when
    *home* is the first home seen or is already served."""
    try:
        resolved = Path(home).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return False
    with _queue_state_lock:
        known = _known_log_homes()
    if not known or resolved in known:
        return False
    return enable_profile_log_routing([*sorted(known), resolved])


def setup_logging(
    *,
    hermes_home: Optional[Path] = None,
    log_level: Optional[str] = None,
    max_size_mb: Optional[int] = None,
    backup_count: Optional[int] = None,
    mode: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Configure the Hermes logging subsystem; returns the ``logs/`` directory.

    Safe to call multiple times; the second call is a no-op unless *force*. Level and
    rotation defaults come from config.yaml ``logging.*``. ``mode="gateway"`` adds
    ``gateway.log`` and ``mode="gui"`` adds ``gui.log``.
    """
    global _logging_initialized
    home = hermes_home or get_hermes_home()
    log_dir = mkdir_under_hermes_home(home / "logs")
    # A second Hermes home in a process that already logs for another one — a dashboard or
    # ``hermes serve`` backend building agents for several profiles, a multiplexed gateway —
    # gets routed by record home. Stacking another file handler here would hand it EVERY
    # profile's records (the handlers carry no home filter), and a duplicate writer on top of
    # an existing router.
    if _adopt_secondary_home(home):
        return log_dir
    cfg_level, cfg_max_size, cfg_backup, _ = _read_logging_config()
    level_name = (log_level or cfg_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    max_bytes = (max_size_mb or cfg_max_size or 5) * 1024 * 1024
    backups = backup_count or cfg_backup or 3

    from agent.redact import RedactingFormatter  # lazy: circular at module load

    root = logging.getLogger()

    # (filename, level, max_bytes, backup_count, component) — a component gates
    # the file on ``mode`` and restricts it to that component's logger prefixes.
    handler_specs = (
        ("agent.log", level, max_bytes, backups, None),
        ("errors.log", logging.WARNING, 2 * 1024 * 1024, 2, None),
        ("gateway.log", logging.INFO, 5 * 1024 * 1024, 3, "gateway"),
        ("gui.log", logging.INFO, 10 * 1024 * 1024, 5, "gui"),
    )
    for filename, lvl, size, count, component in handler_specs:
        if component is not None and mode != component:
            continue
        _add_rotating_handler(
            log_dir / filename, level=lvl, max_bytes=size, backup_count=count,
            formatter=RedactingFormatter(_LOG_FORMAT),
            log_filter=_ComponentFilter(COMPONENT_PREFIXES[component]) if component else None,
        )

    if _logging_initialized and not force:
        return log_dir

    # Root level must be low enough for the handlers to fire.
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    _quiet_noisy_loggers()
    _logging_initialized = True
    return log_dir


def setup_verbose_logging() -> None:
    """Enable DEBUG-level console logging for ``--verbose`` / ``-v`` mode."""
    from agent.redact import RedactingFormatter

    root = logging.getLogger()

    if any(getattr(h, "_hermes_verbose", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(_safe_stderr())
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT_VERBOSE, datefmt="%H:%M:%S"))
    handler._hermes_verbose = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    _quiet_noisy_loggers()
    # rex-deploy at INFO for sandbox status.
    logging.getLogger("rex-deploy").setLevel(logging.INFO)


def _quietly(fn) -> None:
    """Call *fn* (a ``close``/``stop`` bound method) swallowing errors — teardown must never raise."""
    try:
        fn()
    except Exception:
        pass


# ── Write-lag visibility (gate H4) ───────────────────────────────────────────────────────────
# agent.log ran ~30 min behind the wall clock on 2026-10-28: the cross-process CLH lock plus a
# reopen per record made the single queue-listener writer slower than record production, so every
# log-based timeline silently lied. The writer is per-process on Windows now, but the lag stays
# VISIBLE either way: a metric updated on every successful file write, and a rate-limited warning
# to STDERR (warning into a lagging log would itself be read late).
_LAST_WRITE_LAG: Optional[float] = None
_LAG_WARNED_AT: float = 0.0
_LAG_WARN_THRESHOLD_S = 60.0
_LAG_WARN_INTERVAL_S = 300.0


def last_write_lag_seconds() -> Optional[float]:
    """Age of the most recently written record at the moment it reached its file (None = no
    write yet). ~0 is healthy; minutes mean the writer is drowning and every log timestamp in
    this profile's logs is running that far behind reality."""
    return _LAST_WRITE_LAG


def _note_write_lag(record: logging.LogRecord) -> None:
    global _LAST_WRITE_LAG, _LAG_WARNED_AT
    now = time.time()
    _LAST_WRITE_LAG = lag = max(0.0, now - record.created)
    if lag >= _LAG_WARN_THRESHOLD_S and now - _LAG_WARNED_AT >= _LAG_WARN_INTERVAL_S:
        _LAG_WARNED_AT = now
        try:
            # The REAL stderr fd: slash commands run under redirect_stderr into the user's chat
            # reply, and a dynamic sys.stderr write would land there (agy R2-4). With BOTH streams
            # gone (daemon/GUI), skip entirely — print(file=None) would fall through to stdout, a
            # protocol channel under stdio JSON-RPC (agy R3-4).
            stream = sys.__stderr__ or sys.stderr
            if stream is not None:
                print(f"[hermes] log writer is {lag:.0f}s behind the clock; file-log timestamps lag "
                      f"reality by that much", file=stream)
        except Exception:
            pass


def _handle_file_handler_error(handler, record: logging.LogRecord, *, path: Optional[str] = None) -> None:
    """``handleError`` shared by both file-handler kinds: a lost backing stream (EIO, closed file)
    names *path* once and drops the stream so the next emit reopens it; anything else is stdlib's
    report. The QueueListener must never turn a failing destination into one traceback per record."""
    handler._last_emit_errored = True  # a swallowed failure is not a fresh write (write-lag gate)
    exc = sys.exc_info()[1]
    if _is_unavailable_log_stream(exc):
        if not handler._unavailable_reported:
            handler._unavailable_reported = True
            _quietly(lambda: print(
                f"hermes_logging: {path or handler.baseFilename} unavailable ({exc}); "
                "file logging paused until it recovers", file=_safe_stderr()))
        if handler.stream is not None:
            _quietly(handler.stream.close)
        handler.stream = None
        return
    logging.Handler.handleError(handler, record)


class _ManagedRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler with managed-mode perms and external-rotation detection.

    In managed mode (NixOS) the setgid stateDir needs group-readable files, but
    ``_open()``/``doRollover()`` honor the umask (0644), so ``chmod 0660`` follows both.
    A rotating handler also holds an fd: if the file is rotated externally (logrotate,
    ``mv``) writes silently go to the old inode, so each emit compares the path's inode
    to the open stream's and reopens on mismatch (the ``WatchedFileHandler`` pattern).
    """

    def __init__(self, *args, **kwargs):
        from hermes_cli.config import is_managed
        self._managed = is_managed()
        self._unavailable_reported = False
        super().__init__(*args, **kwargs)
        self._record_stream_stat()

    def _chmod_if_managed(self):
        if self._managed:
            try:
                os.chmod(self.baseFilename, 0o660)
            except OSError:
                pass

    def _record_stream_stat(self, st: Optional[os.stat_result] = None) -> None:
        """Snapshot dev/ino of ``baseFilename`` so emit() can detect external rotation."""
        try:
            st = st or os.stat(self.baseFilename)
            self._stat_dev, self._stat_ino = st.st_dev, st.st_ino
        except OSError:
            self._stat_dev, self._stat_ino = None, None

    def _reopen_stream(self, stat_result=None) -> None:
        """Close and reopen ``baseFilename`` (best-effort).

        On failure the stream is left ``None`` so the next emit bails rather than
        writing to a stale inode.
        """
        if self.stream is not None:
            _quietly(self.stream.close)
        self.stream = None  # type: ignore[assignment]
        try:
            self.stream = self._open()
        except Exception:
            return
        self._record_stream_stat(stat_result)

    def _reopen_if_externally_rotated(self) -> None:
        """Reopen when ``baseFilename`` was renamed, unlinked, or replaced by another inode.

        Silent + best-effort: any error falls back to the existing (possibly stale)
        stream so logging keeps working instead of dying on a stat failure.
        """
        try:
            st = os.stat(self.baseFilename)
        except FileNotFoundError:
            self._reopen_stream()  # rotated/unlinked underneath us: recreate at the path
            return
        except OSError:
            return  # transient — try again on the next emit

        if self._stat_dev is None or self._stat_ino is None:
            self._record_stream_stat(st)
        elif (st.st_dev, st.st_ino) != (self._stat_dev, self._stat_ino):
            self._reopen_stream(st)

    def emit(self, record: logging.LogRecord) -> None:
        # The kernel caches inode metadata, so this stat is sub-microsecond on a hot file.
        if self.stream is not None or os.path.exists(self.baseFilename):
            self._reopen_if_externally_rotated()
        self._last_emit_errored = False
        super().emit(record)
        # A record actually reached the file: only now has the destination recovered. Resetting
        # in _open() is wrong — open() succeeds on a device whose write/flush still raise EIO,
        # which re-armed the report and printed the path once per record.
        if self.stream is not None:
            self._unavailable_reported = False
        if not self._last_emit_errored:
            # Success-gated: every write failure goes through handleError, so a swallowed failure
            # must not count as a fresh write (agy R2-4).
            _note_write_lag(record)

    def handleError(self, record: logging.LogRecord) -> None:
        _handle_file_handler_error(self, record)

    def _open(self):
        stream = super()._open()
        self._chmod_if_managed()
        return stream

    def doRollover(self):
        super().doRollover()
        self._chmod_if_managed()
        # Our own rollover writes a new baseFilename; refresh the snapshot so
        # the next emit doesn't mistake it for external rotation.
        self._record_stream_stat()


# Segment retention (gate H4): dead processes leave their last segments behind; every home this
# process builds a file handler for is pruned once, by THAT home's ``logging.retention_days``
# (profiles are independent islands: the launch profile's value never reaches a routed home).
_pruned_log_dirs: set[Path] = set()


def _retention_days(value) -> float:
    """``logging.retention_days`` as a day count: ``None``, booleans, non-numbers and NaN fall
    back to the default (``true`` is a config mistake, not one day; ``.nan`` would compare False
    against every mtime and prune everything); ``.inf`` means keep forever."""
    from hermes_logging_segments import DEFAULT_RETENTION_DAYS
    if value is None or isinstance(value, bool):
        return float(DEFAULT_RETENTION_DAYS)
    try:
        days = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(DEFAULT_RETENTION_DAYS)
    if math.isnan(days):
        return float(DEFAULT_RETENTION_DAYS)
    return days


def _observe_config_bytes(path: Path) -> bytes:
    """The single observation of a home's config.yaml the retention policy is derived from."""
    with open(path, "rb") as handle:
        return handle.read()


def _retention_policy(home: Path) -> Optional[float]:
    """The home's ``logging.retention_days`` observed FAIL-CLOSED: ``None`` when the policy could
    not be read — an unreadable or broken config.yaml, a YAML root that is not a mapping, a
    dangling symlink — and then nothing is deleted. A failed read is never the same as an absent
    setting. Validation and the value come from the SAME bytes (one observation of the file): a
    file that changes between a probe and a second read cannot yield a policy the probe never saw.

    The administrator's managed overlay (``/etc/hermes`` or ``$HERMES_MANAGED_DIR``) is NOT read
    here: whether an overlay exists but is unavailable (a link whose target is unmounted, an
    ancestor that cannot be traversed, a file replaced non-atomically) cannot be told apart from
    "no overlay" by any filesystem probe, and an overlay an administrator used to forbid pruning
    must never vanish into the user's or the default policy. So when a managed scope MIGHT exist
    this version prunes nothing (closure rounds 3–6). The home's own config decides only when no
    managed scope is configured at all."""
    config_path = Path(home) / "config.yaml"
    try:
        if _managed_scope_may_exist():
            return None
        if config_path.is_symlink() and not config_path.exists():
            return None
        user: dict = {}
        if config_path.exists():
            from utils import fast_safe_load
            loaded = fast_safe_load(io.StringIO(_observe_config_bytes(config_path).decode("utf-8")))
            if loaded is not None and not isinstance(loaded, dict):
                return None
            user = loaded or {}
    except Exception:
        return None
    log_cfg = user.get("logging") if isinstance(user, dict) else None
    if isinstance(log_cfg, dict) and "retention_days" in log_cfg:
        value = log_cfg.get("retention_days")
        if isinstance(value, str):
            if "${" in value:
                value = _resolve_retention_ref(value, home)
                if value is None:
                    return None
            return _parse_retention_text(value)
        return _retention_days(value)
    return _retention_days(None)


def _managed_scope_may_exist() -> bool:
    """Whether an administrator's managed overlay MIGHT exist: ``HERMES_MANAGED_DIR`` is set to a
    non-blank value, or (outside pytest) the platform default directory exists or is a link.
    Mirrors the resolution order of ``managed_scope.get_managed_dir`` on EVERY platform — the
    default is a POSIX path and drive-relative on Windows (``C:\\etc\\hermes``), but the canonical
    resolver adopts such a directory there too, so retention must treat it as managed as well —
    and never asks whether the scope is USABLE: unavailable and absent look the same to a probe,
    and only the configured intent is trusted. A probe that fails (an ``OSError`` surfacing from
    ``stat`` on a dangling ancestor) answers True."""
    from hermes_cli import managed_scope
    if os.environ.get("HERMES_MANAGED_DIR", "").strip():
        return True
    if managed_scope._under_pytest():
        return False
    default = managed_scope._DEFAULT_MANAGED_DIR
    try:
        return default.is_symlink() or default.exists()
    except OSError:
        return True


def _parse_retention_text(text: str) -> Optional[float]:
    """A textual retention value (a quoted literal or an expanded reference) parsed FAIL-CLOSED:
    ``None`` for anything that is not a number — empty, prose, NaN, a value still holding a ``${``
    template — because a policy the operator wrote but we cannot read must never become the
    default. ``.inf``/``inf`` keep forever, as the unquoted YAML token does."""
    text = text.strip()
    if not text or "${" in text:
        return None
    if text.lower().lstrip("+") in (".inf", "inf"):
        return math.inf
    try:
        days = float(text)
    except (ValueError, OverflowError):
        return None
    if math.isnan(days):
        return None
    return days


def _is_process_home(home: Path) -> bool:
    """Whether *home* is the HERMES_HOME this process was launched under (task overrides ignored)."""
    try:
        from hermes_constants import get_process_hermes_home
        return Path(home).expanduser().resolve() == get_process_hermes_home().expanduser().resolve()
    except Exception:
        return False


def _observe_env_bytes(path: Path) -> bytes:
    """The single observation of a home's ``.env`` the retention policy is derived from."""
    with open(path, "rb") as handle:
        return handle.read()


def _resolve_retention_ref(value: str, home: Path) -> Optional[str]:
    """Expand ``${VAR}`` / ``${env:VAR}`` in a retention value under the OWNING scope, or ``None``
    when any reference does not resolve there (an unreadable policy, never the default).

    The routed handler for a profile is created on the shared logging worker, under whichever
    process environment launched the gateway — profile A's ``RETENTION_DAYS=1`` must never
    authorize pruning in profile B's home. A reference therefore resolves against the owning
    home's ``.env`` (observed fail-closed); the process environment is consulted only when *home*
    IS the process's own home (launch env is the documented way to pin a value for the profile you
    launched). The managed overlay is never read (see ``_retention_policy``)."""
    from hermes_cli.config import _ENV_REF_RE, _env_ref_var_name

    from agent.secret_scope import _decode_env_bytes, _parse_env_text
    env_path = Path(home) / ".env"
    try:
        # Observed fail-closed: a .env that exists but cannot be read is an unreadable policy,
        # not an absent one (load_env_file's {} on OSError would fall through to the environ).
        if env_path.is_symlink() or env_path.exists():
            dotenv = _parse_env_text(_decode_env_bytes(_observe_env_bytes(env_path)))
        else:
            dotenv = {}
    except OSError:
        return None
    own_home = _is_process_home(home)

    def lookup(name: str) -> Optional[str]:
        found = dotenv.get(name)
        if found is None and own_home:
            found = os.environ.get(name)
        return found

    def substitute(match: "re.Match[str]") -> str:
        name = _env_ref_var_name(match.group(1))
        found = lookup(name) if name else None
        if found is None:
            raise LookupError(name)
        return found

    try:
        expanded = _ENV_REF_RE.sub(substitute, value)
    except LookupError:
        return None
    # A nested reference in a .env value, or an unclosed ``${`` the pattern never matched, is still
    # a template: unresolved, never a literal the parser might accept.
    return None if "${" in expanded else expanded


def _prune_segments_once(log_dir: Path) -> None:
    key = log_dir.resolve()
    if key in _pruned_log_dirs:
        return
    _pruned_log_dirs.add(key)
    from hermes_logging_segments import prune_stale_segments
    # The home is the unresolved parent: a symlinked or junctioned logs/ still belongs to it.
    days = _retention_policy(Path(log_dir).parent)
    if days is None:
        return  # policy unknown → no deletion
    prune_stale_segments(key, retention_days=days)


def _new_file_handler(
    path: Path, *, level: int, max_bytes: int, backup_count: int, formatter
) -> BaseRotatingHandler:
    """Create the ``logs/`` directory and this host's file handler for *path*: per-process
    segments on Windows, the shared managed rotating file elsewhere (module header)."""
    mkdir_under_hermes_home(path.parent)
    _prune_segments_once(path.parent)
    if sys.platform == "win32":
        from hermes_logging_segments import _SegmentedFileHandler
        handler = _SegmentedFileHandler(
            str(path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    else:
        handler = _ManagedRotatingFileHandler(
            str(path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


class _ProfileRoutingFileHandler(logging.Handler):
    """Route queued records to the log file for their Hermes home.

    Used only behind the QueueListener, so its small routing lock never blocks an agent
    or dashboard event loop. Per-home handlers keep rotation, redaction and managed perms.
    """

    def __init__(self, existing: BaseRotatingHandler, profile_homes: Sequence[Path]) -> None:
        """Take over *existing*'s path, level, rotation, formatter and filters."""
        super().__init__(level=existing.level)
        resolved = Path(existing.baseFilename).resolve()
        self.baseFilename = str(resolved)
        self._hermes_routed_log_path = resolved
        self._default_home = resolved.parent.parent.resolve()
        self._profile_homes = {Path(home).expanduser().resolve() for home in profile_homes}
        self._filename = resolved.name
        self._max_bytes = getattr(existing, "maxBytes", 0)
        self._backup_count = getattr(existing, "backupCount", 0)
        self._profile_handlers: dict[Path, BaseRotatingHandler] = {}
        self._profile_handlers_lock = threading.RLock()
        self.setFormatter(existing.formatter)
        for log_filter in existing.filters:
            self.addFilter(log_filter)

    def _home_for_record(self, record: logging.LogRecord) -> Path:
        raw_home = getattr(record, "hermes_home", "")
        try:
            candidate = Path(raw_home).expanduser().resolve()
        except (TypeError, ValueError, OSError):
            candidate = self._default_home
        return candidate if candidate in self._profile_homes else self._default_home

    def _handler_for_home(self, home: Path) -> BaseRotatingHandler:
        with self._profile_handlers_lock:
            if home not in self._profile_handlers:
                self._profile_handlers[home] = _new_file_handler(
                    home / "logs" / self._filename, level=self.level, max_bytes=self._max_bytes,
                    backup_count=self._backup_count, formatter=self.formatter,
                )
            return self._profile_handlers[home]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._handler_for_home(self._home_for_record(record)).handle(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with self._profile_handlers_lock:
            handlers = list(self._profile_handlers.values())
            self._profile_handlers.clear()
        for handler in handlers:
            _quietly(handler.close)
        super().close()

    def release_profile(self, home: Path) -> bool:
        """Close and forget the routed files belonging to a deleted profile."""
        with self._profile_handlers_lock:
            handler = self._profile_handlers.pop(home, None)
            self._profile_homes.discard(home)
        if handler is None:
            return False
        _quietly(handler.close)
        return True


# Asynchronous file logging: an ``emit`` is disk I/O (and, on POSIX, can wait on
# a rename of the shared file); on an asyncio thread that stalls the loop and
# drops WebSocket clients. Every file handler is therefore driven by a single
# QueueListener thread; loggers only do a non-blocking enqueue.

_log_queue: "Optional[queue.SimpleQueue]" = None
_queue_listener: Optional[QueueListener] = None
_queued_file_handlers: list = []
_queue_atexit_registered = False
# Guards every read-modify-write of the four globals above. setup_logging()
# holds no lock and its _logging_initialized guard runs AFTER handler
# registration, so _register_queued_handler() can race a flush/reset from
# another thread (gateway init vs a plugin/CLI path); without this, two
# threads can interleave stop()/reassign/start() and leave two live listeners.
_queue_state_lock = threading.Lock()


class _NonFormattingQueueHandler(QueueHandler):
    """``QueueHandler`` for an in-process queue.

    Stdlib ``prepare()`` formats and strips ``args``/``exc_info`` for cross-process
    pickling; ours is in-process, so targets get the unformatted record and apply their
    own ``RedactingFormatter`` on the listener thread. A shallow copy is returned because
    the emitting thread's synchronous handlers may mutate ``record.message`` meanwhile.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return copy.copy(record)


def _stop_queue_listener() -> None:
    """Flush and stop the background log listener (idempotent; atexit hook, so it takes the lock)."""
    global _queue_listener
    with _queue_state_lock:
        listener, _queue_listener = _queue_listener, None
        if listener is not None:
            _quietly(listener.stop)


def _start_queue_listener_locked() -> None:
    """(Re)build + start a listener over the current handler set (``_queue_state_lock`` held).

    A running listener is stopped first; this only happens while handlers are being
    added (queue empty), so ``stop()`` returns immediately.
    """
    global _queue_listener
    if _queue_listener is not None:
        _queue_listener.stop()
    _queue_listener = QueueListener(_log_queue, *_queued_file_handlers, respect_handler_level=True)
    _queue_listener.start()


def _register_queued_handler(handler: logging.Handler) -> None:
    """Route *handler* through the shared async queue instead of attaching it to root.

    Emitting threads never block on file I/O or the rotation lock; the ``QueueListener``
    applies each handler's own level and filters on its worker thread.
    """
    global _log_queue, _queue_atexit_registered
    with _queue_state_lock:
        if _log_queue is None:
            _log_queue = queue.SimpleQueue()
            qh = _NonFormattingQueueHandler(_log_queue)
            qh._hermes_queue = True  # type: ignore[attr-defined]
            # Always on the root logger so records from any logger reach the queue.
            logging.getLogger().addHandler(qh)
        _queued_file_handlers.append(handler)
        _start_queue_listener_locked()
        if not _queue_atexit_registered:
            # Runs before logging.shutdown (registered earlier at import time),
            # so the listener stops before its file handlers are closed.
            atexit.register(_stop_queue_listener)
            _queue_atexit_registered = True


def flush_log_queue() -> None:
    """Block until all queued records have been written, then resume.

    Stops the listener (which processes every pending record before joining) and
    restarts it. ``stop()`` joins the worker thread — do NOT call this on a hard-exit
    path where the listener may be wedged on the rotation lock; use
    ``drain_log_queue()`` there, which bounds the wait.
    """
    with _queue_state_lock:
        listener = _queue_listener
        if listener is not None:
            listener.stop()
            listener.start()


def drain_log_queue(timeout: float = 1.0) -> None:
    """Best-effort, time-bounded drain for hard-exit paths (no restart).

    If the listener's worker is wedged on the cross-process rotation lock — the very
    failure async logging exists to survive — an unbounded join would re-freeze shutdown.
    """
    listener = _queue_listener
    if listener is None:
        return
    t = threading.Thread(target=lambda: _quietly(listener.stop), name="hermes-log-drain", daemon=True)
    t.start()
    t.join(timeout)


def release_profile_log_handlers(profile_home: str | Path) -> int:
    """Release this process's routed log files below a profile before it is removed.

    The Desktop serve process can route records for several profiles through one
    ``QueueListener``. On Windows a routed handler keeps its current segment open, so
    closing only external profile resources still leaves ``logs/<stem>.d/`` unavailable
    to rmtree.
    """
    global _queue_listener
    try:
        home = Path(profile_home).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        return 0

    with _queue_state_lock:
        listener = _queue_listener
        if listener is not None:
            listener.stop()
            _queue_listener = None
        released = sum(
            handler.release_profile(home)
            for handler in _queued_file_handlers
            if isinstance(handler, _ProfileRoutingFileHandler)
        )
        if listener is not None:
            _start_queue_listener_locked()
    return released


def enable_profile_log_routing(profile_homes: Sequence[str | Path]) -> bool:
    """Make the queued file logs follow a desktop profile context.

    ``setup_logging`` binds handlers to one process home; the desktop dashboard's
    embedded cron ticker may run jobs for every profile, so its static file handlers
    are replaced with profile routers once the profile list is known. Returns ``True``
    when routing is (or already was) enabled; a single-profile caller is left untouched.
    """
    global _queue_listener
    homes: list[Path] = []
    for entry in profile_homes:
        try:
            resolved = Path(entry[1] if isinstance(entry, tuple) else entry).expanduser().resolve()
        except (TypeError, ValueError, OSError):
            continue
        if resolved not in homes:
            homes.append(resolved)
    if len(homes) < 2:
        return False

    with _queue_state_lock:
        if not _queued_file_handlers:
            return False
        routers = [h for h in _queued_file_handlers if isinstance(h, _ProfileRoutingFileHandler)]
        if routers:
            for handler in routers:
                with handler._profile_handlers_lock:
                    handler._profile_homes = handler._profile_homes.union(homes)
            return True
        listener = _queue_listener
        if listener is not None:
            listener.stop()
            _queue_listener = None
        replacement = []
        for existing in _queued_file_handlers:
            if isinstance(existing, BaseRotatingHandler):
                replacement.append(_ProfileRoutingFileHandler(existing, homes))
                _quietly(existing.close)
            else:
                replacement.append(existing)
        _queued_file_handlers[:] = replacement
        if listener is not None:
            _start_queue_listener_locked()
        return True


def _reset_queued_handlers() -> None:
    """Tear down the async logging queue + listener (test-isolation helper)."""
    global _log_queue
    _stop_queue_listener()
    with _queue_state_lock:
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_hermes_queue", False):
                root.removeHandler(h)
        for h in list(_queued_file_handlers):
            _quietly(h.close)
        _queued_file_handlers.clear()
        _pruned_log_dirs.clear()
        _log_queue = None


def _add_rotating_handler(
    path: Path,
    *,
    level: int,
    max_bytes: int,
    backup_count: int,
    formatter: logging.Formatter,
    log_filter: Optional[logging.Filter] = None,
) -> None:
    """Register a queued ``RotatingFileHandler`` for *path*; idempotent per resolved path."""
    resolved = path.resolve()
    for existing in _queued_file_handlers:
        # Already attached directly, or already covered by the profile router — for its default
        # home or any profile home it routes (a bare handler beside it would take every record).
        if getattr(existing, "_hermes_routed_log_path", None) == resolved or (
            isinstance(existing, BaseRotatingHandler)
            and Path(getattr(existing, "baseFilename", "")).resolve() == resolved
        ):
            return
        if isinstance(existing, _ProfileRoutingFileHandler) and existing._filename == resolved.name and (
            resolved.parent.parent == existing._default_home or resolved.parent.parent in existing._profile_homes
        ):
            return
    handler = _new_file_handler(
        path, level=level, max_bytes=max_bytes, backup_count=backup_count, formatter=formatter,
    )
    if log_filter is not None:
        handler.addFilter(log_filter)
    # Routing already on (a second home adopted earlier): a component log added now —
    # ``mode="gateway"`` after the fact — must route too, or it takes every home's records.
    routers = [h for h in _queued_file_handlers if isinstance(h, _ProfileRoutingFileHandler)]
    if routers:
        homes: set[Path] = set()
        for router in routers:
            homes.add(router._default_home)
            homes.update(router._profile_homes)
        routed = _ProfileRoutingFileHandler(handler, sorted(homes))
        _quietly(handler.close)
        handler = routed
    # Queue, not ``addHandler``: file I/O never runs on the caller's thread.
    _register_queued_handler(handler)


def _read_logging_config(home: Optional[Path] = None):
    """Best-effort read of ``logging.*`` from *home*'s config.yaml (the current home by default)."""
    try:
        config_path = (Path(home) / "config.yaml") if home is not None else get_config_path()
        # Prefer the shared effective-config cache (managed overlay included, so an administrator
        # can pin logging.*) so this reuses hermes_cli.main's early parse (one config.yaml parse
        # per process); fall back to a direct parse for bare hermes_logging consumers.
        try:
            from hermes_cli.config_effective import load_user_config_effective
            cfg = load_user_config_effective(config_path)
        except Exception:
            from utils import fast_safe_load
            cfg = {}
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = fast_safe_load(f) or {}
        if not cfg:
            return (None, None, None, None)
        log_cfg = cfg.get("logging", {})
        if isinstance(log_cfg, dict):
            return (log_cfg.get("level"), log_cfg.get("max_size_mb"), log_cfg.get("backup_count"),
                    log_cfg.get("retention_days"))
    except Exception:
        pass
    return (None, None, None, None)


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.

def rotating_file_handlers() -> list:
    """Return the live rotating file handlers.

    They are attached to the async ``QueueListener`` rather than the root
    logger, so callers/tests must use this instead of scanning
    ``logging.getLogger().handlers``."""
    return list(_queued_file_handlers)
# ---- END PLUGIN-COMPAT ----
