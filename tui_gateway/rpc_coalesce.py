"""Coalescing identities for the idempotent reads every window issues at once.

Opening the model picker, a reconnect burst, or a ``sessions.changed`` broadcast makes 20+
windows ask the same backend for the same payload within a second. :class:`~tui_gateway.rpc_lanes.
SingleFlight` lets identical in-flight requests share one build. The key must capture everything
the result depends on, so a request never receives a payload built from inputs it would not have
seen itself:

- ``model.options``: profile home, the caller's picker context (the live session's provider /
  model / base URL overlay), the request flags, and a stat fingerprint of every file the build
  reads: the effective config (user + managed ``config.yaml``, the loader's own cache signature),
  the profile ``.env`` and ``auth.json``, and the managed ``.env``, so a key save or any config
  write forces a fresh build. ``refresh=True`` is never coalesced: it is an explicit re-probe.
- ``session.list``: the database file, the request flags, and the database's stat fingerprint
  (main file + WAL), so a client refetching after ``sessions.changed`` never joins a build that
  started before the change.

A joiner never blocks a lane worker on the shared build (:func:`respond`): it returns at once and
its response is written when the build finishes, so every queued identical request reaches the
in-flight build instead of starting another one after it.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Callable, Hashable

from tui_gateway.rpc_lanes import SingleFlight
from utils import file_signature

MODEL_OPTIONS = SingleFlight()
SESSION_LIST = SingleFlight()

_MODEL_OPTIONS_PROFILE_FILES = (".env", "auth.json")


def _stat_fingerprint(paths) -> tuple:
    out = []
    for path in paths:
        try:
            out.append(file_signature(os.stat(path)))
        except OSError:
            out.append(None)
    return tuple(out)


def model_options_key(home: Any, ctx: Any, params: dict) -> Hashable | None:
    """Coalescing identity for a ``model.options`` build, or None when it must run alone."""
    if params.get("refresh") or not dataclasses.is_dataclass(ctx):
        return None
    from hermes_cli.config import _load_config_cache_sig, get_config_path
    from hermes_cli.managed_scope import get_managed_dir

    home_path = Path(home)
    managed_dir = get_managed_dir()
    context = json.dumps(dataclasses.asdict(ctx), sort_keys=True, default=str)
    inputs = (_load_config_cache_sig(get_config_path())[1],
              _stat_fingerprint([*(home_path / name for name in _MODEL_OPTIONS_PROFILE_FILES),
                                 *([managed_dir / ".env"] if managed_dir else [])]))
    return (str(home_path), context, bool(params.get("explicit_only")), bool(params.get("include_unconfigured")),
            inputs)


def session_list_key(db: Any, *, limit: int, include_hidden: bool) -> Hashable | None:
    """Coalescing identity for a ``session.list`` query, or None when the database has no path."""
    db_path = getattr(db, "db_path", None)
    if db_path is None:
        return None
    db_path = Path(db_path)
    return (str(db_path), limit, include_hidden,
            _stat_fingerprint((db_path, db_path.with_name(db_path.name + "-wal"))))


def respond(flight: SingleFlight, key: Hashable | None, rid: Any, compute: Callable[[], Any], *,
            error_code: int) -> dict | None:
    """The handler's response for *compute* coalesced on *key*.

    The leader computes and returns its response. A joiner returns None at once, freeing its lane
    worker, and its own response is written to its own transport when the shared build finishes. It
    holds its own retirement admission until then, like any request with a pending response."""
    from hermes_cli.backend_retirement import retirement
    from tui_gateway.server import _err, _ok
    from tui_gateway.transport import current_transport

    if key is None:
        return _ok(rid, compute())
    shared, leader = flight.claim(key)
    if leader:
        return _ok(rid, flight.lead(key, shared, compute))
    transport = current_transport()
    if transport is None or not retirement.acquire():
        return _ok(rid, shared.result())

    def deliver(done) -> None:
        try:
            try:
                response = _ok(rid, done.result())
            except Exception as exc:
                response = _err(rid, error_code, str(exc))
            transport.write(response)
        finally:
            retirement.release()

    shared.add_done_callback(deliver)
    return None
