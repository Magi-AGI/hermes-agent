"""Adopted from the Codex closure review of 7152d57c83 (codex-closure-r4/root): H-A probes using the real cache and registration code."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_successful_live_tool_registration_rewrites_legacy_zero_entry(tmp_path):
    import tools.mcp_schema_cache as cache
    import tools.mcp_tool_registration as registration
    from tools.registry import registry

    profile = tmp_path / "profile"
    profile.mkdir()
    token = set_hermes_home_override(profile)
    name = "pytest_r4_ha"
    cfg = {"url": "https://mcp.example.invalid/mcp", "auth": "oauth", "lazy": True}
    registered = []
    try:
        cache._save_all({name: {
            "fingerprint": cache.config_fingerprint(cfg),
            "tools": [{"name": "search", "description": "d", "inputSchema": {}}],
            "utility_tools": [], "ttl_ms": 0, "written_at": time.time() - 60,
        }})
        assert cache.get_cached_entry(name, cache.config_fingerprint(cfg)) is None
        server = SimpleNamespace(
            name=name, _config=cfg, _list_cache_meta={}, tool_timeout=5,
            _tools=[SimpleNamespace(name="search", description="d",
                                    input_schema={"type": "object", "properties": {}},
                                    annotations=None)],
        )
        with patch.object(registration, "_select_utility_schemas", return_value=[]):
            registered = registration._register_server_tools(name, server, cfg)
        assert registered, "successful live registration must publish at least one tool"
        healed = cache.get_cached_entry(name, cache.config_fingerprint(cfg))
        assert healed is not None, "the first live registration should heal a legacy TTL zero"
        assert "ttl_ms" not in healed and "written_at" not in healed
    finally:
        for tool_name in registered:
            registry.deregister(tool_name)
        reset_hermes_home_override(token)


def test_first_page_cache_hint_survives_an_empty_page(tmp_path):
    from tools.mcp_tool import _paginate_full_list
    import mcp.types as types

    first = types.ListToolsResult.model_validate({
        "tools": [], "nextCursor": "page-two", "ttlMs": 30_000,
    })
    second = types.ListToolsResult.model_validate({
        "tools": [{"name": "search", "inputSchema": {"type": "object"}}],
        "ttlMs": 0,
    })
    pages = iter((first, second))

    async def list_tools(**_kwargs):
        return next(pages)

    meta = {}
    result = asyncio.run(_paginate_full_list(
        list_tools, "tools", "pytest_r4_ha", cache_meta_out=meta,
    ))
    assert len(result) == 1
    assert meta.get("ttl_ms") == 30_000, "the first response's hint governs the list"
