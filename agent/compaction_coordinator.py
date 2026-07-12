"""Root-scoped coordinator for the cross-session compaction queue.

**Phase 0 — primitives with NO CALLERS.** Path resolution plus the leased
semaphore (``compaction_slots``) live here, but nothing imports them yet, there
is no ``compaction_queue`` config, and compression is not wired. The queue is
**dark**: this module cannot change behaviour until a separate, user-approved
wiring step. That matters because the InstallDir *is* the running backend.

See ``docs/plans/2026-07-11-compaction-queue-spec.md`` §4.2 (substrate), §4.3
(typed outcomes) and §9.0-GATE (the shared-path gate this design had to pass).

Why this file exists at all
--------------------------
The compaction queue must bound *concurrent compaction summarisation calls*
across every backend process on the machine. The obvious substrate — the
existing ``state.db`` — is **wrong**, and Gate 0 caught it:

``state.db`` is anchored to the **active profile's** ``HERMES_HOME``
(``hermes_state.py``: ``DEFAULT_DB_PATH = get_hermes_home() / "state.db"``), and
under a non-default profile ``get_hermes_home()`` is ``<root>/profiles/<name>``.
But the processes that generate the herd we are trying to bound **deliberately
span profiles**:

* **Kanban workers** — a task's ``assignee`` *is* a profile name, and the worker
  is spawned with ``env["HERMES_HOME"] = resolve_profile_env(profile_arg)``
  (``hermes_cli/kanban_db.py``). The dispatcher herd is cross-profile *by
  construction* — which is exactly the scenario the queue exists for.
* **The desktop backend / gateway** — in app-global remote mode **one backend
  serves every profile** (``tui_gateway/server.py``), opening a *per-session* DB
  at ``SessionDB(db_path=Path(profile_home) / "state.db")``.

A coordinator living in ``state.db`` would therefore sit in a **different file
per profile**: ``max_concurrent = 1`` would permit one concurrent compaction *per
profile*, silently. And because the coordinator **fails open** by design, that
mis-scoping is **invisible** — every acquire succeeds, every log line looks
healthy, and the bound does nothing.

So the coordinator is anchored to the shared **root** instead.

This is not a novel pattern
---------------------------
The kanban **board** hit this identical hazard and solved it the same way:
``kanban_home()`` (``hermes_cli/kanban_db.py``) resolves through
``get_default_hermes_root()`` — with an ``HERMES_KANBAN_HOME`` override for tests
and unusual deployments — precisely because "Resolving the kanban paths through
the active profile's ``HERMES_HOME`` would silently fork the board per profile,
which breaks the dispatcher / worker handoff."

This module mirrors that shape deliberately, including the override.
"""

from __future__ import annotations

import enum
import logging
import os
import random
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Path override for tests and unusual deployments (Docker, custom roots).
#
# This is a PATH OVERRIDE, not config — it tunes no behaviour, it only relocates
# the coordinator file. Directly analogous to the pre-existing HERMES_KANBAN_HOME
# and to HERMES_HOME itself. It is required because the multi-process Gate 0 and
# concurrency tests must point *real subprocesses* at a temp root without
# touching the developer's real <root>/compaction.db — and HERMES_HOME cannot
# serve that purpose, since root-anchoring deliberately IGNORES the per-profile
# HERMES_HOME (that is the entire point of this module).
#
# No queue *tunable* (max_concurrent, TTLs, waits) gets an env var; those are
# YAML-only, per AGENTS' "no new HERMES_* env vars for non-secret config".
COMPACTION_HOME_ENV = "HERMES_COMPACTION_HOME"

COMPACTION_DB_FILENAME = "compaction.db"


def compaction_home() -> Path:
    """Return the shared Hermes root that anchors the compaction coordinator.

    Resolution order (mirrors ``kanban_db.kanban_home()``):

    1. ``HERMES_COMPACTION_HOME`` when set and non-empty — explicit override for
       tests and unusual deployments.
    2. ``get_default_hermes_root()``, which already returns ``<root>`` when
       ``HERMES_HOME`` is ``<root>/profiles/<name>``, and returns ``HERMES_HOME``
       directly for Docker / custom deployments.

    The coordinator is shared across profiles **by design**. Resolving it through
    the active profile's ``HERMES_HOME`` would silently fork the semaphore per
    profile, so the bound would stop covering the cross-profile herd (kanban
    workers, app-global desktop backends) it exists to bound.
    """
    override = os.environ.get(COMPACTION_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def compaction_db_path() -> Path:
    """Return the absolute path of the root-scoped coordinator DB.

    **Resolved at CALL TIME, never cached in a module-level constant.** This is
    not a style preference — it is the specific trap this design must avoid:
    ``hermes_state.DEFAULT_DB_PATH`` is evaluated at *module import*, and
    ``set_hermes_home_override()`` is a ContextVar that deliberately does not
    mutate ``os.environ``, so it does **not** retroactively update that constant.
    That is why the gateway has to pass ``db_path`` explicitly when it binds a
    session to another profile.

    If this module cached its path at import, a process that imported it under
    one ``HERMES_HOME`` and later ran under another (exactly what the gateway
    does per session) would coordinate against a stale path — silently, since the
    coordinator fails open. Root-anchoring makes this mostly moot in practice
    (the root is the same either way), but the discipline is required so a
    ``HERMES_COMPACTION_HOME`` override or a Docker layout cannot be frozen at
    import time.
    """
    return compaction_home() / COMPACTION_DB_FILENAME


# ── Phase 0: leased-semaphore slot primitives ────────────────────────────────
#
# PURE ADDITIONS — nothing calls these yet, and no `compaction_queue` config
# exists, so this phase cannot change behaviour. The InstallDir runs the live
# backend, so shipping dark is what keeps building here safe.
#
# The semaphore bounds *concurrent compaction summarisation calls* across every
# session, process, AND PROFILE under one Hermes root. `slot_id` rows are the
# bound: at most `max_concurrent` live rows can exist at once.

# Versioned INDEPENDENTLY of hermes_state.SCHEMA_VERSION — different file,
# different lifecycle, different owner (spec §9.0). This is not a decorative
# constant: it is stamped into the `compaction_meta` table on first use (see
# _record_schema_version) so the first real migration has something to branch on
# rather than having to guess the shape of DBs already in the field.
SCHEMA_VERSION = 1

# Write-contention tuning. Mirrors hermes_state.SessionDB's rationale: keep the
# SQLite busy timeout short and retry in Python with random jitter, because
# SQLite's built-in deterministic backoff creates convoy effects when several
# processes contend. Contention here is *expected and normal* — a herd of
# backends all trying to acquire the one slot is the whole point.
_BUSY_TIMEOUT_S = 1.0
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020
_WRITE_RETRY_MAX_S = 0.150

DEFAULT_SLOT_TTL_SECONDS = 300.0

# Log the resolved coordinator path once per process (spec §7). The rev-5 defect
# class — a coordinator silently bound to the wrong file — is invisible under
# fail-open semantics, so printing the file actually in use is the cheapest way
# for an operator to confirm the bound is real.
_logged_path_lock = threading.Lock()
_logged_path: Optional[str] = None


class SlotOutcome(str, enum.Enum):
    """Typed outcome of a coordinator operation.

    The three-way split is the single most important design decision in this
    module. See ``try_acquire_compaction_slot`` for why DENIED and
    COORDINATOR_ERROR must never be collapsed.
    """

    ACQUIRED = "acquired"                 # caller owns slot_id; caller MUST release
    DENIED = "denied"                     # queue genuinely full; caller defers this cycle
    COORDINATOR_ERROR = "coordinator_error"  # queue unusable; caller BYPASSES (fail-open)


@dataclass(frozen=True)
class SlotResult:
    outcome: SlotOutcome
    slot_id: Optional[str] = None
    holder: Optional[str] = None
    session_id: Optional[str] = None
    slots_in_use: Optional[int] = None
    max_concurrent: Optional[int] = None
    expires_at: Optional[float] = None
    reclaimed_expired: int = 0
    error: Optional[str] = None

    @property
    def acquired(self) -> bool:
        return self.outcome is SlotOutcome.ACQUIRED

    @property
    def coordinator_failed(self) -> bool:
        """True when the caller must FAIL OPEN (bypass the queue, compact now)."""
        return self.outcome is SlotOutcome.COORDINATOR_ERROR


@dataclass(frozen=True)
class SlotLoad:
    """Diagnostics snapshot of the root-wide bound."""

    slots_in_use: int = 0
    max_concurrent: Optional[int] = None
    holders: List[dict] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def make_holder(session_id: str = "", agent_id: Any = None) -> str:
    """Build a unique holder id: pid:tid:agent:nonce.

    Same shape as ``conversation_compression._compression_lock_holder`` so ops
    can read both in diagnostics. The nonce disambiguates two acquires from the
    same thread; pid+tid let an operator tell a crashed holder from a live one
    (expiry-based reclaim uses ``expires_at``, but ``holder`` is what shows up in
    logs).
    """
    return (
        f"pid={os.getpid()}"
        f":tid={threading.get_ident()}"
        f":agent={id(agent_id) if agent_id is not None else 0:x}"
        f":nonce={uuid.uuid4().hex[:8]}"
    )


_DDL = """
CREATE TABLE IF NOT EXISTS compaction_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS compaction_slots (
    slot_id     TEXT PRIMARY KEY,          -- "0".."N-1"; the row count IS the bound
    holder      TEXT NOT NULL,             -- pid:tid:agent:nonce
    session_id  TEXT NOT NULL DEFAULT '',  -- diagnostics only — NOT a scheduling input
    profile     TEXT NOT NULL DEFAULT '',  -- diagnostics only — which profile holds it
    source      TEXT NOT NULL DEFAULT '',  -- diagnostics only (platform / 'kanban')
    acquired_at REAL NOT NULL,
    updated_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_compaction_slots_expires
    ON compaction_slots(expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_compaction_slots_holder
    ON compaction_slots(holder);
"""

_SCHEMA_VERSION_KEY = "schema_version"


def _open_readonly() -> Optional[sqlite3.Connection]:
    """Open the coordinator DB READ-ONLY, or return None if it does not exist.

    Diagnostics must not have side effects. ``_connect()`` deliberately does —
    it mkdirs the root, creates the DB, runs the DDL, and stamps the schema
    version — which is correct for the write path but wrong for a read.
    A ``hermes doctor``-style call must never *materialise* the coordinator it
    is reporting on: that would make "does the queue exist yet?" unanswerable,
    and would litter a fresh root with compaction.db/-wal/-shm files.

    ``mode=ro`` is belt-and-braces on top of the existence check: SQLite will
    refuse to create the file, so even a race (file deleted between the check
    and the open) fails cleanly rather than resurrecting an empty DB.
    """
    db_path = compaction_db_path()
    if not db_path.exists():
        return None
    return sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)


def get_schema_version() -> Optional[int]:
    """Return the schema version recorded in the coordinator DB, or None.

    READ-ONLY and side-effect-free: ``None`` means the DB does not exist yet, or
    exists but is unreadable/malformed/pre-metadata. Never raises, and never
    creates the DB (or its -wal/-shm sidecars) — see ``_open_readonly``.
    """
    conn = None
    try:
        conn = _open_readonly()
        if conn is None:
            return None  # no DB yet — the queue has simply never been used here
        row = conn.execute(
            "SELECT value FROM compaction_meta WHERE key = ?", (_SCHEMA_VERSION_KEY,),
        ).fetchone()
        return int(row[0]) if row else None
    except Exception:
        logger.debug("compaction coordinator: schema version unreadable", exc_info=True)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _record_schema_version(conn: sqlite3.Connection) -> None:
    """Stamp SCHEMA_VERSION into ``compaction_meta``.

    The coordinator's schema is versioned INDEPENDENTLY of ``state.db``'s
    ``SCHEMA_VERSION`` — different file, different lifecycle, different owner
    (spec §9.0). Recording it now (rather than leaving ``SCHEMA_VERSION`` as a
    decorative constant) means the first real migration has something to branch
    on, instead of having to guess the shape of DBs already in the field.

    ``INSERT OR IGNORE`` so an existing, older stamp is never silently
    overwritten — a future migration must read the old value, migrate, and only
    then bump it. Best-effort: a failure to stamp must not fail an acquire, since
    the coordinator has to keep working (or fail OPEN) regardless.
    """
    conn.execute(
        "INSERT OR IGNORE INTO compaction_meta (key, value) VALUES (?, ?)",
        (_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
    )


class _CoordinatorInputError(ValueError):
    """A caller/config value was not usable (None, "", NaN, an object, ...).

    Raised by the coercion helpers below and caught by every public primitive,
    so it maps to COORDINATOR_ERROR — i.e. **fail open, compact unbounded** —
    rather than escaping as an uncaught TypeError/ValueError.

    This matters because the values come from YAML: ``compaction_queue.max_concurrent``
    could be ``"one"``, ``null``, or a nested dict in a hand-edited config. A raw
    ``int("one")`` inside the queue path would then throw straight through
    ``compress_context`` and break the *turn*, when the correct degradation for
    ANY unusable coordinator input is to skip the bound and let compaction
    proceed. Never DENIED: a malformed config must not silently freeze compaction
    machine-wide (see ``_coordinator_error``).
    """


def _coerce_max_concurrent(value: Any) -> int:
    """Clamp ``max_concurrent`` to a usable int >= 1, or raise _CoordinatorInputError.

    A *valid but non-positive* number clamps to 1 (0 would deadlock all
    compaction, violating fail-open — spec §6). A *non-numeric* value is not
    clampable and is an input error.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _CoordinatorInputError(f"max_concurrent must be a number, got {value!r}")
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        raise _CoordinatorInputError(f"max_concurrent must be finite, got {value!r}")
    return max(1, int(value))


def _coerce_ttl(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _CoordinatorInputError(f"ttl_seconds must be a number, got {value!r}")
    if value != value or value == float("inf"):
        raise _CoordinatorInputError(f"ttl_seconds must be finite, got {value!r}")
    return max(1.0, float(value))


def _coerce_now(value: Any) -> float:
    if value is None:
        return time.time()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _CoordinatorInputError(f"now must be a number or None, got {value!r}")
    if value != value:
        raise _CoordinatorInputError(f"now must be finite, got {value!r}")
    return float(value)


def _log_resolved_path_once(db_path: Path) -> None:
    global _logged_path
    with _logged_path_lock:
        if _logged_path == str(db_path):
            return
        _logged_path = str(db_path)
    logger.info("Compaction coordinator: using root-scoped DB %s", db_path)


def _connect() -> sqlite3.Connection:
    """Open the root-scoped coordinator DB.

    ``compaction_db_path()`` is called HERE, per operation — never cached in a
    module-level constant. That is the ``hermes_state.DEFAULT_DB_PATH`` trap
    (frozen at import; a ContextVar HERMES_HOME override never updates it), and
    falling into it would silently bind a session bound to profile B against
    profile A's coordinator.
    """
    db_path = compaction_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _log_resolved_path_once(db_path)

    conn = sqlite3.connect(
        str(db_path), isolation_level=None, timeout=_BUSY_TIMEOUT_S,
    )
    try:
        from hermes_state import apply_wal_with_fallback

        # Reused rather than re-derived: it already handles WAL-incompatible
        # filesystems (NFS/SMB/some FUSE) by falling back to journal_mode=DELETE.
        # Importing this helper does NOT bind us to SessionDB or DEFAULT_DB_PATH.
        apply_wal_with_fallback(conn, db_label=COMPACTION_DB_FILENAME)
    except Exception:  # pragma: no cover - pragma failure must not be fatal
        logger.debug("compaction coordinator: WAL pragma failed", exc_info=True)
    conn.executescript(_DDL)
    try:
        _record_schema_version(conn)
    except Exception:  # pragma: no cover - stamping must never break an acquire
        logger.debug("compaction coordinator: schema stamp failed", exc_info=True)
    return conn


def _execute_write(fn: Callable[[sqlite3.Connection], T]) -> T:
    """Run a write transaction with BEGIN IMMEDIATE + jittered retry.

    BEGIN IMMEDIATE takes the write lock at transaction START, so the
    read-count-then-insert sequence in acquire is atomic against other
    processes — without it, two backends could both COUNT 0 and both INSERT,
    breaking the bound (and doing so *silently*, which is the failure mode this
    whole workstream exists to prevent).

    Raises on exhaustion; callers map that to COORDINATOR_ERROR.
    """
    last_err: Optional[Exception] = None
    for attempt in range(_WRITE_MAX_RETRIES):
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.commit()
                return result
            except BaseException:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last_err = exc
            time.sleep(random.uniform(_WRITE_RETRY_MIN_S, _WRITE_RETRY_MAX_S))
        finally:
            try:
                conn.close()
            except Exception:
                pass
    raise last_err if last_err else sqlite3.OperationalError("write retries exhausted")


def _coordinator_error(op: str, exc: BaseException) -> SlotResult:
    """Map ANY coordinator failure to COORDINATOR_ERROR — never to DENIED.

    ── THE CRITICAL INVARIANT OF THIS MODULE ──────────────────────────────────
    This DELIBERATELY DIVERGES from the neighbouring per-session lock primitives
    in ``hermes_state.py``, which catch ``sqlite3.Error`` and return ``False``.
    Do NOT "harmonise" this with them.

    For a per-session MUTEX, collapsing error into "you didn't get the lock" is
    safe: the worst case is one skipped compaction for one session.

    For this GLOBAL QUEUE it is catastrophic. ``DENIED`` means "the queue is
    full, defer and retry next cycle" — so a broken coordinator that reported
    DENIED would defer compaction *forever*, on *every* session, machine-wide: a
    permanent no-compaction stall, i.e. the exact OPPOSITE of the fail-open
    contract (spec §4.3, requirement 5).

    So: DENIED is only ever returned when a transaction SUCCEEDED and observed
    ``count >= max_concurrent``. Every sqlite3.Error, every unexpected
    exception, and (at the call site) every AttributeError from module/version
    skew maps to COORDINATOR_ERROR, whose contract is: BYPASS the queue and
    compact unbounded. Degrading to today's unbounded behaviour is always
    preferable to silently freezing compaction.
    """
    detail = f"{type(exc).__name__}: {exc}"
    logger.warning(
        "Compaction coordinator: %s failed (%s) — failing OPEN (bypassing the "
        "queue and compacting unbounded). The bound is not in effect.",
        op, detail,
    )
    return SlotResult(outcome=SlotOutcome.COORDINATOR_ERROR, error=detail)


def try_acquire_compaction_slot(
    session_id: str = "",
    *,
    holder: Optional[str] = None,
    max_concurrent: int = 1,
    ttl_seconds: float = DEFAULT_SLOT_TTL_SECONDS,
    profile: str = "",
    source: str = "",
    now: Optional[float] = None,
) -> SlotResult:
    """Try to claim one compaction slot. NEVER blocks.

    One write transaction: reclaim expired leases → count live slots → DENIED if
    full, else INSERT the lowest free ``slot_id``.

    Returns:
        ACQUIRED          — caller owns ``result.slot_id`` and MUST release it.
        DENIED            — genuinely full. Caller defers this cycle: messages
                            unchanged, NO side effects, no status spam.
        COORDINATOR_ERROR — queue unusable. Caller BYPASSES and compacts
                            unbounded (fail-open). See ``_coordinator_error``.
    """
    # Coercion happens INSIDE the try below, not before it: these values arrive
    # from YAML config, so a malformed one (`max_concurrent: "one"`, `null`, a
    # nested dict) must degrade to COORDINATOR_ERROR → fail open → compact
    # unbounded. Before this, a raw int()/float() here would have thrown straight
    # out of compress_context and broken the turn.
    try:
        max_concurrent = _coerce_max_concurrent(max_concurrent)
        ttl = _coerce_ttl(ttl_seconds)
        ts = _coerce_now(now)
        holder_id = holder or make_holder(session_id)
    except Exception as exc:
        return _coordinator_error("acquire (bad input)", exc)

    def _txn(conn: sqlite3.Connection) -> SlotResult:
        # 1. Reclaim crashed/expired holders. This is what makes a hard-killed
        #    backend self-heal within one TTL instead of wedging the queue.
        cur = conn.execute(
            "DELETE FROM compaction_slots WHERE expires_at <= ?", (ts,),
        )
        reclaimed = cur.rowcount or 0

        # 2. Count what is genuinely live.
        taken = {
            row[0] for row in conn.execute("SELECT slot_id FROM compaction_slots")
        }
        in_use = len(taken)

        # 3. DENIED only here — on a SUCCESSFUL observation of a full queue.
        if in_use >= max_concurrent:
            return SlotResult(
                outcome=SlotOutcome.DENIED,
                session_id=session_id,
                slots_in_use=in_use,
                max_concurrent=max_concurrent,
                reclaimed_expired=reclaimed,
            )

        # 4. Lowest free slot id.
        slot_id = next(
            str(i) for i in range(max_concurrent) if str(i) not in taken
        )
        expires_at = ts + ttl
        conn.execute(
            "INSERT INTO compaction_slots "
            "(slot_id, holder, session_id, profile, source, acquired_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (slot_id, holder_id, session_id or "", profile or "", source or "",
             ts, ts, expires_at),
        )
        return SlotResult(
            outcome=SlotOutcome.ACQUIRED,
            slot_id=slot_id,
            holder=holder_id,
            session_id=session_id,
            slots_in_use=in_use + 1,
            max_concurrent=max_concurrent,
            expires_at=expires_at,
            reclaimed_expired=reclaimed,
        )

    try:
        result = _execute_write(_txn)
    except Exception as exc:  # sqlite3.Error AND anything unexpected
        return _coordinator_error("acquire", exc)

    if result.acquired:
        logger.info(
            "Compaction coordinator: ACQUIRED slot %s (session=%s profile=%s "
            "source=%s in_use=%s/%s reclaimed=%d)",
            result.slot_id, session_id or "-", profile or "-", source or "-",
            result.slots_in_use, result.max_concurrent, result.reclaimed_expired,
        )
    return result


def refresh_compaction_slot(
    slot_id: str,
    holder: str,
    *,
    ttl_seconds: float = DEFAULT_SLOT_TTL_SECONDS,
    now: Optional[float] = None,
) -> SlotResult:
    """Extend the lease on a slot we hold.

    Returns:
        ACQUIRED          — still ours; lease extended to ``expires_at``.
        DENIED            — we no longer hold it (lease expired and was reclaimed,
                            or it was released). NOT an error — a successful
                            observation that the slot is not ours.
        COORDINATOR_ERROR — DB failure; surfaced distinctly so a lease refresher
                            can tolerate transient blips instead of treating them
                            as lost ownership.
    """
    try:
        ttl = _coerce_ttl(ttl_seconds)
        ts = _coerce_now(now)
    except Exception as exc:
        return _coordinator_error("refresh (bad input)", exc)
    expires_at = ts + ttl

    def _txn(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "UPDATE compaction_slots SET expires_at = ?, updated_at = ? "
            "WHERE slot_id = ? AND holder = ? AND expires_at > ?",
            (expires_at, ts, slot_id, holder, ts),
        )
        return cur.rowcount or 0

    try:
        rows = _execute_write(_txn)
    except Exception as exc:
        return _coordinator_error("refresh", exc)

    if rows:
        return SlotResult(
            outcome=SlotOutcome.ACQUIRED,
            slot_id=slot_id, holder=holder, expires_at=expires_at,
        )
    return SlotResult(outcome=SlotOutcome.DENIED, slot_id=slot_id, holder=holder)


def release_compaction_slot(slot_id: str, holder: str) -> SlotResult:
    """Release a slot. IDEMPOTENT — safe to call twice, or on an expired slot.

    Holder-scoped so a process cannot release a slot that TTL-reclaim already
    handed to someone else.

    Returns:
        ACQUIRED          — the row was ours and is now gone.
        DENIED            — nothing to release (already released / reclaimed).
                            A benign no-op, NOT an error.
        COORDINATOR_ERROR — DB failure. The lease still expires by TTL, so the
                            slot self-heals within one TTL even if release never
                            lands.
    """
    def _txn(conn: sqlite3.Connection) -> int:
        cur = conn.execute(
            "DELETE FROM compaction_slots WHERE slot_id = ? AND holder = ?",
            (slot_id, holder),
        )
        return cur.rowcount or 0

    try:
        rows = _execute_write(_txn)
    except Exception as exc:
        return _coordinator_error("release", exc)

    if rows:
        logger.info("Compaction coordinator: released slot %s", slot_id)
        return SlotResult(
            outcome=SlotOutcome.ACQUIRED, slot_id=slot_id, holder=holder,
        )
    return SlotResult(outcome=SlotOutcome.DENIED, slot_id=slot_id, holder=holder)


def get_compaction_slot_load(
    max_concurrent: Optional[int] = None,
    *,
    now: Optional[float] = None,
) -> SlotLoad:
    """Diagnostics: how many slots are live RIGHT NOW, root-wide.

    Read-only and non-reclaiming: expired rows are filtered out of the count but
    not deleted, so a diagnostic call never mutates the queue. Reports the
    MACHINE-ROOT-WIDE load across every profile, not this profile's.
    """
    conn = None
    try:
        ts = _coerce_now(now)  # inside the try — a bad value reports error, not a false zero
        # READ-ONLY, like get_schema_version: a diagnostic must never materialise
        # the coordinator it reports on. On a root where the queue has never run,
        # "no DB" is a legitimate answer meaning "no slots held" — not an error,
        # and not a reason to create compaction.db/-wal/-shm.
        conn = _open_readonly()
        if conn is None:
            return SlotLoad(slots_in_use=0, max_concurrent=max_concurrent)
        rows = conn.execute(
            "SELECT slot_id, holder, session_id, profile, source, acquired_at, expires_at "
            "FROM compaction_slots WHERE expires_at > ? ORDER BY slot_id",
            (ts,),
        ).fetchall()
        holders = [
            {
                "slot_id": r[0], "holder": r[1], "session_id": r[2],
                "profile": r[3], "source": r[4],
                "acquired_at": r[5], "expires_at": r[6],
            }
            for r in rows
        ]
        return SlotLoad(
            slots_in_use=len(holders),
            max_concurrent=max_concurrent,
            holders=holders,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.debug("Compaction coordinator: slot load unavailable (%s)", detail)
        return SlotLoad(max_concurrent=max_concurrent, error=detail)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
