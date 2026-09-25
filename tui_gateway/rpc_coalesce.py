"""Coalescing identities for the idempotent reads every window issues at once.

Opening the model picker, a reconnect burst, or a ``sessions.changed`` broadcast makes 20+
windows ask the same backend for the same payload within a second. :class:`~tui_gateway.rpc_lanes.
SingleFlight` lets identical in-flight requests share one build. The key must capture everything
the result depends on, so a request never receives a payload built from inputs it would not have
seen itself:

- ``model.options``: profile home, the caller's picker context (the live session's provider /
  model / base URL overlay), the request flags, and a stat fingerprint of the files the build
  reads (config.yaml, .env, auth.json), so a key save or config write forces a fresh build.
  ``refresh=True`` is never coalesced: it is an explicit re-probe.
- ``session.list``: the database file, the request flags, and the database's stat fingerprint
  (main file + WAL), so a client refetching after ``sessions.changed`` never joins a build that
  started before the change.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Hashable

from tui_gateway.rpc_lanes import SingleFlight
from utils import file_signature

MODEL_OPTIONS = SingleFlight()
SESSION_LIST = SingleFlight()

_MODEL_OPTIONS_INPUT_FILES = ("config.yaml", ".env", "auth.json")


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
    home_path = Path(home)
    context = json.dumps(dataclasses.asdict(ctx), sort_keys=True, default=str)
    return (str(home_path), context, bool(params.get("explicit_only")), bool(params.get("include_unconfigured")),
            _stat_fingerprint(home_path / name for name in _MODEL_OPTIONS_INPUT_FILES))


def session_list_key(db: Any, *, limit: int, include_hidden: bool) -> Hashable | None:
    """Coalescing identity for a ``session.list`` query, or None when the database has no path."""
    db_path = getattr(db, "db_path", None)
    if db_path is None:
        return None
    db_path = Path(db_path)
    return (str(db_path), limit, include_hidden,
            _stat_fingerprint((db_path, db_path.with_name(db_path.name + "-wal"))))
