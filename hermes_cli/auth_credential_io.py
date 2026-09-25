"""Strict credential writer (W) and witnessed reads (R).

Credential recovery protocol, stage 1 (design rev 9 §1). Every credential file Hermes owns —
``auth.json``, the Codex chain authority registry, owner-local chain records, ownership-op
payloads — is written through :func:`write_credential_file`, which reports exactly what it knows:

* ``NOT_COMMITTED``: the destination is untouched (the rename never happened).
* ``COMMITTED``: the rename happened AND a re-read returned the bytes written.
* ``COMMITTED_OR_UNCERTAIN``: the rename may have happened but could not be confirmed; the caller
  re-reads before deciding anything.

There is no in-place or copy fallback (``utils.atomic_replace`` has both): a writer that cannot
rename atomically reports ``NOT_COMMITTED`` rather than exposing a half-written file. A contended
rename (Windows: another handle on the target) is retried a fixed number of times — an attempt
budget, never a deadline — and a caller that stops waiting abandons the result, never the
mutation (the function always runs to one of the three outcomes).

Witnessed reads read a file's bytes ONCE, then hash and parse those same bytes, so a save can be
checked against exactly the snapshot it was derived from (see ``auth.py::_save_auth_store`` for
the witnessed-save table).
"""

from __future__ import annotations

import contextvars
import enum
import hashlib
import json
import logging
import os
import random
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Contended-rename budget (design §1 W): 6 attempts, jittered 20..100 ms between them.
REPLACE_ATTEMPTS = 6
_REPLACE_DELAY_RANGE_S = (0.02, 0.1)
# Windows errors a held target (or source) handle produces; anything else is not contention.
_WINDOWS_CONTENDED = frozenset({5, 32, 33})


class WriteOutcome(enum.Enum):
    NOT_COMMITTED = "not_committed"
    COMMITTED = "committed"
    COMMITTED_OR_UNCERTAIN = "committed_or_uncertain"


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    path: Path
    sha: str
    error: Optional[BaseException] = None

    @property
    def committed(self) -> bool:
        return self.outcome is WriteOutcome.COMMITTED


class CredentialWriteError(OSError):
    """A credential write did not commit (the destination is unchanged)."""


class CredentialWriteUncertain(OSError):
    """A credential write may or may not have landed; re-read before deciding anything."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_path_memo: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar("credential_path_memo", default=None)


@contextmanager
def path_memo():
    """Memoize path canonicalization for one bounded protocol pass (a dispatch, an ownership op, a
    refresh transaction): each realpath costs a filesystem round trip, and a pass resolves the same
    few store paths dozens of times. Nested scopes share the outer memo."""
    if _path_memo.get() is not None:
        yield _path_memo.get()
        return
    token = _path_memo.set({})
    try:
        yield _path_memo.get()
    finally:
        _path_memo.reset(token)


def memo() -> Optional[dict]:
    """The active pass memo, or None (callers may cache other per-pass values in it)."""
    return _path_memo.get()


def canonical_path(path: Union[str, Path]) -> Path:
    """The real file a write lands in: every symlink resolved, so a symlinked store is written
    THROUGH (upstream ``atomic_replace`` semantics), never replaced by a regular file."""
    cache = _path_memo.get()
    raw = os.fspath(path)
    if cache is None:
        return Path(os.path.realpath(raw))
    hit = cache.get(raw)
    if hit is None:
        hit = cache[raw] = Path(os.path.realpath(raw))
    return hit


def store_key(path: Union[str, Path]) -> str:
    """Comparable identity of a credential file: canonical path, case-folded where the OS is."""
    return os.path.normcase(str(canonical_path(path)))


def _is_contended(exc: OSError) -> bool:
    """A Windows error another process's handle or in-flight rename can cause. ``open()`` reports a
    file in the delete-pending window of a peer's ``os.replace`` as a bare EACCES (errno 13, no
    winerror: the CRT maps ERROR_ACCESS_DENIED), so that counts too — bounded like the rest."""
    if os.name != "nt":
        return False
    winerror = getattr(exc, "winerror", None)
    return winerror in _WINDOWS_CONTENDED or (winerror is None and isinstance(exc, PermissionError))


is_contended_error = _is_contended


def retry_contended(action, *, attempts: int = REPLACE_ATTEMPTS):
    """Run *action* (an open/read/unlink of a credential file); a Windows sharing/lock violation from
    another process's handle or rename is retried within the same bounded budget as the writer's
    rename (jittered 20..100 ms). After the budget the LAST error is raised unchanged: a real
    permission error is never turned into a silent success. Any other error raises at once."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except OSError as exc:
            if not _is_contended(exc) or attempt == attempts:
                raise
            time.sleep(random.uniform(*_REPLACE_DELAY_RANGE_S))


def read_bytes_retrying(path: Union[str, Path]) -> bytes:
    return retry_contended(Path(path).read_bytes)


def unlink_retrying(path: Union[str, Path]) -> None:
    retry_contended(Path(path).unlink)


def _read_back(path: Path) -> Optional[bytes]:
    try:
        return read_bytes_retrying(path)
    except OSError:
        return None


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return  # directories cannot be opened for fsync on Windows; the file fsync still applies
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def encode_json(data: Any) -> bytes:
    """The byte form every credential JSON file is written in (surrogate-safe, see ``utils``)."""
    try:
        text = json.dumps(data, indent=2, ensure_ascii=False)
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(data, indent=2, ensure_ascii=True).encode("utf-8")


def write_credential_file(dest: Union[str, Path], data: bytes) -> WriteResult:
    """Write *data* to *dest* with the strict protocol (module docstring).

    Pre-write refusals — the deleted-profile resurrection guard, an unwritable directory — raise
    before anything is created. Everything after the temp file exists is reported as an outcome.
    """
    from hermes_constants import mkdir_under_hermes_home, secure_parent_dir

    target = canonical_path(dest)
    digest = sha256_hex(data)
    mkdir_under_hermes_home(target.parent)
    secure_parent_dir(target)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
    except OSError as exc:
        with suppress(OSError):
            tmp.unlink()
        return WriteResult(WriteOutcome.NOT_COMMITTED, target, digest, exc)

    last_error: Optional[OSError] = None
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            os.replace(tmp, target)
            last_error = None
            break
        except OSError as exc:
            last_error = exc
            if tmp.exists():
                if not _is_contended(exc) or attempt == REPLACE_ATTEMPTS:
                    with suppress(OSError):
                        tmp.unlink()
                    logger.debug("credential write %s not committed: %s", target, exc)
                    return WriteResult(WriteOutcome.NOT_COMMITTED, target, digest, exc)
                time.sleep(random.uniform(*_REPLACE_DELAY_RANGE_S))
                continue
            # The temp file is gone although the rename reported failure: it may have landed.
            return WriteResult(
                WriteOutcome.COMMITTED_OR_UNCERTAIN, target, digest, exc)
    if last_error is not None:  # pragma: no cover - loop always returns on failure
        return WriteResult(WriteOutcome.NOT_COMMITTED, target, digest, last_error)
    if os.name != "nt":
        with suppress(OSError):
            os.chmod(target, 0o600)
    _fsync_dir(target.parent)
    if _read_back(target) != data:
        return WriteResult(WriteOutcome.COMMITTED_OR_UNCERTAIN, target, digest,
                           OSError(f"re-read of {target} did not return the bytes written"))
    return WriteResult(WriteOutcome.COMMITTED, target, digest)


def require_committed(result: WriteResult, what: str) -> WriteResult:
    """Raise unless *result* is ``COMMITTED`` (for writes whose loss is not tolerable)."""
    if result.outcome is WriteOutcome.COMMITTED:
        return result
    if result.outcome is WriteOutcome.NOT_COMMITTED:
        raise CredentialWriteError(f"{what} was not written ({result.error})")
    raise CredentialWriteUncertain(f"{what} may not have been written ({result.error})")


# ── Witnessed reads ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Witness:
    """What one read of a credential file saw.

    ``sha`` is None when the file did not exist. ``parsed_ok`` is False when the bytes did not
    parse (a degraded read). ``valid`` is False once a save's outcome was uncertain: nothing can
    be derived from that snapshot any more.
    """

    key: str
    sha: Optional[str]
    parsed_ok: bool
    valid: bool = True

    def invalidated(self) -> "Witness":
        return Witness(self.key, self.sha, self.parsed_ok, valid=False)


def read_witnessed_bytes(path: Union[str, Path]) -> Tuple[Optional[bytes], Optional[str]]:
    """``(bytes, sha)`` of one read, ``(None, None)`` when the file does not exist.

    Other ``OSError``s propagate: an unreadable file is not an absent one."""
    try:
        data = read_bytes_retrying(path)
    except FileNotFoundError:
        return None, None
    return data, sha256_hex(data)


def current_sha(path: Union[str, Path]) -> Optional[str]:
    return read_witnessed_bytes(path)[1]


class WitnessedStore(dict):
    """A dict parsed from one witnessed read; ``witness`` travels with it (``dict(x)`` drops it,
    which turns a copy into an unwitnessed store — the safe direction)."""

    witness: Optional[Witness] = None


def witness_of(data: Any) -> Optional[Witness]:
    return getattr(data, "witness", None) if isinstance(data, WitnessedStore) else None


def attach_witness(data: Any, witness: Optional[Witness]) -> Any:
    if isinstance(data, WitnessedStore):
        data.witness = witness
    return data
