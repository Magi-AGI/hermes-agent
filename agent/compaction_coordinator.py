"""Root-scoped path resolution for the cross-session compaction coordinator.

**Step B — path helper ONLY.** No DB is opened, no schema is created, no slot is
acquired, and nothing calls this yet. The slot table, the leased semaphore, and
the compaction wiring land in later phases, gated on Gate 0 passing against this
helper. See ``docs/plans/2026-07-11-compaction-queue-spec.md`` §4.2 / §9.0-GATE.

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

import os
from pathlib import Path

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
