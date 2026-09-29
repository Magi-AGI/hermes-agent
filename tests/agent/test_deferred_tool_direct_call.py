"""H-C: a deferred MCP tool called by its own name must not be reported as nonexistent.

Live session ``20260929_142521_70c345`` (profile ``default``, qwen3.5:9b), from the
``messages`` table:

1. ``tool_describe(["mcp__magi_archive__get_card"])``          -> schema returned
2. ``tool_call({"name": "mcp__magi_archive__get_card", ...})`` -> ran (row tool_name is the
   UNDERLYING name, which makes the transcript look like a direct call)
3. ``tool_call({"name": "mcp__magi_archive__search_cards"...})``-> ran
4. ``mcp__magi_archive__search_cards(...)`` called DIRECTLY      -> "Tool '...' does not exist.
   Available tools: <20 visible tools>"

Nothing was deregistered: deferred tools are never in ``valid_tool_names`` while the
tool_search bridge is active, so a direct call always hit the generic unknown-name error.
That error is false (the tool exists and was working a turn earlier) and lists only the
visible tools, so the model concluded "the Magi Archive tools aren't connected" and
gave up. These tests drive the real turn loop (validation -> error result -> next API call).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tools.tool_search import bridge_tool_schemas

_SERVER = "pytest_hc_srv"
_TOOL = f"mcp__{_SERVER}__search_cards"
_OTHER = f"mcp__{_SERVER}__get_card"


def _td(name: str) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": f"{name} tool",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}


_VISIBLE = [_td("web_search")]
_FULL = _VISIBLE + [_td(_TOOL), _td(_OTHER)]            # pre-assembly session scope
_ASSEMBLED = _VISIBLE + bridge_tool_schemas(2)            # what the model is shown


def _get_tool_definitions(*_a, **kw):
    return list(_FULL if kw.get("skip_tool_search_assembly") else _ASSEMBLED)


def _tc(name: str, arguments: dict, call_id: str | None = None):
    return SimpleNamespace(id=call_id or f"call_{uuid.uuid4().hex[:8]}", type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def _resp(tool_calls=None, content=""):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls" if tool_calls else "stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.fixture()
def deferred_mcp_agent():
    from tools import mcp_tool
    from tools.registry import registry

    calls: list[dict] = []

    def _handler(args, **_kw):
        calls.append(args)
        return json.dumps({"ok": True, "echo": args})

    for name in (_TOOL, _OTHER):
        registry.register(name=name, toolset=f"mcp-{_SERVER}", schema=_td(name)["function"], handler=_handler)
    with mcp_tool._lock:
        for name in (_TOOL, _OTHER):
            mcp_tool._mcp_tool_server_names[name] = _SERVER
    with (
        patch("model_tools.get_tool_definitions", side_effect=_get_tool_definitions),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        try:
            yield agent, calls
        finally:
            with mcp_tool._lock:
                for name in (_TOOL, _OTHER):
                    mcp_tool._mcp_tool_server_names.pop(name, None)
            for name in (_TOOL, _OTHER):
                registry.deregister(name)


def _run(agent, responses):
    agent.client.chat.completions.create.side_effect = responses
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("read the card")


def _tool_result(messages, call_id):
    return next(m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


def test_precondition_deferred_tool_is_hidden_but_bridge_reachable(deferred_mcp_agent):
    """Pins the live shape: the MCP tool is NOT a visible tool, only the bridge is."""
    agent, _ = deferred_mcp_agent
    assert _TOOL not in agent.valid_tool_names
    assert {"tool_search", "tool_describe", "tool_call"} <= agent.valid_tool_names


def test_direct_call_after_bridged_success_is_not_reported_as_nonexistent(deferred_mcp_agent):
    """The exact live sequence: bridged call succeeds, then a direct call to the same tool."""
    agent, calls = deferred_mcp_agent
    result = _run(agent, [
        _resp([_tc("tool_call", {"name": _TOOL, "arguments": {"q": "Main Page"}}, "c1")]),
        _resp([_tc(_TOOL, {"q": "Main Page"}, "c2")]),
        _resp(content="done"),
    ])
    assert result["completed"] is True
    assert calls == [{"q": "Main Page"}], "the bridged call must have run exactly once"
    content = _tool_result(result["messages"], "c2")["content"]
    assert "does not exist" not in content, content
    # The correction must name the working door and the exact tool name.
    assert "tool_call" in content and _TOOL in content, content


def test_direct_call_to_deferred_tool_in_mixed_batch_gets_bridge_guidance(deferred_mcp_agent):
    """Sibling path: a batch mixing a visible tool with a direct deferred call (turn_tool_round
    mixed-batch branch) uses the same error builder and must give the same correction."""
    agent, _ = deferred_mcp_agent
    with patch("model_tools.handle_function_call", return_value=json.dumps({"ok": True})):
        result = _run(agent, [
            _resp([_tc("web_search", {"query": "x"}, "v1"), _tc(_OTHER, {"q": "Home"}, "d1")]),
            _resp(content="done"),
        ])
    content = _tool_result(result["messages"], "d1")["content"]
    assert "does not exist" not in content, content
    assert "tool_call" in content and _OTHER in content, content


def test_genuinely_unknown_name_still_gets_catalog_error(deferred_mcp_agent):
    """Control: a name that is neither visible nor deferred keeps the upstream error."""
    agent, _ = deferred_mcp_agent
    result = _run(agent, [
        _resp([_tc(f"mcp__{_SERVER}__no_such_tool", {}, "u1")]),
        _resp(content="done"),
    ])
    content = _tool_result(result["messages"], "u1")["content"]
    assert content.startswith(f"Tool 'mcp__{_SERVER}__no_such_tool' does not exist."), content


def test_registered_but_out_of_scope_tool_is_not_advertised(deferred_mcp_agent):
    """No scope leak: a tool registered in the process but outside this session's toolset
    scope (another profile/server the session cannot reach) keeps the generic error; the
    bridge correction is only for names ``tool_call`` would actually accept."""
    from tools.registry import registry

    agent, _ = deferred_mcp_agent
    foreign = "mcp__pytest_hc_foreign__secret_tool"
    registry.register(name=foreign, toolset="mcp-pytest_hc_foreign", schema=_td(foreign)["function"],
                      handler=lambda args, **kw: "{}")
    try:
        result = _run(agent, [_resp([_tc(foreign, {}, "f1")]), _resp(content="done")])
    finally:
        registry.deregister(foreign)
    content = _tool_result(result["messages"], "f1")["content"]
    assert content.startswith(f"Tool '{foreign}' does not exist."), content
