"""Registering a connected (or schema-cached) MCP server's tools into the tool registry:
include/exclude filtering, trust-tier metadata capture, utility-tool selection, name-collision
resolution and the schema-cache write-through. Both entry points (``_register_server_tools``
live, ``_register_from_cache_sync`` lazy) build ``_Candidate`` records for ``_register_candidates``."""

import json
import logging
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional
from tools.mcp_tool_common import _parse_boolish, _core, _resolve_tool_timeout, mcp_field
from tools import mcp_tool_handlers as _handlers
from tools import mcp_tool_schema as _schema
from tools.mcp_tool_handlers import (
    _make_check_fn, _make_get_prompt_handler, _make_list_prompts_handler,
    _make_list_resources_handler, _make_read_resource_handler)
from tools.mcp_tool_schema import (
    _UTILITY_CAPABILITY_ATTRS, _build_utility_schemas, _normalize_name_filter, matches_name_filter)
from tools.mcp_tool_scope import _key_name, _key_scope, _resolve_server_key, _server_key

if TYPE_CHECKING:  # pragma: no cover
    from tools.mcp_tool import MCPServerTask

logger = logging.getLogger("tools.mcp_tool")
_SCOPE_REFRESH_LOCKS = tuple(threading.RLock() for _ in range(16))

_UTILITY_ORIGIN_PREFIX = "generated utility "
# Utility tool key -> handler factory; each takes (server_name, tool_timeout).
_UTILITY_HANDLER_FACTORIES = {
    "list_resources": _make_list_resources_handler, "read_resource": _make_read_resource_handler,
    "list_prompts": _make_list_prompts_handler, "get_prompt": _make_get_prompt_handler}


def _normalize_server_trust(value: Any) -> str:
    """Config ``trust`` -> tier. None -> ``full`` (compat default); unrecognized -> ``untrusted`` (fail closed)."""
    if value is None:
        return _core._TRUST_FULL
    text = str(value).strip().lower()
    if text in (_core._TRUST_FULL, _core._TRUST_UNTRUSTED):
        return text
    logger.warning("MCP trust: unrecognized trust value %r — treating as 'untrusted' (valid values: full, untrusted)",
                   value)
    return _core._TRUST_UNTRUSTED


def _annotation_read_only_hint(mcp_tool: Any) -> bool:
    """True only when annotations (SDK object or cache dict) carry ``readOnlyHint is True``; unknown = write-capable."""
    annotations = getattr(mcp_tool, "annotations", None)
    hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) else getattr(annotations, "readOnlyHint", None)
    return hint is True


def _record_tool_trust_metadata(server_name: str, config: dict, tools: List[Any], key=None) -> None:
    """Capture per-server trust and per-tool readOnlyHint at discovery — the security boundary: the call-time gate
    classifies from data we control, never re-read server-supplied state. *key* is the connection (default: the
    registering profile's own); the ``trust`` policy is recorded under it for the profile that owns it — an
    adopting profile records its own policy in ``_record_scope_trust``."""
    with _core._lock:
        if key is None:
            key = _server_key(server_name)
        _core._server_trust_levels[key] = _normalize_server_trust((config or {}).get("trust"))
        hints = _core._tool_read_only_hints.setdefault(key, {})
        hints.update({t.name: _annotation_read_only_hint(t) for t in tools if getattr(t, "name", None)})


def _record_scope_trust(server_name: str, config: dict, scope: str) -> None:
    """``trust`` is the CONSUMING profile's policy, never the connection's: an ``untrusted`` profile that
    adopts a ``full`` profile's live connection must still be asked before every write-capable call."""
    with _core._lock:
        _core._server_trust_levels[_server_key(server_name, scope, current=False)] = _normalize_server_trust(
            (config or {}).get("trust"))


def _track_mcp_tool_server(tool_name: str, server_name: str) -> None:
    """Remember the exact raw MCP server that registered *tool_name*."""
    with _core._lock:
        _core._mcp_tool_server_names[tool_name] = server_name


def _forget_mcp_tool_server(tool_name: str) -> None:
    """Forget MCP server provenance for a deregistered tool."""
    with _core._lock:
        _core._mcp_tool_server_names.pop(tool_name, None)


def _server_key_for_task(server) -> object:
    """Connection key of a live ``MCPServerTask`` (teardown runs on the MCP loop without the
    discovering profile's context, so the key is found by identity, never re-derived)."""
    with _core._lock:
        for key, live in _core._servers.items():
            if live is server:
                return key
    return _server_key(server.name)


def _deregister_mcp_tool_all_scopes(server, tool_name: str) -> None:
    """Deregister one server tool from every profile overlay that owns it. *server* is the
    live task or a connection key."""
    from tools.registry import registry

    key = server if isinstance(server, (str, tuple)) else _server_key_for_task(server)
    with _core._lock:
        scopes = set(_core._server_tool_scopes.get(key, ()))
        if not scopes:
            scopes = {_core._server_registry_scope(key)}
    for scope in scopes:
        registry.deregister(tool_name, scope=scope)
    _forget_mcp_tool_server(tool_name)
    _restore_server_toolset_alias(key)


def _restore_server_toolset_alias(key) -> None:
    """Keep the process-global alias while any profile still owns tools of a server with this
    name — including another profile's same-named connection (the alias is per NAME; the
    registry deregister dropped it after checking only one scope)."""
    from tools.registry import registry

    server_name = _key_name(key)
    with _core._lock:
        scopes = {None}
        for k in list(_core._servers) + list(_core._lazy_server_configs):
            if _key_name(k) == server_name:
                scopes.add(_key_scope(k))
                scopes.update(_core._server_tool_scopes.get(k, ()))
        scopes.update(scope for (k, scope) in _core._server_scope_configs if _key_name(k) == server_name)
    # Any profile's slot (owner, adopter-only, or lazy stubs), not only the owner's names.
    if any(registry.get_tool_names_for_toolset_in_scope(f"mcp-{server_name}", scope) for scope in scopes):
        registry.register_toolset_alias(server_name, f"mcp-{server_name}")


def _remove_server_scope(key, scope: str) -> None:
    """One profile stops using a shared live connection: a policy LOSS, recomputed (its live entries
    go; its membership, policy and trust record go)."""
    _set_profile_policy(key, scope, None)


def _select_utility_schemas(server_name: str, server: "MCPServerTask", config: dict) -> List[dict]:
    """Utility schemas allowed by config (``tools.resources``/``tools.prompts``) and advertised
    capabilities. ``initialize_result.capabilities`` is the truth (sub-object non-None iff the
    family is served); without it fall back to the legacy session-method check, which never
    filters anything since ClientSession defines all four methods."""
    tools_filter = config.get("tools") or {}
    enabled = {f: _parse_boolish(tools_filter.get(f), default=True) for f in ("resources", "prompts")}
    advertised = getattr(getattr(server, "initialize_result", None), "capabilities", None)

    def _skip_reason(handler_key: str) -> Optional[str]:
        family = _UTILITY_CAPABILITY_ATTRS[handler_key]
        if not enabled[family]:
            return f"{family} disabled"
        if advertised is not None:
            if getattr(advertised, family, None) is None:
                return f"server does not advertise '{family}' capability"
            return None
        # Legacy gate (no initialize_result): the ClientSession method shares the handler key.
        return None if hasattr(server.session, handler_key) else f"session lacks {handler_key}"
    selected: List[dict] = []
    for entry in _build_utility_schemas(server_name):
        reason = _skip_reason(entry["handler_key"])
        if reason:
            logger.debug("MCP server '%s': skipping utility '%s' (%s)", server_name, entry["handler_key"], reason)
        else:
            selected.append(entry)
    return selected


def _existing_tool_names() -> List[str]:
    """Tool names for all connected servers plus lazy (cache-registered) servers, whose tools live only in the registry."""
    scope = _core._mcp_registry_scope()
    if scope is not None:
        from tools.registry import registry

        with _core._lock:
            server_names = [
                _key_name(key) for key in _core._servers
                if _core._server_visible_in_scope(key, scope)
            ]
            server_names.extend(
                _key_name(key) for key in _core._lazy_server_tool_names
                if key not in _core._servers and _core._server_visible_in_scope(key, scope)
            )
        return sorted({
            tool_name
            for server_name in server_names
            for tool_name in registry.get_tool_names_for_toolset(f"mcp-{server_name}")
        })

    names: List[str] = []
    for server in _core._servers.values():
        names.extend(server._registered_tool_names if hasattr(server, "_registered_tool_names")
                     else (_schema._convert_mcp_schema(server.name, t)["name"] for t in server._tools))
    with _core._lock:
        names.extend(n for key, tool_names in _core._lazy_server_tool_names.items()
                     if key not in _core._servers for n in tool_names)
    return names


def _make_tool_filter(name: str, config: dict) -> Callable[[str], bool]:
    """Include/exclude predicate for a server's tool names: ``tools.include`` is a whitelist (``[]`` = register
    nothing), ``tools.exclude`` a blacklist; entries are exact names or fnmatch globs; include wins over exclude."""
    tools_filter = config.get("tools") or {}
    # Selective tool loading: honour include/exclude lists from config. Rules (matching issue #690 spec,
    # extended with glob support): tools.include — whitelist: only matching tool names are registered
    # tools.exclude — blacklist: all tools EXCEPT matching ones are registered entries may be exact names or
    # fnmatch globs (e.g. "*_radar_*") include takes precedence over exclude include: [] → register nothing
    # (an explicit empty whitelist, as written by the install checklist's "uncheck everything" path) Neither
    # set → register all tools (backward-compatible default)
    include_raw = tools_filter.get("include")
    include_set = _normalize_name_filter(include_raw, f"mcp_servers.{name}.tools.include")
    exclude_set = _normalize_name_filter(tools_filter.get("exclude"), f"mcp_servers.{name}.tools.exclude")
    if isinstance(include_raw, (str, list, tuple, set)):
        return lambda tool_name: matches_name_filter(tool_name, include_set)
    return lambda tool_name: not (exclude_set and matches_name_filter(tool_name, exclude_set))


def _cached_tools(raws: Iterable[Any]) -> List[SimpleNamespace]:
    """Schema-cache rows -> stand-ins for MCP Tool objects; rows that are not dicts or lack a name
    are dropped. Missing or non-dict ``annotations`` (older cache files) fail closed to write-capable."""
    return [SimpleNamespace(name=raw["name"], description=raw.get("description") or "",
                            inputSchema=raw["inputSchema"] if isinstance(raw.get("inputSchema"), dict) else {},
                            annotations=raw["annotations"] if isinstance(raw.get("annotations"), dict) else None)
            for raw in raws if isinstance(raw, dict) and raw.get("name")]


@dataclass
class _Candidate:
    """One registration attempt (native tool or generated utility); ``origin`` is the provenance text in diagnostics."""

    registry_name: str
    origin: str
    schema: dict
    handler: Callable

    @property
    def is_utility(self) -> bool:
        return self.origin.startswith(_UTILITY_ORIGIN_PREFIX)


def _tool_candidates(name: str, tools: Iterable[Any], should_register: Callable[[str], bool],
                     tool_timeout) -> List[_Candidate]:
    """Native tools (live SDK objects or cache stand-ins) -> candidates. The injection scan runs on
    BOTH paths: the cache file is user-writable JSON."""
    out: List[_Candidate] = []
    for t in tools:
        if not should_register(t.name):
            logger.debug("MCP server '%s': skipping tool '%s' (filtered by config)", name, t.name)
            continue
        _schema._scan_mcp_description(name, t.name, t.description or "")
        schema = _schema._convert_mcp_schema(name, t)
        handler = _handlers._make_tool_handler(name, t.name, tool_timeout)
        out.append(_Candidate(schema["name"], f"tool {t.name!r}", schema, handler))
    return out


def _utility_candidates(name: str, entries: Iterable[Any], tool_timeout) -> List[_Candidate]:
    """``{schema, handler_key}`` rows (live selection or cache) -> candidates; malformed rows dropped."""
    out: List[_Candidate] = []
    for raw in entries:
        schema, key = (raw.get("schema"), raw.get("handler_key")) if isinstance(raw, dict) else (None, None)
        if isinstance(schema, dict) and key in _UTILITY_HANDLER_FACTORIES and schema.get("name"):
            out.append(_Candidate(schema["name"], f"{_UTILITY_ORIGIN_PREFIX}{key!r}", schema,
                                  _UTILITY_HANDLER_FACTORIES[key](name, tool_timeout)))
    return out


def _resolve_name_collisions(name: str, candidates: List[_Candidate]) -> List[_Candidate]:
    """Preflight name collisions: exact duplicates dropped silently; a utility normalizing onto
    a native tool's name is shadowed (native wins); any other multi-origin collision skips every
    colliding entry (fail closed). Returns survivors in order."""
    unique: List[_Candidate] = []
    origins_by_name: Dict[str, set[str]] = {}
    for c in candidates:
        origins = origins_by_name.setdefault(c.registry_name, set())
        if c.origin in origins:
            logger.debug("MCP server '%s': duplicate registration candidate %s for '%s'; keeping one",
                         name, c.origin, c.registry_name)
            continue
        origins.add(c.origin)
        unique.append(c)
    ambiguous: Dict[str, List[str]] = {}
    shadowed: set[tuple[str, str]] = set()
    # A generated resource/prompt utility that normalizes onto a server-native tool's name must not knock
    # that native tool out of the registry: the native tool is the capability the user connected the server
    # for, while the generated utility (read_resource/list_resources/list_prompts/get_prompt) is optional
    # sugar that only matters when the server exposes no such tool of its own (#87112). Resolve that
    # specific collision in favour of the native tool — keep it, drop the shadowed utility — and fall back
    # to the conservative skip-everything only for genuinely ambiguous collisions (two or more native tools
    # normalizing to one name, which we cannot disambiguate). The four utility keys are distinct, so a
    # colliding set holds at most one utility origin.
    for registry_name, origins in origins_by_name.items():
        if len(origins) <= 1:
            continue
        utility_origins = sorted(o for o in origins if o.startswith(_UTILITY_ORIGIN_PREFIX))
        native_origins = sorted(origins - set(utility_origins))
        if len(native_origins) == 1 and utility_origins:
            shadowed.update((registry_name, o) for o in utility_origins)
            logger.info(
                "MCP server '%s': generated utility %s normalizes onto server-native %s — keeping the native tool "
                "and dropping the utility (the utility only applies when the server has no such tool of its own)",
                name, ", ".join(utility_origins), native_origins[0])
        else:
            ambiguous[registry_name] = sorted(origins)
    for registry_name, origins in sorted(ambiguous.items()):
        logger.error("MCP server '%s': name normalization collision for '%s' from %s; skipping every colliding "
                     "entry instead of choosing an arbitrary handler", name, registry_name, ", ".join(origins))
    return [c for c in unique if c.registry_name not in ambiguous and (c.registry_name, c.origin) not in shadowed]


def _register_candidates(name: str, candidates: List[_Candidate], *, check_fn: Callable,
                         scope: Callable[[], Optional[str]], lazy: bool) -> List[str]:
    """Register LAZY (schema-cache) stubs under toolset ``mcp-{name}``; returns the names that
    landed. A live connection's per-profile entries are written only by ``_reconcile_profile``.
    The ownership pre-check is advisory: ``registry.register()`` is the atomic gate and its
    verdict is re-read after every call."""
    from tools.registry import registry
    toolset_name = f"mcp-{name}"
    registered: List[str] = []
    scope_value = scope()
    for c in candidates:
        existing_toolset = registry.get_toolset_for_tool(c.registry_name)
        if existing_toolset and existing_toolset != toolset_name:  # foreign owner: skip, preserve it
            if lazy:
                if not c.is_utility:
                    logger.warning("MCP server '%s' (lazy): cached tool '%s' collides with toolset '%s' — skipping",
                                   name, c.registry_name, existing_toolset)
            elif existing_toolset.startswith("mcp-"):
                logger.error("MCP server '%s': %s normalizes to '%s', already owned by MCP toolset '%s' — skipping to "
                             "preserve the existing owner", name, c.origin, c.registry_name, existing_toolset)
            else:
                logger.warning("MCP server '%s': %s (→ '%s') collides with built-in tool in toolset '%s' — skipping to "
                               "preserve built-in", name, c.origin, c.registry_name, existing_toolset)
            continue
        registry.register(
            name=c.registry_name, toolset=toolset_name, schema=c.schema, handler=c.handler, check_fn=check_fn,
            is_async=False, description=c.schema.get("description") or "", scope=scope_value)
        if registry.get_toolset_for_tool(c.registry_name) == toolset_name:
            _track_mcp_tool_server(c.registry_name, name)
            registered.append(c.registry_name)
        elif not lazy:
            logger.error("MCP server '%s': registration of %s as '%s' was rejected by the registry; "
                         "skipping provenance/count updates", name, c.origin, c.registry_name)
    if registered:
        registry.register_toolset_alias(name, toolset_name)
    return registered


def _tools_payload(server: "MCPServerTask", should_register) -> List[dict]:
    """The cache rows for ``server._tools`` that *should_register* admits."""
    rows = []
    for t in server._tools:
        if not should_register(t.name):
            continue
        # mcp 2.0 renamed every Tool model field to snake_case and left camelCase as a
        # *serialization* alias only, which pydantic does not apply to attribute access: a bare
        # camelCase getattr returns None on 2.x instead of raising. That silently wrote an empty
        # ``inputSchema`` into the schema cache on every write-through, so a ``lazy: true`` server
        # registered from cache with every parameter stripped. ``mcp_field`` reads both spellings.
        schema_obj = mcp_field(t, "input_schema", "inputSchema")
        rows.append({
            "name": t.name, "description": t.description or "",
            "inputSchema": schema_obj if isinstance(schema_obj, dict) else {},
            "annotations": {"readOnlyHint": _annotation_read_only_hint(t)},  # lazy path trust-gates identically
        })
    return rows


def _write_schema_cache(name: str, server: "MCPServerTask", config: dict, should_register) -> None:
    """Write-through (#56832): persist the list just published so the next startup registers this
    server lazily (no spawn). Never raises.

    The entry always mirrors the published list, INCLUDING an empty one: ``tools: []`` with the
    fingerprint and that response's own hints. An empty entry is still a lazy hit (the next startup
    registers nothing and connects nothing: servers such as the Unity bridge list zero tools while
    their host app is closed, and each live connect spawns a costly relay process), and it stops
    removed tools from being resurrected. Each profile using the connection gets its own entry,
    under its own policy, from the per-profile reconcile (_write_profile_cache).

    A failed write is logged at WARNING: until a later successful write, the next startup may
    register the previous manifest."""
    from tools.mcp_schema_cache import config_fingerprint, write_cache_entry

    try:
        fingerprint = config_fingerprint(config)
        utility_payload = [{"schema": e["schema"], "handler_key": e["handler_key"]}
                           for e in _select_utility_schemas(name, server, config)]
        hints = _list_hints(server)
    except Exception as exc:
        logger.warning("MCP schema cache for '%s' not updated (%s); the next startup may use the previous manifest",
                       name, exc)
        return
    try:
        write_cache_entry(name, fingerprint, tools=_tools_payload(server, should_register),
                          utility_tools=utility_payload, **hints)
    except Exception as exc:
        logger.warning("MCP schema cache write failed for '%s' (%s); the next startup may use the previous "
                       "manifest until a live connect rewrites it", name, exc)


def _list_hints(server: "MCPServerTask") -> dict:
    cache_meta = getattr(server, "_list_cache_meta", None) or {}
    return {"ttl_ms": cache_meta.get("ttl_ms"), "cache_scope": cache_meta.get("cache_scope")}


def _policy_allows_utility(config: dict, handler_key: str) -> bool:
    """``tools.resources`` / ``tools.prompts`` of *config* for one cached utility row (an unknown
    handler key is dropped: a cache row must not widen what the reading profile allows)."""
    family = _UTILITY_CAPABILITY_ATTRS.get(handler_key)
    if family is None:
        return False
    return _parse_boolish(((config or {}).get("tools") or {}).get(family), default=True)


def _register_server_tools(name: str, server: "MCPServerTask", config: dict) -> List[str]:
    """PUBLISH ``server._tools`` (initial discovery, revival, list_changed refresh, reconnect): the
    connection's published list changed, so every profile using it is recomputed by
    ``_reconcile_connection`` (the owner under *config*, each adopter under its own policy).
    Returns the owner's names. Lossy normalization collisions fail closed; a name another toolset
    owns is never overwritten.

    This function OWNS the PUBLICATION writes to ``server._registered_tool_names``: a published
    name list is written here, inside ``_connection_lock(key)``, never by a caller after the lock
    is released (MCP-P2-1 round 2). Callers used to assign the return value themselves, so an owner
    policy edit interleaving between the reconcile and that assignment installed the correct
    snapshot and had it overwritten with this publication's earlier names — status/tool counts then
    read stale until the next reconcile. Use the return value; do not assign the attribute.

    ONE carve-out, and it is not a publication: the TEARDOWN/PARK reset in
    ``MCPServerTask._deregister_tools`` (``mcp_tool_server_run.py``, reached from ``shutdown()``
    and from the park path) clears ``_list_published``, calls ``_reconcile_connection`` to empty
    every profile, then assigns ``[]``. That assignment is outside this lock, but it cannot lose a
    publication's write: it only ever stores the empty list that the reconcile it just ran has
    already made true, and a publication racing it re-establishes both the registry entries and
    the snapshot together under the lock. The single-writer rule constrains who may write a
    NON-EMPTY list; the deregistration reset is idempotent with its own reconcile (R3-3).
    """
    key = _server_key_for_task(server)
    owner_scope = _core._server_registry_scope(key)
    slot = (key, owner_scope)
    # Was this task THE live connection for *key* when the publication started? The adoption paths
    # pass ``require_live`` because they decide from a snapshot taken outside the lock; the owner
    # path has the same exposure (relay finding R2): a teardown landing mid-publication pops the key
    # and runs ``_forget_connection_profiles_locked``, after which writing the owner policy
    # resurrects a record for a destroyed connection. It cannot simply pass ``require_live=True``,
    # because a legitimate publication by a server that is NOT in ``_servers`` exists -- a retained
    # initial-failure connection republishing once ``_ready`` is set
    # (``mcp_tool_transport._register_discovered_tools_if_needed``). So the guard is conditional on
    # having BEEN live: only a connection that was live and then vanished is a teardown.
    #
    # KNOWN RESIDUAL (R3-5), recorded rather than closed, with its width stated. This read takes
    # ``_core._lock`` on its own and releases it before ``_connection_lock(key)`` is acquired
    # below, so the two are not one atomic observation. A connection that is NOT yet the
    # ``_servers`` entry when ``was_live`` is read, becomes it immediately after, and is then torn
    # down before ``_set_profile_policy`` runs, carries ``was_live=False`` past the guard and can
    # still resurrect a ghost owner record. Width: the interval between this lock release and the
    # policy write inside the loop below — and it requires that exact become-live-then-die ordering
    # inside it. Not merged into the publication lock on purpose: teardown orders against
    # ``_core._lock``, never against ``_connection_lock(key)`` (relay R1), so holding ``_core._lock``
    # across the whole publication would be a lock-order inversion with ``_set_profile_policy``'s
    # own acquisition rather than a fix. Closing it properly means teardown and publication sharing
    # one liveness epoch; that is a design change beyond this round's scope and is the recorded
    # follow-up. Blast radius is the MCP-P2-2 class (a ghost policy/membership record for a
    # destroyed connection, swept at the next discovery), not tool exposure or trust.
    with _core._lock:
        was_live = _core._servers.get(key) is server
    # Selecting the policy and writing it happen under the SAME per-connection lock as
    # _set_profile_policy, so a concurrent owner edit cannot land between them; the policy epoch is
    # re-validated before writing, so an edit that lands even re-entrantly restarts the selection
    # (the round-10 stale-recompute rule).
    with _connection_lock(key):
        for _ in range(_RECONCILE_MAX_PASSES):
            with _core._lock:
                recorded = _core._server_scope_configs.get(slot)
                epoch = _core._server_scope_epochs.get(slot, 0)
            # A REPUBLICATION by the same connection task (list_changed, reconnect, revival) passes its
            # connect-time config. If the owner's recorded policy is for the same route, it is the
            # current policy (a config-only edit applied by the registration pass: trust,
            # include/exclude): keep it, never revert it to the connect-time snapshot. A task's FIRST
            # publication, or a different route, takes the passed config (a record from an earlier
            # connection is not this task's policy).
            policy = config
            if (getattr(server, "_published_before", False) is True
                    and isinstance(recorded, dict) and isinstance(config, dict)
                    and _connection_identity(_without_visibility_filter(recorded))
                    == _connection_identity(_without_visibility_filter(config))):
                policy = recorded
            _record_tool_trust_metadata(name, policy, server._tools, key)
            with _core._lock:
                if _core._server_scope_epochs.get(slot, 0) != epoch:
                    continue  # the owner's policy moved while selecting: select again
            server._list_published = True
            server._published_before = True
            _set_profile_policy(key, owner_scope, policy, server=server, reconcile=False,
                                require_live=was_live)
            return _publish_owner_names(key, server, _reconcile_connection(key, server))
        logger.warning("MCP server '%s': owner policy kept changing during publication; publishing with the "
                       "latest recorded policy", name)
        with _core._lock:
            latest = _core._server_scope_configs.get(slot)
        policy = latest if isinstance(latest, dict) else config
        _record_tool_trust_metadata(name, policy, server._tools, key)
        server._list_published = True
        server._published_before = True
        _set_profile_policy(key, owner_scope, policy, server=server, reconcile=False,
                            require_live=was_live)
        return _publish_owner_names(key, server, _reconcile_connection(key, server))


def _publish_owner_names(key, server, names: List[str]) -> List[str]:
    """Settle ``server._registered_tool_names`` while the caller still holds ``_connection_lock``.

    ``_reconcile_profile`` already writes the snapshot through ``_record_owner_name_snapshot`` for a
    live owner, which is the normal path; this covers the one case that skips — a server publishing
    while it is not (yet / any longer) the entry in ``_core._servers`` for *key*, e.g. a retained
    initial-failure connection whose ``_ready`` is set (``mcp_tool_transport.
    _register_discovered_tools_if_needed``). Doing it here rather than in the caller is the point:
    inside the lock no policy edit can interleave, so this write can never clobber a newer snapshot.
    """
    with _core._lock:
        if _core._servers.get(key) is server:
            return names  # _record_owner_name_snapshot already wrote it under this same lock
    try:
        server._registered_tool_names = list(names)
    except AttributeError:  # a test double without the slot
        pass
    return names


# ---------------------------------------------------------------------------------------------
# Per-profile state: ONE pure recompute (round 10)
#
# Every profile P that uses connection K (the owner, and each adopter) has, per K:
#   live registry entries in toolset ``mcp-{name}`` in P's slot, and P's schema-cache entry.
# Both are a pure function of two inputs:
#   1. K's last PUBLISHED raw list (``server._tools`` / ``_list_cache_meta`` / capabilities,
#      while ``server._list_published``; nothing once K is torn down), and
#   2. P's current policy for K (``_server_scope_configs[(K, P)]``: tool filter,
#      ``tools.resources`` / ``tools.prompts``; absent = P does not use K).
# ``_reconcile_profile`` computes that function and applies the difference against P's OWN slot.
# Input 1 changes go through ``_register_server_tools`` / teardown -> ``_reconcile_connection``;
# input 2 changes go through ``_set_profile_policy``. Nothing else writes per-profile state.
# Both run under one lock per connection, and a policy epoch makes an in-flight recompute whose
# input moved under it (even re-entrantly) start over rather than publish a stale result.
# ---------------------------------------------------------------------------------------------

_CONNECTION_LOCKS = tuple(threading.RLock() for _ in range(32))
_RECONCILE_MAX_PASSES = 8


def _connection_lock(key) -> "threading.RLock":
    return _CONNECTION_LOCKS[hash(key) % len(_CONNECTION_LOCKS)]


def _profile_scopes(key) -> List[Optional[str]]:
    """The owner's scope (ALWAYS: its slot must equal f(list, owner policy), which is empty when it
    has none) and every scope holding a policy for *key*, owner first, then a stable order."""
    owner = _core._server_registry_scope(key)
    with _core._lock:
        scopes = {scope for (k, scope) in _core._server_scope_configs if k == key}
    scopes.add(owner)
    return sorted(scopes, key=lambda s: (s != owner, "" if s is None else str(s)))


def _sync_membership_locked(key) -> None:
    """``_server_tool_scopes[key]`` = the owner's scope while the connection is live, plus every
    non-None scope holding a policy for *key* (derived, never written elsewhere). Caller holds
    ``_core._lock``."""
    scopes = {scope for (k, scope) in _core._server_scope_configs if k == key and scope is not None}
    owner = _core._server_scope_keys.get(key) or _key_scope(key)
    if owner is not None and key in _core._servers:
        scopes.add(owner)
    if scopes:
        _core._server_tool_scopes[key] = scopes
    else:
        _core._server_tool_scopes.pop(key, None)


def _set_profile_policy(key, scope: Optional[str], config: Optional[dict], *, server=None,
                        reconcile: bool = True, require_live: bool = False) -> List[str]:
    """The ONLY writer of a profile's policy for a connection: gain (config for a new scope), change
    (a different config), or loss (``None``). Recomputes that profile unless *reconcile* is False
    (the caller reconciles the whole connection next). Returns the profile's landed names.

    ``require_live``: the caller decided from a snapshot of ``_core._servers`` taken OUTSIDE this
    lock -- the adoption pass in ``_register_connected_into_current_scope``, and the owner
    publication in ``_register_server_tools``. A shutdown landing in that window has already run
    ``_forget_connection_profiles_locked``, so writing the snapshot's
    policy now would resurrect a record for a destroyed connection: a ghost policy and membership
    that teardown will never revisit and that the orphan re-registration
    (``mcp_tool_lifecycle._reregister_orphaned_adopters``, whose pending set was built from the
    membership that existed AT teardown) cannot see, leaving the profile tool-less until its next
    discovery. Re-validated here, a dead connection is a policy LOSS instead (the state teardown
    left), so the next pass re-adopts or connects cleanly.

    WHICH lock carries that invariant: ``_core._lock``, NOT ``_connection_lock(key)``.
    ``mcp_tool_lifecycle.shutdown_mcp_servers`` never acquires a connection lock -- it pops
    ``_core._servers`` and runs ``clear_selected_status`` -> ``_forget_connection_profiles_locked``
    inside ``_core._lock``. So the liveness re-read and the policy write below sit in ONE
    ``_core._lock`` block on purpose: that is what makes them atomic against the sweep.
    ``_connection_lock(key)`` serializes publication against concurrent policy edits -- a
    different race -- and would not order anything against teardown.
    """
    name = _key_name(key)
    with _connection_lock(key):
        owner = _core._server_registry_scope(key)
        with _core._lock:
            slot = (key, scope)
            if require_live and config is not None:
                live_now = _core._servers.get(key)
                if live_now is None or getattr(live_now, "session", None) is None:
                    logger.debug("MCP server '%s': connection was destroyed while profile %s was adopting it; "
                                 "not recording a policy for the dead connection", name, scope)
                    config = None
            if config is None:
                _core._server_scope_configs.pop(slot, None)
            else:
                _core._server_scope_configs[slot] = dict(config)
            _core._server_scope_epochs[slot] = _core._server_scope_epochs.get(slot, 0) + 1
            _sync_membership_locked(key)
            if config is None:
                _core._server_trust_levels.pop(_server_key(name, scope, current=False), None)
        if config is not None:
            # ``trust`` is part of the profile's policy, the owner's included: the call-time gate reads
            # it by the profile's own key, so a policy-only ``full`` -> ``untrusted`` edit takes effect now.
            _record_scope_trust(name, config, scope)
        if not reconcile:
            return []
        landed = _reconcile_profile(key, scope, server=server)
        _restore_server_toolset_alias(key)
        return landed


def _desired_candidates(name: str, server: "MCPServerTask", config: dict) -> List[_Candidate]:
    """f(published list, policy) for one profile: its filter over the native tools plus the
    utilities its ``tools.resources`` / ``tools.prompts`` allow (and the server advertises), after
    the same collision resolution every registration uses."""
    candidates = _tool_candidates(name, server._tools, _make_tool_filter(name, config), server.tool_timeout)
    candidates += _utility_candidates(name, _select_utility_schemas(name, server, config), server.tool_timeout)
    return _resolve_name_collisions(name, candidates)


def _apply_profile(name: str, scope: Optional[str], desired: List[_Candidate]) -> tuple:
    """Make *scope*'s own slot hold exactly *desired* for toolset ``mcp-{name}``: register (or
    overwrite) each desired entry, verified in THAT slot, and deregister whatever else the slot holds
    for the toolset. A name another toolset owns (built-in, another server) is never overwritten.
    Returns ``(landed names, failed)``."""
    from tools.registry import registry

    toolset = f"mcp-{name}"
    actual = set(registry.get_tool_names_for_toolset_in_scope(toolset, scope))
    check_fn = _make_check_fn(name)
    landed: List[str] = []
    failed = False
    for c in desired:
        existing = registry.get_entry(c.registry_name, scope=scope)
        if existing is not None and existing.toolset != toolset:
            if existing.toolset.startswith("mcp-"):
                logger.error("MCP server '%s': %s normalizes to '%s', already owned by MCP toolset '%s' — skipping to "
                             "preserve the existing owner", name, c.origin, c.registry_name, existing.toolset)
            else:
                logger.warning("MCP server '%s': %s (→ '%s') collides with built-in tool in toolset '%s' — skipping to "
                               "preserve built-in", name, c.origin, c.registry_name, existing.toolset)
            continue
        try:
            registry.register(
                name=c.registry_name, toolset=toolset, schema=c.schema, handler=c.handler, check_fn=check_fn,
                is_async=False, description=c.schema.get("description") or "", scope=scope)
        except Exception as exc:
            failed = True
            logger.warning("MCP server '%s': registering '%s' failed (%s)", name, c.registry_name, exc)
            continue
        entry = registry.snapshot_registration(c.registry_name, scope=scope)
        if entry is not None and entry.toolset == toolset:
            _track_mcp_tool_server(c.registry_name, name)
            landed.append(c.registry_name)
        else:
            logger.error("MCP server '%s': registration of %s as '%s' was rejected by the registry; "
                         "skipping provenance/count updates", name, c.origin, c.registry_name)
    for stale in sorted(actual - set(landed)):
        registry.deregister(stale, scope=scope)
        if not registry.is_registered_anywhere(stale):
            _forget_mcp_tool_server(stale)
    if landed:
        registry.register_toolset_alias(name, toolset)
    return landed, failed


def _write_profile_cache(name: str, server: "MCPServerTask", config: dict, scope: Optional[str]) -> None:
    """*scope*'s schema-cache entry = f(published list, its policy), under that profile's home."""
    from pathlib import Path
    from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override

    try:
        here = hermes_home_key()
    except Exception:
        here = None
    if scope is None or scope == here:
        _write_schema_cache(name, server, config, _make_tool_filter(name, config))
        return
    token = set_hermes_home_override(Path(scope))
    try:
        _write_schema_cache(name, server, config, _make_tool_filter(name, config))
    finally:
        reset_hermes_home_override(token)


def _reconcile_profile(key, scope: Optional[str], *, server=None) -> List[str]:
    """Recompute one profile's live entries and cache for one connection from (published list,
    policy) and apply the difference. Idempotent; safe to call for any input change."""
    name = _key_name(key)
    slot = (key, scope)
    with _connection_lock(key):
        for _ in range(_RECONCILE_MAX_PASSES):
            with _core._lock:
                config = _core._server_scope_configs.get(slot)
                epoch = _core._server_scope_epochs.get(slot, 0)
                live = server if server is not None else _core._servers.get(key)
            # MCPServerTask always defines it (False until published); a bare double that lacks it
            # stands for a connection whose ``_tools`` is its published list.
            published = live is not None and bool(getattr(live, "_list_published", True))
            desired = _desired_candidates(name, live, config) if (config is not None and published) else []
            with _core._lock:
                if _core._server_scope_epochs.get(slot, 0) != epoch:
                    continue  # the policy moved while computing (possibly re-entrantly): recompute
            landed, failed = _apply_profile(name, scope, desired)
            with _core._lock:
                if _core._server_scope_epochs.get(slot, 0) != epoch:
                    continue  # moved while applying: the next pass converges on the new input
            if config is not None and published:
                if failed:
                    logger.warning("MCP server '%s': live tools for profile %s incomplete; its schema cache "
                                   "is left unchanged until a publication succeeds", name, scope)
                else:
                    _write_profile_cache(name, live, config, scope)
            _record_owner_name_snapshot(key, scope, live, landed)
            return landed
    logger.warning("MCP server '%s': profile %s policy kept changing during reconcile; left for the next pass",
                   name, scope)
    return []


def _record_owner_name_snapshot(key, scope: Optional[str], live, landed: List[str]) -> None:
    """Keep the owner's ``_registered_tool_names`` equal to what its slot actually holds.

    The snapshot is the status/tool-count source (``mcp_tool_discovery.get_mcp_status`` and the
    ``_connected_summary`` line read ``len(server._registered_tool_names)``). It used to be written
    only where ``_register_server_tools`` is CALLED (discovery, ``_publish_tool_list``), so an
    owner-policy-only edit applied by the registration pass -- which reaches the registry through
    ``_set_profile_policy`` -> ``_reconcile_profile`` and never republishes -- moved the live
    entries while the snapshot kept the pre-edit names. Writing it here, in the one place that
    computes the owner's landed set, makes the two equal for EVERY input change (publish, teardown,
    policy edit), not only for a publication.
    """
    if live is None or scope != _core._server_registry_scope(key):
        return
    with _core._lock:
        if _core._servers.get(key) is not live:
            return  # a bare double or a replaced connection: not the live owner's snapshot
    try:
        live._registered_tool_names = list(landed)
    except AttributeError:  # a test double without the slot
        pass


def _reconcile_connection(key, server=None) -> List[str]:
    """Recompute EVERY profile using *key* (owner first) after input 1 (the published list, or its
    absence at teardown) changed. Returns the OWNER's landed names."""
    owner = _core._server_registry_scope(key)
    owner_names: List[str] = []
    with _connection_lock(key):
        for scope in _profile_scopes(key):
            names = _reconcile_profile(key, scope, server=server)
            if scope == owner:
                owner_names = names
        _restore_server_toolset_alias(key)
    return owner_names


def _forget_connection_profiles_locked(key) -> None:
    """Connection destroyed (shutdown): drop every profile's policy record for *key*. Its live state
    was already emptied through ``_reconcile_connection`` at teardown. Caller holds ``_core._lock``."""
    for slot in [slot for slot in _core._server_scope_configs if slot[0] == key]:
        _core._server_scope_configs.pop(slot, None)
        _core._server_scope_epochs[slot] = _core._server_scope_epochs.get(slot, 0) + 1
    _core._server_tool_scopes.pop(key, None)


def _server_enabled(config: dict) -> bool:
    return _parse_boolish(config.get("enabled", True), default=True)


def _connection_identity(config: dict) -> tuple:
    """What makes one live connection reusable for another profile: the route fingerprint PLUS
    everything that authenticates it (``config_fingerprint`` deliberately excludes credentials so
    the schema cache survives a token rotation). Two profiles pointing at the same URL with different
    headers/env/auth/client certificates are two identities; borrowing across them would call tools
    as the other user."""
    from tools.mcp_schema_cache import config_fingerprint

    def _frozen(value):
        return json.dumps(value or {}, sort_keys=True, default=str)

    return (config_fingerprint(config), _frozen(config.get("env")), _frozen(config.get("headers")),
            _auth_type(config), _frozen(config.get("client_cert")), _frozen(config.get("client_key")))


def _without_visibility_filter(config: dict) -> dict:
    """*config* without ``tools.include`` / ``tools.exclude``: which tools a profile SEES is policy
    (the reconcile applies it), not part of which connection it is."""
    tools = config.get("tools")
    if not isinstance(tools, dict) or not ({"include", "exclude"} & set(tools)):
        return config
    return {**config, "tools": {k: v for k, v in tools.items() if k not in ("include", "exclude")}}


def _auth_type(config: dict) -> str:
    return (config.get("auth") or "").lower().strip()


def _same_server_route(server: Any, config: dict, *, cross_profile: bool = False) -> bool:
    """Whether *server* matches *config*, with OAuth connections never reusable across profiles.

    WHICH TOOLS a profile sees (``tools.include`` / ``tools.exclude``) is its POLICY, applied by
    its own reconcile against its own slot, not part of which connection it is: two profiles with
    the same endpoint and credentials but different filters are the same route and share one
    connection (one process; for stdio relays such as ``unity-*`` that is one child, not two).
    The owner path already stripped the filter before calling here; stripping it HERE makes the
    cross-profile adoption path agree, so route identity means the same thing on both.

    OAuth credentials live in the owning profile's token storage rather than the static config,
    so identical OAuth configs cannot prove that two profiles authenticate as the same account.
    """
    if (_connection_identity(_without_visibility_filter(getattr(server, "_config", {}) or {}))
            != _connection_identity(_without_visibility_filter(config))):
        return False
    # Identities match, so both sides carry the same normalised auth type.
    return not (cross_profile and _auth_type(config) == "oauth")


def register_connected_into_current_scope(servers: dict) -> int:
    """Serialize shared-scope reconciliation and registration for one discovery pass."""
    scope = _core._mcp_registry_scope()
    if scope is None:
        return 0
    with _SCOPE_REFRESH_LOCKS[hash(scope) % len(_SCOPE_REFRESH_LOCKS)]:
        return _register_connected_into_current_scope(servers)


def _register_connected_into_current_scope(servers: dict) -> int:
    """Heal the current profile's MCP overlay from already-connected shared servers.

    A shared live connection remains owned by the profile that opened it, but a profile with the
    same route must still receive its callable tool entries. The current profile's config is the
    allowlist, and route fingerprints prevent borrowing a differently-authenticated connection.
    Missing or changed config entries remove only this profile's overlay.
    """
    from tools.registry import registry

    scope = _core._mcp_registry_scope()
    if scope is None:
        return 0

    # This profile's current memberships (its own connections and those it adopted): each is a
    # policy LOSS (config gone/disabled, route changed, connection down) or a policy CHANGE (config
    # differs from the recorded one), recomputed immediately either way.
    with _core._lock:
        memberships = [(key, recorded) for (key, s), recorded in _core._server_scope_configs.items() if s == scope]
    for key, recorded in memberships:
        with _core._lock:
            server = _core._servers.get(key)
        config = servers.get(_key_name(key))
        cross_profile = _key_scope(key) != scope
        # ``_same_server_route`` strips the visibility filter on both sides, so owner and adopter
        # are judged by the same route identity.
        if (config is None or not _server_enabled(config) or server is None
                or getattr(server, "session", None) is None
                or not _same_server_route(server, config, cross_profile=cross_profile)):
            _remove_server_scope(key, scope)
        elif dict(config) != recorded:
            # Same out-of-lock snapshot as the gain below: a connection torn down in the window
            # must end as a policy loss, not as a changed policy for a dead key.
            _set_profile_policy(key, scope, config, require_live=True)

    registered_servers = 0
    for name, config in servers.items():
        if not _server_enabled(config):
            continue
        with _core._lock:
            own = _server_key(name, scope, current=False)
            if own in _core._servers or own in _core._lazy_server_configs:
                continue  # this profile has its own connection (live or lazily registered) for the name
            # Any other profile's live connection with the same route AND credentials is shareable.
            shared = [(key, live) for key, live in _core._servers.items()
                      if _key_name(key) == name and getattr(live, "session", None) is not None
                      and _same_server_route(live, config, cross_profile=True)]
        if not shared:
            continue
        key, _server = shared[0]
        with _core._lock:
            already = (key, scope) in _core._server_scope_configs
        if already:
            continue  # reconciled above
        # GAIN: the owner keeps teardown; this profile's state is recomputed under its own policy.
        # ``shared`` was read outside the connection lock, so the write re-validates that the
        # connection is still alive (else it would leave a ghost policy for a destroyed one).
        if _set_profile_policy(key, scope, config, require_live=True):
            registered_servers += 1
    return registered_servers


def _register_from_cache_sync(name: str, config: dict, entry: dict) -> List[str]:
    """Lazy startup: register from a cached manifest with no child process (first real call goes
    through ``_ensure_lazy_server_connected``). Trust metadata is recorded first so the
    call-time gate is identical for live and cached registrations.

    Lazy startup (#56832, design by Vansh5632): tools appear in the registry immediately; the first real
    call routes through ``_get_connected_server_for_call`` → ``_ensure_lazy_server_connected``.
    """
    from tools.mcp_schema_cache import config_fingerprint, tools_from_cache_entry, utility_tools_from_cache_entry
    tool_timeout = _resolve_tool_timeout(config)
    cached_tools = _cached_tools(tools_from_cache_entry(entry))
    _record_tool_trust_metadata(name, config, cached_tools)
    candidates = _tool_candidates(name, cached_tools, _make_tool_filter(name, config), tool_timeout)
    # A cache row may have been written under another profile's policy (a shared connection's
    # owner): only the families THIS config enables are registered.
    utility_rows = [row for row in utility_tools_from_cache_entry(entry)
                    if isinstance(row, dict) and _policy_allows_utility(config, str(row.get("handler_key") or ""))]
    candidates += _utility_candidates(name, utility_rows, tool_timeout)
    registered = _register_candidates(
        name, candidates, check_fn=_make_check_fn(name), scope=_core._mcp_registry_scope, lazy=True)
    if registered:
        with _core._lock:
            key = _server_key(name)
            _core._lazy_server_configs[key] = dict(config)
            _core._lazy_server_fingerprints[key] = config_fingerprint(config)
            _core._lazy_server_tool_names[key] = list(registered)
        logger.info("MCP server '%s' (lazy): registered %d tool(s) from schema cache", name, len(registered))
    return registered
