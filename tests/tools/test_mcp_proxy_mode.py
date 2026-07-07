import json

import pytest

from tools import mcp_tool
from tools.registry import registry


@pytest.fixture
def proxy_registry_cleanup():
    created = []
    old_proxy_names = set(getattr(mcp_tool, "_mcp_proxy_tool_names", set()))
    try:
        yield created
    finally:
        for name in created:
            registry.deregister(name)
            try:
                mcp_tool._forget_mcp_tool_server(name)
            except Exception:
                pass
        if hasattr(mcp_tool, "_mcp_proxy_tool_names"):
            mcp_tool._mcp_proxy_tool_names.clear()
            mcp_tool._mcp_proxy_tool_names.update(old_proxy_names)


def _catalog(*, server="magi-archive", tool="search_cards"):
    tool_name = f"mcp_{server.replace('-', '_')}_{tool}"
    return {
        "version": 1,
        "profile": "claudetriad",
        "tools": [
            {
                "name": tool_name,
                "server": server,
                "toolset": f"mcp-{server}",
                "schema": {
                    "name": tool_name,
                    "description": "Search cards",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
                "description": "Search cards",
                "is_async": False,
                "max_result_size_chars": 12345,
            }
        ],
        "toolset_aliases": {server: f"mcp-{server}"},
    }


def test_register_mcp_proxy_tools_preserves_catalog_schema_and_aliases(proxy_registry_cleanup):
    catalog = _catalog()
    created = mcp_tool.register_mcp_proxy_tools(catalog, broker_call=lambda *_args, **_kw: {"result": "ok"})
    proxy_registry_cleanup.extend(created)

    assert created == ["mcp_magi_archive_search_cards"]
    entry = registry.get_entry("mcp_magi_archive_search_cards")
    assert entry is not None
    assert entry.toolset == "mcp-magi-archive"
    assert entry.schema == catalog["tools"][0]["schema"]
    assert entry.max_result_size_chars == 12345
    assert registry.get_toolset_alias_target("magi-archive") == "mcp-magi-archive"


def test_mcp_proxy_handler_forwards_to_broker_and_returns_normal_tool_shape(proxy_registry_cleanup):
    calls = []

    def fake_broker(tool_name, arguments, **kwargs):
        calls.append((tool_name, arguments, kwargs))
        return {
            "call_id": kwargs.get("call_id"),
            "server": "magi-archive",
            "origin": {"profile": "claudetriad", "session_id": "sid-1"},
            "queue_wait_ms": 7,
            "result": "called",
            "structuredContent": {"ok": True},
        }

    created = mcp_tool.register_mcp_proxy_tools(_catalog(), broker_call=fake_broker)
    proxy_registry_cleanup.extend(created)

    payload = json.loads(
        registry.dispatch(
            "mcp_magi_archive_search_cards",
            {"query": "butterfly"},
            task_id="tool-call-1",
        )
    )

    assert payload == {"result": "called", "structuredContent": {"ok": True}}
    assert calls == [
        (
            "mcp_magi_archive_search_cards",
            {"query": "butterfly"},
            {"call_id": "tool-call-1", "timeout": None},
        )
    ]


def test_proxy_mode_discover_uses_catalog_and_does_not_spawn_native_servers(monkeypatch, proxy_registry_cleanup):
    catalog = _catalog(server="hyperon-wiki", tool="get_card")
    monkeypatch.setenv("HERMES_MCP_MODE", "proxy")
    monkeypatch.setenv("HERMES_MCP_PROXY_CATALOG_JSON", json.dumps(catalog))
    monkeypatch.setattr(mcp_tool, "register_mcp_servers", lambda *_a, **_kw: pytest.fail("native MCP spawn should be disabled in proxy mode"))

    created = mcp_tool.discover_mcp_tools()
    proxy_registry_cleanup.extend(created)

    assert created == ["mcp_hyperon_wiki_get_card"]
    assert registry.get_entry("mcp_hyperon_wiki_get_card") is not None


def test_proxy_mode_required_server_gating_fails_closed(monkeypatch, proxy_registry_cleanup):
    monkeypatch.setenv("HERMES_MCP_MODE", "proxy")
    monkeypatch.setenv("HERMES_MCP_PROXY_REQUIRED_SERVERS", "magi-archive,hyperon-wiki")

    with pytest.raises(RuntimeError, match="missing required MCP broker servers: hyperon-wiki"):
        mcp_tool.register_mcp_proxy_tools(_catalog(server="magi-archive"), broker_call=lambda *_a, **_kw: {"result": "ok"})
