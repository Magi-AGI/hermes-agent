import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from tools import mcp_tool
from tools.registry import registry


class _FakeTextBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeCallResult:
    def __init__(self, text: str = "ok", *, is_error: bool = False, structured=None):
        self.isError = is_error
        self.content = [_FakeTextBlock(text)]
        if structured is not None:
            self.structuredContent = structured


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        return _FakeCallResult("called", structured={"tool": name, "args": arguments or {}})


@pytest.fixture
def isolated_mcp_state():
    old_servers = dict(mcp_tool._servers)
    old_connecting = set(mcp_tool._server_connecting)
    old_errors = dict(mcp_tool._server_connect_errors)
    old_tool_server_names = dict(mcp_tool._mcp_tool_server_names)
    old_tool_original_names = dict(mcp_tool._mcp_tool_original_names)
    old_parallel_safe = set(mcp_tool._parallel_safe_servers)
    created_tools = []
    try:
        yield created_tools
    finally:
        for name in created_tools:
            registry.deregister(name)
        mcp_tool._servers.clear()
        mcp_tool._servers.update(old_servers)
        mcp_tool._server_connecting.clear()
        mcp_tool._server_connecting.update(old_connecting)
        mcp_tool._server_connect_errors.clear()
        mcp_tool._server_connect_errors.update(old_errors)
        mcp_tool._mcp_tool_server_names.clear()
        mcp_tool._mcp_tool_server_names.update(old_tool_server_names)
        mcp_tool._mcp_tool_original_names.clear()
        mcp_tool._mcp_tool_original_names.update(old_tool_original_names)
        mcp_tool._parallel_safe_servers.clear()
        mcp_tool._parallel_safe_servers.update(old_parallel_safe)


def _register_fake_mcp_tool(created_tools, server_name="magi-archive", tool_name="search_cards"):
    prefixed = f"mcp_{server_name.replace('-', '_')}_{tool_name}"
    registry.register(
        name=prefixed,
        toolset=f"mcp-{server_name}",
        schema={
            "name": prefixed,
            "description": "Search cards",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        handler=mcp_tool._make_tool_handler(server_name, tool_name, 2),
        check_fn=lambda: True,
        description="Search cards",
    )
    mcp_tool._track_mcp_tool_server(prefixed, server_name)
    registry.register_toolset_alias(server_name, f"mcp-{server_name}")
    created_tools.append(prefixed)
    return prefixed


def _fake_server(name="magi-archive"):
    return SimpleNamespace(
        name=name,
        session=_FakeSession(),
        _rpc_lock=asyncio.Lock(),
        _registered_tool_names=[],
        _tools=[],
        tool_timeout=2,
        _sampling=None,
        _pending_call_context=None,
    )


def test_broker_catalog_preserves_registered_mcp_tool_shape_and_aliases(isolated_mcp_state):
    created_tools = isolated_mcp_state
    tool_name = _register_fake_mcp_tool(created_tools)
    srv = _fake_server()
    srv._registered_tool_names = [tool_name]
    mcp_tool._servers["magi-archive"] = srv

    catalog = mcp_tool.export_mcp_broker_catalog(origin_profile="default")

    assert catalog["profile"] == "default"
    assert catalog["toolset_aliases"]["magi-archive"] == "mcp-magi-archive"
    [tool] = catalog["tools"]
    assert tool["name"] == tool_name
    assert tool["server"] == "magi-archive"
    assert tool["toolset"] == "mcp-magi-archive"
    assert tool["schema"] == registry.get_schema(tool_name)
    assert "SECRET_TOKEN" not in json.dumps(catalog)


def test_broker_status_scrubs_config_and_reports_connected_tools(isolated_mcp_state, monkeypatch):
    created_tools = isolated_mcp_state
    tool_name = _register_fake_mcp_tool(created_tools)
    srv = _fake_server()
    srv._registered_tool_names = [tool_name]
    srv._config = {"env": {"API_KEY": "shh"}, "command": "python"}
    mcp_tool._servers["magi-archive"] = srv
    monkeypatch.setattr(
        mcp_tool,
        "_load_mcp_config",
        lambda: {"magi-archive": {"command": "python", "env": {"API_KEY": "shh"}}},
    )

    status = mcp_tool.export_mcp_broker_status(origin_profile="default")

    assert status["profile"] == "default"
    assert status["servers"] == [
        {
            "name": "magi-archive",
            "transport": "stdio",
            "tools": 1,
            "connected": True,
            "disabled": False,
            "status": "connected",
        }
    ]
    assert "API_KEY" not in json.dumps(status)
    assert "shh" not in json.dumps(status)


def test_broker_call_forwards_to_registered_tool_and_reports_queue_wait(isolated_mcp_state, monkeypatch):
    created_tools = isolated_mcp_state
    tool_name = _register_fake_mcp_tool(created_tools)
    srv = _fake_server()
    srv._registered_tool_names = [tool_name]
    mcp_tool._servers["magi-archive"] = srv

    result = mcp_tool.call_mcp_broker_tool(
        tool_name,
        {"query": "butterfly"},
        origin_profile="default",
        origin_session_id="sid-1",
        call_id="call-1",
        timeout=5,
    )

    assert result["call_id"] == "call-1"
    assert result["server"] == "magi-archive"
    assert result["tool"] == tool_name
    assert result["origin"] == {"profile": "default", "session_id": "sid-1"}
    assert result["result"] == "called"
    assert result["structuredContent"] == {"tool": "search_cards", "args": {"query": "butterfly"}}
    assert result["queue_wait_ms"] >= 0
    assert srv.session.calls == [("search_cards", {"query": "butterfly"})]


def test_broker_cross_server_calls_do_not_block_each_other(isolated_mcp_state):
    created_tools = isolated_mcp_state
    first_tool = _register_fake_mcp_tool(created_tools, server_name="magi-archive", tool_name="search_cards")
    second_tool = _register_fake_mcp_tool(created_tools, server_name="hyperon-wiki", tool_name="get_card")

    entered = 0
    entered_lock = threading.Lock()
    both_entered = threading.Event()

    class CoordinatedSession:
        async def call_tool(self, name, arguments=None):
            nonlocal entered
            with entered_lock:
                entered += 1
                if entered == 2:
                    both_entered.set()
            deadline = time.monotonic() + 1.0
            while not both_entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            return _FakeCallResult(name)

    first = _fake_server("magi-archive")
    first.session = CoordinatedSession()
    first._registered_tool_names = [first_tool]
    second = _fake_server("hyperon-wiki")
    second.session = CoordinatedSession()
    second._registered_tool_names = [second_tool]
    mcp_tool._servers["magi-archive"] = first
    mcp_tool._servers["hyperon-wiki"] = second

    results = []

    def invoke(tool):
        results.append(mcp_tool.call_mcp_broker_tool(tool, {}, origin_profile="default", timeout=2))

    t1 = threading.Thread(target=invoke, args=(first_tool,))
    t2 = threading.Thread(target=invoke, args=(second_tool,))
    t1.start()
    t2.start()
    t1.join(timeout=3)
    t2.join(timeout=3)

    assert both_entered.is_set()
    assert len(results) == 2
    assert sorted(r["result"] for r in results) == ["get_card", "search_cards"]


def test_broker_call_timeout_reports_structured_queue_timeout(isolated_mcp_state):
    created_tools = isolated_mcp_state
    tool_name = _register_fake_mcp_tool(created_tools)
    srv = _fake_server()
    srv._registered_tool_names = [tool_name]
    mcp_tool._servers["magi-archive"] = srv

    acquired = threading.Event()
    release = threading.Event()

    async def _hold_lock():
        async with srv._rpc_lock:
            acquired.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

    holder = threading.Thread(target=lambda: asyncio.run(_hold_lock()), daemon=True)
    holder.start()
    assert acquired.wait(2)
    try:
        result = mcp_tool.call_mcp_broker_tool(tool_name, {}, origin_profile="default", timeout=0.05)
    finally:
        release.set()
        holder.join(timeout=2)

    assert result["error"]["code"] == "mcp_broker_queue_timeout"
    assert result["error"]["server"] == "magi-archive"
    assert result["queue_wait_ms"] >= 0
