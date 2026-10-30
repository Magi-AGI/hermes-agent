"""Adopted from Codex preliminary review of 05be569a75 (review-artifacts/mcp-hbhc/test_codex_prelim_hc.py). No network or live MCP server."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tools.tool_search import bridge_tool_schemas


def _td(name):
    return {"type": "function", "function": {"name": name,
            "description": name, "parameters": {"type": "object", "properties": {}}}}


def _tc(name, args, call_id):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _resp(tool_calls=None, content=""):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls" if tool_calls else "stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _run(agent, responses):
    agent.client.chat.completions.create.side_effect = responses
    with (patch.object(agent, "_persist_session"),
          patch.object(agent, "_save_trajectory"),
          patch.object(agent, "_cleanup_task_resources")):
        return agent.run_conversation("use the tool")


def _result(messages, call_id):
    return next(m["content"] for m in messages
                if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


def _agent_for(full, assembled):
    def defs(*_args, **kwargs):
        return list(full if kwargs.get("skip_tool_search_assembly") else assembled)

    return (patch("model_tools.get_tool_definitions", side_effect=defs),
            patch("model_tools.check_toolset_requirements", return_value={}),
            patch("agent.process_bootstrap.OpenAI"))


def _build_agent():
    agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def test_case_variant_of_deferred_name_is_not_dispatched_as_terminal():
    from tools.registry import registry

    name = "read_terminal"
    registry.register(name=name, toolset="desktop_ui", schema=_td(name)["function"],
                      handler=lambda args, **kw: "{}")
    full = [_td("terminal"), _td(name)]
    assembled = [_td("terminal"), *bridge_tool_schemas(1)]
    try:
        p1, p2, p3 = _agent_for(full, assembled)
        with p1, p2, p3:
            agent = _build_agent()
            # Round 6: repair is case-only into a VISIBLE name; read_terminal is not visible.
            assert agent._repair_tool_call("Read_Terminal") is None
            with patch("model_tools.handle_function_call", return_value="{}") as dispatch:
                out = _run(agent, [_resp([_tc("Read_Terminal", {"pane_id": "p1"}, "x1")]),
                                   _resp(content="done")])
            called = [c.args[0] for c in dispatch.call_args_list if c.args]
            assert "terminal" not in called, called
            assert "tool_call" in _result(out["messages"], "x1")
    finally:
        registry.deregister(name)


def test_scope_lookup_failure_cannot_repair_exact_deferred_name_into_terminal():
    from tools.registry import registry

    name = "read_terminal"
    registry.register(name=name, toolset="desktop_ui", schema=_td(name)["function"],
                      handler=lambda args, **kw: "{}")
    full = [_td("terminal"), _td(name)]
    assembled = [_td("terminal"), *bridge_tool_schemas(1)]
    try:
        p1, p2, p3 = _agent_for(full, assembled)
        with p1, p2, p3:
            agent = _build_agent()
            with (patch("model_tools.get_tool_definitions", side_effect=OSError("transient scope read")),
                  patch("model_tools.handle_function_call", return_value="{}") as dispatch):
                out = _run(agent, [_resp([_tc(name, {"pane_id": "p1"}, "x2")]),
                                   _resp(content="done")])
            called = [c.args[0] for c in dispatch.call_args_list if c.args]
            assert "terminal" not in called, called
            assert "tool_call" in _result(out["messages"], "x2")
    finally:
        registry.deregister(name)


def test_reachable_remote_connector_direct_call_gets_bridge_guidance():
    import model_tools
    from tools import tool_search

    name = "connectors__gmail__SEND_EMAIL"
    full = [_td("manage_connections")]
    assembled = [*full, *bridge_tool_schemas(0, connections_granted=True)]
    p1, p2, p3 = _agent_for(full, assembled)
    with p1, p2, p3:
        agent = _build_agent()
        described = json.loads(tool_search.dispatch_tool_describe(
            {"names": [name]}, current_tool_defs=full,
            connector_describe=lambda names: {"tools": {name: {
                "description": "Send mail", "parameters": {"type": "object"}}}}))
        assert name in described["tools"], "precondition: fake granted connector is describable"
        bridge = model_tools._dispatch_bridge_tool(
            "tool_call", {"calls": [{"name": name, "arguments": {}}]}, None, None)
        assert bridge[1][0] == tool_search.CONNECTOR_BATCH_SENTINEL
        out = _run(agent, [_resp([_tc(name, {}, "c1")]), _resp(content="done")])
        content = _result(out["messages"], "c1")
        assert "does not exist" not in content, content
        assert "tool_call" in content, content


def test_scope_cache_tracks_explicit_availability_reprobe_without_registry_mutation():
    """A /reload-mcp-style re-probe can change raw tool definitions without a registry edit."""
    import model_tools
    from agent.tool_executor import _tool_search_scoped_names
    from tools.registry import invalidate_check_fn_cache, registry
    from tools.tool_search import scoped_deferrable_names

    name = "mcp__pytest_hc_cache__read"
    toolset = "mcp-pytest_hc_cache"
    available = {"now": True}
    registry.register(name=name, toolset=toolset, schema=_td(name)["function"],
                      handler=lambda args, **kw: "{}",
                      check_fn=lambda: available["now"])
    try:
        agent = SimpleNamespace(enabled_toolsets=[toolset], disabled_toolsets=None)
        assert name in _tool_search_scoped_names(agent)
        old_generation = registry._generation
        available["now"] = False
        invalidate_check_fn_cache()
        model_tools._clear_tool_defs_cache()
        fresh = scoped_deferrable_names(model_tools.get_tool_definitions(
            enabled_toolsets=[toolset], quiet_mode=True, skip_tool_search_assembly=True))
        assert name not in fresh, "precondition: re-probe must change raw definitions"
        assert registry._generation == old_generation
        from agent.tool_executor import _unwrap_tool_search_call
        _, _, scope_block = _unwrap_tool_search_call(
            agent, "tool_call", {"calls": [{"name": name, "arguments": {}}]})
        assert scope_block is not None, (
            "the stale correction cache also admitted a deferred call no longer in the raw session scope")
        assert name not in _tool_search_scoped_names(agent), (
            "direct-call correction still advertises the now-inaccessible tool")
    finally:
        registry.deregister(name)
        invalidate_check_fn_cache()
        model_tools._clear_tool_defs_cache()


def test_stale_scope_cache_executes_tool_after_check_fn_revocation():
    import model_tools
    from agent.tool_executor import _tool_search_scoped_names
    from tools.registry import invalidate_check_fn_cache, registry
    from tools.tool_search import scoped_deferrable_names

    name = "mcp__pytest_hc_revoked__read"
    toolset = "mcp-pytest_hc_revoked"
    available = {"now": True}
    calls = []
    registry.register(name=name, toolset=toolset, schema=_td(name)["function"],
                      handler=lambda args, **kw: calls.append(args) or "{}",
                      check_fn=lambda: available["now"])
    try:
        with patch("agent.process_bootstrap.OpenAI"):
            agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                           enabled_toolsets=[toolset], quiet_mode=True, skip_context_files=True,
                           skip_memory=True)
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        assert "tool_call" in agent.valid_tool_names
        assert name in _tool_search_scoped_names(agent)
        available["now"] = False
        invalidate_check_fn_cache()
        model_tools._clear_tool_defs_cache()
        fresh = scoped_deferrable_names(model_tools.get_tool_definitions(
            enabled_toolsets=[toolset], quiet_mode=True, skip_tool_search_assembly=True))
        assert name not in fresh
        out = _run(agent, [_resp([_tc("tool_call", {"calls": [{"name": name, "arguments": {}}]}, "b1")]),
                           _resp(content="done")])
        assert out["completed"] is True
        assert not calls, "handler ran after check_fn turned false and fresh catalog excluded it"
    finally:
        registry.deregister(name)
        invalidate_check_fn_cache()
        model_tools._clear_tool_defs_cache()
