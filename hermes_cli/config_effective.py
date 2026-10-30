"""The effective USER config: config.yaml + managed overlay + ``${VAR}`` expansion, no defaults.

``load_config()`` merges ``DEFAULT_CONFIG`` first, which is wrong for readers that treat a
missing key as "unset" (the gateway's presence-sensitive env bridge, ``cfg == {}`` sentinels,
cron model pinning) — so nine surfaces used to hand-roll raw-read → overlay → expand in
differing orders and none of them replayed the model-key canonicalization or the last-known-good
recovery ``load_config()`` gained. This module is that one primitive.

Order matches ``_load_config_impl``: the user layer is expanded BEFORE the managed overlay so a
managed ``${VAR}`` resolves against the process environment only (``apply_managed_overlay``
expands it) and can never be re-resolved through a profile's secret scope
(docs/design/managed-scope.md §4.1). ``read_user_config_raw`` stays the write-back primitive.
"""

from __future__ import annotations

import copy
import itertools
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hermes_cli import config as _config
from hermes_cli import managed_scope

# path -> raw user mapping from the last successful parse in this process; served (through the
# normal pipeline) when the file is later found mid-edit as broken YAML.
_LAST_GOOD_USER_RAW: Dict[str, Dict[str, Any]] = {}
# path -> (*cache_sig, effective, env_snapshot); cache_sig = user + managed utils.content_signature.
_EFFECTIVE_CACHE: Dict[str, Tuple[Any, ...]] = {}

# Monotonically increasing observation id, stamped while ``_config._CONFIG_LOCK`` is held and
# published to the calling THREAD so a caller can order its own observation against a concurrent
# reader's without holding the config lock for the whole downstream publication.
#
# Why callers need it: readers that cache a derived answer (the gateway's per-home fallback-chain
# retention) observe under the lock but publish after it is released. Without an observation id,
# reader A could observe, be descheduled, reader B observe a NEWER state and publish it, and A then
# overwrite B's record with the older answer and a fresh timestamp — losing a managed revocation
# (R5-3). ``last_observation_epoch()`` is the read's own id; a stale publication is identifiable by
# comparing it with the one already on record.
_OBSERVATION_EPOCH = itertools.count(1)
_LAST_OBSERVATION = threading.local()


def last_observation_epoch() -> Optional[int]:
    """The observation id of THIS thread's most recent :func:`load_user_config_effective` call.

    ``None`` before this thread has read anything. Thread-local on purpose: it identifies the
    calling reader's own observation, which is exactly what a later publication must be ordered by.
    """
    return getattr(_LAST_OBSERVATION, "epoch", None)



class _ManagedConfigUnreadable(OSError):
    """The managed (admin) config.yaml exists but did not read, raised only under ``fail_closed``.

    An ``OSError``, because that is what the fail-closed callers' ``except Exception`` handlers
    already treat as "could not read the config; keep the last known-good state" — the same shape a
    torn user file produces.
    """


def user_config_bypassed() -> bool:
    """Is the user layer suppressed for this process (``--safe-mode`` / ``--ignore-user-config``)?

    THE predicate for that flag, so a reader never hand-rolls the env lookup and no surface can key
    on the wrong variable. ``hermes_cli/main.py:3027`` sets ``HERMES_IGNORE_USER_CONFIG`` for both
    flags (safe mode implies it); ``HERMES_SAFE_MODE``/``HERMES_IGNORE_RULES`` gate other subsystems
    and must NOT be consulted here — ``--ignore-user-config`` alone leaves both at their defaults.

    Deliberately NOT consulted by :func:`load_user_config_effective` itself. That function has ~20
    callers (cron scheduling, logging, time, send_message, TUI cwd lookup, doctor) whose contract
    with this flag is not part of this slice; a reader that must honour the bypass asks for it
    explicitly via ``ignore_user_config=``. The chain readers do so through
    ``gateway/run_config_loaders``, which owns "what chain may a new agent have".
    """
    return os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1"


def _effective(raw: Dict[str, Any], managed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    expanded = _config._expand_env_vars(raw)
    merged = managed_scope.apply_managed_overlay(expanded if isinstance(expanded, dict) else {}, managed)
    return _config._normalize_root_model_keys(merged if isinstance(merged, dict) else {})


def _recover_user_raw(config_path: Path, path_key: str, exc: Exception) -> Dict[str, Any]:
    """Last-known-good raw user mapping after a parse failure: this process's last good parse,
    else the newest ``good`` copy in backups/config/, else ``{}`` (warned as defaults)."""
    raw = _LAST_GOOD_USER_RAW.get(path_key)
    fallback = "last-known-good"
    if raw is None:
        from hermes_cli.config_backups import load_newest_good_backup
        raw = load_newest_good_backup(config_path)
        fallback = "last-known-good-backup"
    _config._warn_config_parse_failure(config_path, exc, fallback=fallback if raw is not None else "defaults")
    return copy.deepcopy(raw) if raw is not None else {}


def load_user_config_effective(config_path: Optional[Path] = None, *, fail_closed: bool = False,
                               ignore_user_config: bool = False) -> Dict[str, Any]:
    """User ``config.yaml`` → ``${VAR}`` expansion → managed overlay → model-key canonicalization.
    NO ``DEFAULT_CONFIG`` merge: a key absent from the file (and from the managed layer) is absent
    here, so ``{}`` sentinels and presence-sensitive bridges keep working. An absent file is an
    empty user layer (the managed layer still applies). Returns a fresh deepcopy.

    Broken YAML: ``fail_closed=True`` raises the parse error (for callers that keep their own
    last-good state); otherwise the last successfully parsed user file — in-process first, then
    the newest ``backups/config/*.good.*`` copy — is served through the same pipeline, so a
    mid-edit torn write never silently drops user overrides (same contract as ``load_config``).
    Cached on the user + managed file signatures and the values of every referenced env var.

    A failed MANAGED read is a failed read too (A44, and ``load_config`` already treats it as one).
    The admin layer can only ever REMOVE or override what the user file says — a managed
    ``fallback_providers: []`` suppresses a user chain — so applying an unreadable managed file as an
    empty overlay hands the caller the user's unconstrained config while reporting success. Every
    ``fail_closed`` caller holds its own last-known-good state for exactly this case, so it raises
    here as well.

    ``ignore_user_config=True`` is the ``--safe-mode`` / ``--ignore-user-config`` layer suppression
    (:func:`user_config_bypassed`), passed EXPLICITLY rather than read from the environment here, so
    this primitive's ~20 existing callers keep their current behaviour. The user layer becomes ``{}``
    — the parse and the ``_LAST_GOOD_USER_RAW`` / backup recovery are both skipped, so a suppressed
    file can re-enter through neither (the signature read in ``config.py`` still stats and reads the
    user bytes; it is the PARSE and the recovery that are suppressed, not the stat) — while the
    MANAGED overlay still applies and is still read fail-closed:
    the bypass suppresses the USER's choices, never the administrator's mandatory policy, and is
    not an escape hatch from A44. The observation epoch is stamped either way. Cached under a
    distinct key so a bypassed result can never be served to a non-bypassed caller or vice versa
    (one process runs both: a safe-mode CLI still hosts gateway/cron readers)."""
    if config_path is None:
        config_path = _config.get_config_path()
    # The bypass changes the ANSWER for the same path, so it has to change the cache identity too.
    path_key = f"{config_path}\x00no-user" if ignore_user_config else str(config_path)
    with _config._CONFIG_LOCK:
        # One content-keyed observation (A43): the signature, the bytes parsed below and a failed
        # read all come from the same read, so a same-metadata edit is never served stale.
        snap = _config._load_config_cache_sig(config_path)
        # Stamped under the lock, so observation order across concurrent readers is this counter's
        # order. A caller that publishes a derived answer AFTER releasing the lock reads it back via
        # last_observation_epoch() and can refuse to overwrite a newer observation (R5-3). Stamped
        # before the fail-closed raise below too: a failed read is an observation of failure, and a
        # caller serving its retained answer must not look older than it is.
        _LAST_OBSERVATION.epoch = next(_OBSERVATION_EPOCH)
        user_sig, cache_sig = snap.user_sig, snap.cache_sig
        if fail_closed and snap.managed_error is not None:
            raise _ManagedConfigUnreadable(str(snap.managed_error))
        cached = _EFFECTIVE_CACHE.get(path_key)
        n = len(cache_sig) if cache_sig is not None else 0
        if cached is not None and cache_sig is not None and tuple(cached[:n]) == tuple(cache_sig):
            if all(_config._env_ref_lookup(k) == v for k, v in cached[n + 1].items()):
                return copy.deepcopy(cached[n])

        raw: Dict[str, Any] = {}
        recovered = False
        if ignore_user_config:
            # The whole user layer is suppressed: skip the parse AND the last-known-good recovery.
            # Recovering here would be the bypass's own leak — serving a remembered copy of exactly
            # the file the operator asked to ignore. A torn user file is therefore not an error in
            # this mode either; only the managed read above can fail closed.
            user_sig = None
        raw_hit = _config._raw_config_cache_hit(path_key, user_sig) if snap.user_error is None and user_sig else None
        if raw_hit is not None:
            raw = copy.deepcopy(raw_hit)  # one parse per process, shared with read_raw_config()
            _LAST_GOOD_USER_RAW.setdefault(path_key, copy.deepcopy(raw))
        elif user_sig is not None:
            try:
                if snap.user_error is not None:
                    raise snap.user_error
                loaded = _config._parse_config_bytes(snap.user_bytes)
            except Exception as exc:
                if fail_closed:
                    raise
                raw, recovered = _recover_user_raw(config_path, path_key, exc), True
            else:
                raw = loaded if isinstance(loaded, dict) else {}
                _config._clear_parse_failure_after_clean_read(config_path, user_sig)
                _config._RAW_CONFIG_CACHE[path_key] = (*user_sig, copy.deepcopy(raw))
                _LAST_GOOD_USER_RAW[path_key] = copy.deepcopy(raw)
                # Same copy load_config keeps: a fresh process recovers from it (see _recover_user_raw).
                # Only for the ACTIVE home — a read of another profile's file (doctor, TUI cwd lookup)
                # must not create backups/ inside that profile.
                if config_path == _config.get_config_path():
                    from hermes_cli.config_backups import backup_config
                    backup_config(config_path, "good")

        env_snapshot = _config._env_ref_snapshot(raw)
        managed = snap.managed_value  # the managed bytes cache_sig signed (A43)
        if managed:
            _config._env_ref_snapshot(managed, env_snapshot)
        effective = _effective(raw, managed)
        # A recovered result is never cached under the corrupt file's signature: a later
        # ``fail_closed`` caller must still see the parse error, not a cache hit.
        if cache_sig is not None and not recovered:
            _EFFECTIVE_CACHE[path_key] = (*cache_sig, copy.deepcopy(effective), env_snapshot)
        else:
            _EFFECTIVE_CACHE.pop(path_key, None)
        return effective
