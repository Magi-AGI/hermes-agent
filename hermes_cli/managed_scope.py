"""Managed scope — IT-pushed, user-immutable config & env layer.

DISTINCT from ``hermes_cli.config.is_managed()`` / ``HERMES_MANAGED`` (a coarse package-manager
write-lock that blocks all mutation); this layer injects specific immutable values. The two are
independent and may coexist. v1 enforcement is filesystem permissions only (see
``docs/design/managed-scope.md`` §7); ``get_managed_dir()`` is the single seam for adding
macOS / Windows native locations later.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional

import yaml

# Stale-module bridge: this module binds ``utils.content_signature`` at import time, so a fresh
# import in a post-pull updater process (pre-handoff purge keeps root modules cached) dies
# unless the stale ``utils`` is dropped first. See hermes_cli.stale_modules.
from hermes_cli.stale_modules import drop_stale_root_modules

drop_stale_root_modules()

from utils import content_signature

logger = logging.getLogger(__name__)

# POSIX default. Other-platform locations belong ONLY inside get_managed_dir().
_DEFAULT_MANAGED_DIR = Path("/etc/hermes")

_CACHE_LOCK = threading.Lock()
# path_key -> (*utils.content_signature, parsed)
_CONFIG_CACHE: Dict[str, tuple] = {}
_ENV_CACHE: Dict[str, tuple] = {}


def _under_pytest() -> bool:
    """True inside the test suite: ignore the system ``/etc/hermes`` so a real managed scope on a
    dev/CI box can't leak policy into the suite. An explicit ``HERMES_MANAGED_DIR`` still wins."""
    return "PYTEST_CURRENT_TEST" in os.environ


def get_managed_dir() -> Optional[Path]:
    """Resolve the managed-scope directory, or None when no scope is present.

    Priority: ``$HERMES_MANAGED_DIR`` (IT-only bootstrap override; never persisted to any .env;
    honored only when non-empty AND the directory exists), then ``/etc/hermes`` when it exists.
    A missing directory resolves to None — the common case, so it must be cheap + side-effect-free.
    """
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if override:
        p = Path(override)
    elif _under_pytest():
        return None
    else:
        p = _DEFAULT_MANAGED_DIR
    return p if p.is_dir() else None


def invalidate_managed_cache() -> None:
    """Drop cached managed config/env. For tests and post-edit reloads."""
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()
        _ENV_CACHE.clear()
        _VANISHED_SINCE.clear()
    global _LAST_MANAGED_DIR
    _LAST_MANAGED_DIR = None


class ManagedRead(NamedTuple):
    """One observation of a managed file. ``signature`` is ``utils.content_signature`` of the bytes that
    ``value`` was parsed from (``ABSENT_SIGNATURE`` when there is no file). ``error`` is set when the file
    exists (or was just seen) but could not be read or parsed; ``value`` is then ``{}`` (fail-open for
    loading) and the caller decides whether a failed authoritative read may drive a decision (A44).
    ``error_kind``: "read" (retryable: unreadable, vanished mid-replace, or changed while read) or
    "parse" (complete, stable bytes that are not a YAML mapping)."""
    signature: tuple
    value: dict
    error: Optional[str]
    error_kind: Optional[str] = None


ABSENT_SIGNATURE = (0, 0, 0, 0, b"")
_ABSENT = ManagedRead(ABSENT_SIGNATURE, {}, None)
# A managed file this process has read that is now missing reads as a FAILED read for this long before
# it counts as removed: a delete-then-write editor or a replace must not lift the admin's policy for
# that window, while a real removal still takes effect (A43 closure r2, finding 6).
VANISHED_GRACE_SECONDS = 5.0
_VANISHED_SINCE: Dict[str, float] = {}


def _read_managed_bytes(path: Path) -> tuple:
    """``(content_signature, bytes)`` from ONE open: the cache key and the parse cover the same bytes."""
    with open(path, "rb") as handle:
        st = os.fstat(handle.fileno())
        data = handle.read()
    return content_signature(st, data), data


def _cached_read(path: Path, cache: Dict[str, tuple], parse) -> ManagedRead:
    """Shared content-keyed read of one managed file; ``value`` is a deepcopy of the parse.

    Absent -> ``_ABSENT`` (a file read before counts as absent only after ``VANISHED_GRACE_SECONDS``).
    Unreadable or unparseable (including a non-mapping root) -> ``error`` set, never cached, logged
    LOUDLY (the admin needs to know their policy isn't applied) but never raised, so a malformed
    managed file can't brick startup."""
    path_key = str(path)
    try:
        key, data = _read_managed_bytes(path)
    except FileNotFoundError as exc:
        with _CACHE_LOCK:
            known = path_key in cache
            if not known:
                return _ABSENT
            since = _VANISHED_SINCE.setdefault(path_key, time.monotonic())
            if time.monotonic() - since < VANISHED_GRACE_SECONDS:
                return _managed_failure(path, "read", exc)
            cache.pop(path_key, None)
            _VANISHED_SINCE.pop(path_key, None)
        return _ABSENT
    except OSError as exc:
        return _managed_failure(path, "read", exc)
    with _CACHE_LOCK:
        _VANISHED_SINCE.pop(path_key, None)
        hit = cache.get(path_key)
        if hit is not None and hit[:len(key)] == key:
            return ManagedRead(key, copy.deepcopy(hit[len(key)]), None)
    try:
        parsed = parse(data)
        if not isinstance(parsed, dict):
            raise ValueError(f"top level is a {type(parsed).__name__}, not a mapping of settings")
    except Exception as exc:  # noqa: BLE001 — fail-open, but LOUD
        try:
            stable = _read_managed_bytes(path)[1] == data
        except OSError:
            stable = False
        return _managed_failure(path, "parse" if stable else "read", exc)
    with _CACHE_LOCK:
        cache[path_key] = (*key, copy.deepcopy(parsed))
    return ManagedRead(key, parsed, None)


def _managed_failure(path: Path, kind: str, exc: Exception) -> ManagedRead:
    verb = "parsed" if kind == "parse" else "read"
    logger.warning(
        "managed scope: %s could not be %s: %s — IGNORING this managed file. "
        "Admin policy from this file is NOT being applied. Fix and restart.",
        path, verb, exc)
    return ManagedRead(ABSENT_SIGNATURE, {}, f"managed file {path} could not be {verb} ({exc})", kind)


# The managed directory last resolved: when it disappears (renamed/replaced), its files are "known files
# that vanished" and go through the same grace as a vanished file instead of reading as never present.
_LAST_MANAGED_DIR: Optional[Path] = None


def _read_managed_file(name: str, cache: Dict[str, tuple], parse) -> ManagedRead:
    global _LAST_MANAGED_DIR
    managed_dir = get_managed_dir()
    if managed_dir is None:
        previous = _LAST_MANAGED_DIR
        if previous is None:
            return _ABSENT
        with _CACHE_LOCK:
            known = str(previous / name) in cache
        if not known:
            return _ABSENT
        # _cached_read sees FileNotFoundError on a known file: failed read inside the grace window,
        # clean absence (and the entry dropped) after it.
        return _cached_read(previous / name, cache, parse)
    _LAST_MANAGED_DIR = managed_dir
    return _cached_read(managed_dir / name, cache, parse)


def _parse_managed_yaml(data: bytes) -> Any:
    """The parsed document, validated by the caller. Only an EMPTY document (blank or comments-only
    file) is the intentional "no policy" ``{}``: an explicit ``null``/``false``/``0``/``[]`` root is
    returned as-is so the mapping check rejects it (closure r3: ``or {}`` accepted them as empty)."""
    text = data.decode("utf-8")
    if yaml.compose(text) is None:
        return {}
    return yaml.safe_load(text)


def read_managed_config() -> ManagedRead:
    """The managed config.yaml as one observation (signature, mapping, read/parse error)."""
    return _read_managed_file("config.yaml", _CONFIG_CACHE, _parse_managed_yaml)


def load_managed_config() -> dict:
    """Parsed managed config.yaml, or {} when absent/malformed (fail-open)."""
    return read_managed_config().value


def load_managed_env() -> Dict[str, str]:
    """Parsed managed .env (KEY=VALUE), or {} when absent (fail-open)."""
    return _read_managed_file(".env", _ENV_CACHE, _parse_managed_env).value


def _parse_managed_env(data: bytes) -> Dict[str, str]:
    from agent.secret_scope import _parse_env_text

    # utf-8-sig: strips a BOM; strict, because an admin file must fail LOUD (load_env_file would
    # silently fall back to latin-1).
    return _parse_env_text(data.decode("utf-8-sig"))


def apply_managed_overlay(config: dict, managed: Optional[dict] = None) -> dict:
    """Overlay administrator-pinned config values on top of an already-built dict.

    ``${VAR}`` refs in the managed config expand against the PROCESS env only, so a user cannot
    shadow a managed literal via a ref they control; a bare root ``model: x/y`` string is promoted
    to ``model.default`` so it can't clobber the dict shape callers expect; managed values
    deep-merge ON TOP per leaf while sibling keys stay user-controlled. Fail-open: returns
    ``config`` unchanged when no scope is present or on any error. Mutates and returns ``config``.
    ``managed``: an already-observed managed mapping (from :func:`read_managed_config`), so a caller
    that signed the managed file overlays exactly what it signed; None reads it here.
    """
    try:
        if managed is None:
            managed = load_managed_config()
        if not managed:
            return config
        # Imported lazily to avoid an import cycle (config imports managed_scope).
        from hermes_cli.config import _deep_merge, _expand_env_vars, _normalize_root_model_keys
        managed_expanded = _normalize_root_model_keys(_expand_env_vars(managed))
        # _normalize_root_model_keys only promotes the string when root provider/base_url
        # keys exist to migrate; handle the bare case here (matches cli.py) so _deep_merge
        # never replaces the caller's ``model`` dict with a string.
        if isinstance(managed_expanded.get("model"), str):
            managed_expanded = dict(managed_expanded)
            managed_expanded["model"] = {"default": managed_expanded["model"]}
        return _deep_merge(config, managed_expanded)
    except Exception:  # noqa: BLE001 — overlay must never break a caller
        logger.warning("managed scope: failed to apply config overlay", exc_info=True)
        return config


def _flatten_keys(d: dict, prefix: str = "") -> set:
    keys: set = set()
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            keys |= _flatten_keys(v, dotted)
        else:
            keys.add(dotted)
    return keys


def managed_config_keys() -> set:
    """Dotted leaf keys pinned by the managed config (e.g. {'model.default'})."""
    return _flatten_keys(load_managed_config())


def is_key_managed(dotted_key: str) -> bool:
    """True if the exact dotted config key is pinned by the managed layer."""
    return dotted_key in managed_config_keys()


def is_env_managed(name: str) -> bool:
    """True if the env var name is pinned by the managed .env layer."""
    return name in load_managed_env()
