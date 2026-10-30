"""Adopted from the Codex round-7 closure review of b959ce33b5 (codex-closure-r7/real_reconnect).
Round-7 H-A probes through the real MCP stdio reconnect lifecycle. R8 design: a successful empty
publication writes an EMPTY cache entry (``tools: []``) instead of deleting it.

The local child process reads a mode file when each connection starts.  No
session objects are assigned by the tests; ``run()`` tears down and recreates
the SDK transport in response to ``_reconnect_event``.
"""

import asyncio
import textwrap
import sys
from contextlib import contextmanager
from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import mcp_schema_cache as cache
from tools import mcp_tool_registration as registration
from tools.mcp_tool import MCPServerTask
from tools.registry import ToolRegistry


_CHILD = r'''
import anyio
import sys
from pathlib import Path

from mcp import types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

mode = Path(sys.argv[1]).read_text(encoding="utf-8").strip()

async def list_tools(_ctx, _params):
    tool = types.Tool.model_validate({
        "name": mode, "description": mode, "inputSchema": {"type": "object"},
    })
    return types.ListToolsResult.model_validate({"tools": [tool.model_dump(by_alias=True)], "ttlMs": 120000})

server = Server("r7-local", on_list_tools=list_tools) if mode != "no_tools" else Server("r7-local")

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

anyio.run(main)
'''


@contextmanager
def _profile(path):
    path.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(path)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


async def _await_reconnect(server, old_session):
    for _ in range(200):
        if server.session is not None and server.session is not old_session and server._ready.is_set():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("stdio transport did not reconnect within ten seconds")


def _fixture(tmp_path, name):
    child = tmp_path / "local_mcp.py"
    child.write_text(textwrap.dedent(_CHILD), encoding="utf-8")
    mode = tmp_path / "mode.txt"
    mode.write_text("alpha", encoding="utf-8")
    config = {"command": sys.executable, "args": [str(child), str(mode)], "lazy": True,
              "protocol": "stateless", "connect_timeout": 5}
    return MCPServerTask(name), config, mode


def test_stdio_reconnect_republishes_new_list_and_hints(tmp_path, caplog):
    async def scenario():
        server, config, mode = _fixture(tmp_path, "r7_stdio_changed")
        await server.start(config)
        try:
            assert [t.name for t in server._tools] == ["alpha"]
            server._registered_tool_names = registration._register_server_tools(server.name, server, config)
            assert server._registered_tool_names == ["mcp__r7_stdio_changed__alpha"]

            # A fresh connection with the same list should republish without a change warning.
            old_session = server.session
            server._reconnect_event.set()
            await _await_reconnect(server, old_session)
            assert [t.name for t in server._tools] == ["alpha"]
            assert server._registered_tool_names == ["mcp__r7_stdio_changed__alpha"]
            assert not [r for r in caplog.records if "tools changed dynamically" in r.message]

            old_session = server.session
            mode.write_text("beta", encoding="utf-8")
            server._reconnect_event.set()
            await _await_reconnect(server, old_session)

            assert [t.name for t in server._tools] == ["beta"]
            assert server._registered_tool_names == ["mcp__r7_stdio_changed__beta"]
            entry = cache.get_cached_entry(server.name, cache.config_fingerprint(config))
            assert [t["name"] for t in entry["tools"]] == ["beta"]
            assert entry.get("ttl_ms") == 120000
            assert len([r for r in caplog.records if "tools changed dynamically" in r.message]) == 1
        finally:
            await server.shutdown()

    with _profile(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())


def test_stdio_reconnect_without_tools_capability_removes_old_manifest(tmp_path):
    async def scenario():
        server, config, mode = _fixture(tmp_path, "r7_stdio_no_tools")
        await server.start(config)
        try:
            assert [t.name for t in server._tools] == ["alpha"]
            server._registered_tool_names = registration._register_server_tools(server.name, server, config)
            fingerprint = cache.config_fingerprint(config)
            assert server._registered_tool_names == ["mcp__r7_stdio_no_tools__alpha"]
            assert [t["name"] for t in cache.get_cached_entry(server.name, fingerprint)["tools"]] == ["alpha"]

            old_session = server.session
            mode.write_text("no_tools", encoding="utf-8")
            server._reconnect_event.set()
            await _await_reconnect(server, old_session)

            assert server.initialize_result.capabilities.tools is None
            assert server._tools == []
            from tools.registry import registry
            entry = cache.get_cached_entry(server.name, fingerprint)
            observed = (
                server._registered_tool_names,
                registry.get_tool_names_for_toolset("mcp-r7_stdio_no_tools"),
                [t["name"] for t in entry["tools"]] if entry else None,
            )
            assert observed == ([], [], []), observed  # R8: empty entry, not a deleted one
        finally:
            await server.shutdown()

    with _profile(tmp_path / "home"), patch("tools.registry.registry", ToolRegistry()), patch.object(
        registration, "_select_utility_schemas", return_value=[]
    ):
        asyncio.run(scenario())
