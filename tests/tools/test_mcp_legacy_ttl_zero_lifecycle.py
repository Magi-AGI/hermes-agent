"""H-A pinning test: an ENABLED lazy OAuth server with a legacy ``ttl_ms=0`` schema-cache
entry whose startup connect keeps failing (expired token, interactive OAuth suppressed).

Design under test (42c5344382 + 82471fc46f, Codex-reviewed; NOT changed here): a legacy 0 is
indistinguishable from an explicit wire 0 ("immediately stale"), so it stays a cache miss until
a successful live connect rewrites the entry without the SDK default.

Pinned behaviour, per startup (each startup = a fresh process, so module state is reset):
1. the real ``get_cached_entry`` misses on the legacy entry, so the server is NOT registered
   from cache (no phantom tools that would fail when called);
2. exactly one live connect attempt runs, inside a bounded discovery pass, with interactive
   OAuth suppressed (no browser);
3. a failed connect leaves the legacy entry untouched — it never heals on failure;
4. the first successful connect rewrites it TTL-less; the next startup is a lazy cache hit
   with no connect at all.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tools.mcp_tool as mcp
from tools import mcp_schema_cache as sc
from tools import mcp_tool_discovery as disc
from tools import mcp_tool_registration as reg

_NAME = "fake-oauth-srv"
_CFG = {"url": "https://mcp.example.invalid/mcp", "auth": "oauth", "lazy": True, "connect_timeout": 15}

# Per-process connect bookkeeping (a restart starts with all of it empty).
_STATE = ("_servers", "_server_connecting", "_server_connect_failures", "_server_connect_errors",
          "_server_connect_retry_after", "_server_scope_keys",
          "_lazy_server_configs", "_lazy_server_fingerprints", "_lazy_server_tool_names")


@pytest.fixture(autouse=True)
def _restore_mcp_state():
    saved = {name: getattr(mcp, name).copy() for name in _STATE if hasattr(mcp, name)}
    yield
    _new_process()
    for name, value in saved.items():
        getattr(mcp, name).update(value)


def _new_process() -> None:
    for name in _STATE:
        if hasattr(mcp, name):
            getattr(mcp, name).clear()


def _cache_entry() -> dict | None:
    data = json.loads(Path(sc._cache_path()).read_text(encoding="utf-8"))
    return data.get(_NAME)


def _startup(connect):
    """One desktop/CLI startup: the real suppressed-discovery wrapper -> discover_mcp_tools ->
    register_mcp_servers (real lazy-from-cache pass, real get_cached_entry). Only the network
    connect and the MCP loop thread are faked."""
    from hermes_cli import mcp_startup

    budgets: list[float] = []

    def _run_inline(factory, timeout=30):
        budgets.append(timeout)
        return asyncio.run(factory() if callable(factory) else factory)

    with (
        patch("tools.mcp_tool_config._load_mcp_config", return_value={_NAME: dict(_CFG)}),
        patch.object(mcp, "_ensure_mcp_sdk", return_value=True),
        patch("tools.mcp_tool_loop._ensure_mcp_loop"),
        patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=_run_inline),
        patch("tools.mcp_tool_discovery._discover_and_register_server", side_effect=connect),
    ):
        mcp_startup._discover_mcp_tools_without_interactive_oauth()
    return budgets


def _seed_legacy_entry() -> None:
    sc._save_all({_NAME: {"fingerprint": sc.config_fingerprint(_CFG),
                          "tools": [{"name": "search", "description": "d", "inputSchema": {}}],
                          "utility_tools": [], "ttl_ms": 0, "written_at": time.time() - 86_400}})


def test_legacy_zero_entry_failing_connect_never_heals_and_never_prompts():
    from tools.mcp_oauth import OAuthNonInteractiveError, _is_interactive

    _seed_legacy_entry()
    legacy = _cache_entry()
    assert sc.get_cached_entry(_NAME, sc.config_fingerprint(_CFG)) is None  # real read gate: miss

    seen_interactive: list[bool] = []

    async def _failing_connect(name, cfg):
        seen_interactive.append(_is_interactive())
        raise OAuthNonInteractiveError(
            f"MCP OAuth for '{name}': non-interactive environment and no cached tokens found. "
            f"Run `hermes mcp login {name}` interactively first to complete initial authorization.")

    for _ in range(3):  # three restarts
        _new_process()
        budgets = _startup(_failing_connect)
        assert budgets and all(b <= mcp._MCP_DISCOVERY_PASS_MAX_SEC for b in budgets), budgets
        with mcp._lock:
            assert not any(_NAME in str(k) for k in mcp._lazy_server_configs), "registered from a miss"
        assert _cache_entry() == legacy, "a failed connect must not touch the legacy entry"

    assert seen_interactive == [False, False, False], "one suppressed live attempt per startup"


def test_first_successful_connect_heals_entry_and_next_startup_is_lazy():
    _seed_legacy_entry()
    fake_server = SimpleNamespace(
        _tools=[SimpleNamespace(name="search", description="d", input_schema={"type": "object"},
                                annotations=None)],
        _list_cache_meta={},  # mcp 2.x SDK defaults are no longer recorded (42c5344382)
        _config=dict(_CFG),
        initialize_result=None, session=SimpleNamespace(),  # a live connect always has a session
    )

    async def _succeeding_connect(name, cfg):
        reg._write_schema_cache(name, fake_server, cfg, lambda _n: True)
        return []

    _new_process()
    _startup(_succeeding_connect)
    healed = _cache_entry()
    assert "ttl_ms" not in healed and "written_at" not in healed, healed
    assert sc.get_cached_entry(_NAME, sc.config_fingerprint(_CFG)) is not None

    async def _must_not_connect(name, cfg):
        raise AssertionError("healed lazy entry must register from cache without connecting")

    _new_process()
    with patch("tools.mcp_tool_registration._register_from_cache_sync", return_value=["mcp__x__search"]) as lazy:
        _startup(_must_not_connect)
    lazy.assert_called_once()


# --- pagination: which page's SEP-2549 hints are recorded -----------------------------------

def _paged(*pages):
    import mcp.types as types

    results = [types.ListToolsResult.model_validate(p) for p in pages]
    calls = iter(results)

    async def _list(**_kw):
        return next(calls)
    return _list


def _tool(name):
    return {"name": name, "inputSchema": {"type": "object"}}


def test_later_page_sdk_defaults_or_hints_are_never_recorded():
    """Hints come from the first page only; a later page can neither inject the SDK default 0
    nor an explicit value."""
    from tools.mcp_tool import _paginate_full_list

    meta: dict = {}
    asyncio.run(_paginate_full_list(_paged({"tools": [_tool("a")], "nextCursor": "c1"},
                                           {"tools": [_tool("b")], "ttlMs": 0}),
                                    "tools", "srv", cache_meta_out=meta))
    assert meta == {}


def test_first_page_hint_survives_later_pages():
    from tools.mcp_tool import _paginate_full_list

    meta: dict = {}
    items = asyncio.run(_paginate_full_list(_paged({"tools": [_tool("a")], "nextCursor": "c1", "ttlMs": 60000},
                                                   {"tools": [_tool("b")]}),
                                            "tools", "srv", cache_meta_out=meta))
    assert meta == {"ttl_ms": 60000} and len(items) == 2


def test_warm_rediscovery_honours_backoff_and_reload_mcp_retries_once():
    """R4 AG-9: in ONE process (no cold restart). A failed connect stamps the connect backoff;
    a warm re-discovery (config watcher, a second register pass) must honour it: no new attempt,
    no registration from the legacy miss, entry untouched. ``/reload-mcp`` (shutdown) clears the
    backoff, and the next pass makes exactly one more suppressed attempt."""
    from tools.mcp_oauth import OAuthNonInteractiveError, _is_interactive
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    _seed_legacy_entry()
    legacy = _cache_entry()
    attempts: list[bool] = []

    async def _failing_connect(name, cfg):
        attempts.append(_is_interactive())
        raise OAuthNonInteractiveError(f"MCP OAuth for '{name}': no cached tokens")

    _new_process()
    _startup(_failing_connect)
    assert attempts == [False]
    with mcp._lock:
        assert any(_NAME in str(k) for k in mcp._server_connect_retry_after), "no backoff stamped"

    _startup(_failing_connect)  # warm: same process, backoff still active
    assert attempts == [False], "a warm re-discovery ignored the connect backoff"
    with mcp._lock:
        assert not any(_NAME in str(k) for k in mcp._lazy_server_configs), "registered from a miss"
    assert _cache_entry() == legacy

    with patch("tools.mcp_tool_loop._stop_mcp_loop"):
        shutdown_mcp_servers()  # what /reload-mcp does first
    with mcp._lock:
        assert not any(_NAME in str(k) for k in mcp._server_connect_retry_after)
    _startup(_failing_connect)
    assert attempts == [False, False], "reload must retry exactly once, suppressed"
    assert _cache_entry() == legacy
