"""SEP-2549 schema-cache TTL expiry (tools/mcp_schema_cache.py)."""

import time

import pytest

from tools import mcp_schema_cache as sc


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_cache_path", lambda: tmp_path / "cache.json")
    yield


def test_entry_without_ttl_never_expires():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}])
    assert sc.get_cached_entry("srv", "fp") is not None


def test_entry_within_ttl_served():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    entry = sc.get_cached_entry("srv", "fp")
    assert entry is not None
    assert entry["ttl_ms"] == 60_000
    assert "written_at" in entry


def test_entry_past_ttl_is_a_miss(monkeypatch):
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=1_000)
    real_time = time.time
    monkeypatch.setattr(sc.time, "time", lambda: real_time() + 2.0)
    assert sc.get_cached_entry("srv", "fp") is None


def test_ttl_rewrite_advances_written_at():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    first = sc.get_cached_entry("srv", "fp")["written_at"]
    time.sleep(0.01)
    # Identical payload would previously short-circuit; TTL'd entries must
    # rewrite so written_at advances on every live reconfirmation.
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    second = sc.get_cached_entry("srv", "fp")["written_at"]
    assert second > first


def test_cache_scope_round_trips():
    sc.write_cache_entry(
        "srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000, cache_scope="private"
    )
    assert sc.get_cached_entry("srv", "fp")["cache_scope"] == "private"


def test_legacy_sdk_default_zero_ttl_is_served():
    # Pre-fix entries stored the mcp 2.x SDK default ttl_ms=0 for servers that sent no hint;
    # they must be served (TTL-less), or lazy registration can never hit.
    sc._save_all({"srv": {"fingerprint": "fp", "tools": [{"name": "t"}], "ttl_ms": 0,
                          "written_at": time.time() - 3600}})
    assert sc.get_cached_entry("srv", "fp") is not None


def test_explicit_wire_zero_ttl_is_immediately_stale():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=0)
    assert sc.get_cached_entry("srv", "fp") is None


def test_sdk_default_hints_are_not_recorded():
    import asyncio
    import mcp.types as types
    from tools.mcp_tool import _paginate_full_list

    async def _list(**_kw):
        return types.ListToolsResult(tools=[])

    meta: dict = {}
    asyncio.run(_paginate_full_list(_list, "tools", "srv", cache_meta_out=meta))
    assert meta == {}

    async def _list_hinted(**_kw):
        return types.ListToolsResult.model_validate({"tools": [], "ttlMs": 60000})

    meta = {}
    asyncio.run(_paginate_full_list(_list_hinted, "tools", "srv", cache_meta_out=meta))
    assert meta == {"ttl_ms": 60000}
