"""SEP-2549 schema-cache TTL expiry (tools/mcp_schema_cache.py)."""

import time

import pytest

from tests.driven_clock import DrivenClock
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


def test_ttl_rewrite_advances_written_at(monkeypatch):
    clock = DrivenClock("time")
    monkeypatch.setattr(sc, "time", clock)
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    first = sc.get_cached_entry("srv", "fp")["written_at"]
    clock.advance(1.0)
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


def test_legacy_zero_ttl_is_a_miss_until_rewritten():
    # A pre-fix ttl_ms=0 is ambiguous (SDK default vs an explicit wire 0), so it stays a miss;
    # the next live connect rewrites it without the SDK default and later reads hit.
    sc._save_all({"srv": {"fingerprint": "fp", "tools": [{"name": "t"}], "ttl_ms": 0,
                          "written_at": time.time() - 3600}})
    assert sc.get_cached_entry("srv", "fp") is None
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}])
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
