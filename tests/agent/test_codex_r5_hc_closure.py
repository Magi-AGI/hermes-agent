"""Adopted from the Codex closure review of 7152d57c83 (codex-closure-r4/hc); no network or live tools."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent
from tools.registry import registry
from tools.tool_search import bridge_tool_schemas


def _td(name):
    return {"type": "function", "function": {"name": name, "description": name,
            "parameters": {"type": "object", "properties": {}}}}


def _tc(name, call_id):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments="{}"))


def _resp(tool_calls=None):
    msg = SimpleNamespace(content="" if tool_calls else "done", tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls" if tool_calls else "stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@contextmanager
def _agent(full_names, visible_names, *, enabled_toolsets=None, disabled_toolsets=None):
    full = [_td(n) for n in full_names]
    deferred_count = len(set(full_names) - set(visible_names))
    assembled = [_td(n) for n in visible_names] + bridge_tool_schemas(deferred_count)

    def defs(*_args, **kwargs):
        return list(full if kwargs.get("skip_tool_search_assembly") else assembled)

    with (patch("model_tools.get_tool_definitions", side_effect=defs),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                        enabled_toolsets=enabled_toolsets, disabled_toolsets=disabled_toolsets,
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        yield agent


def _run(agent, responses):
    agent.client.chat.completions.create.side_effect = responses
    with (patch.object(agent, "_persist_session"),
          patch.object(agent, "_save_trajectory"),
          patch.object(agent, "_cleanup_task_resources")):
        return agent.run_conversation("use the tool")


def _result(messages, call_id):
    return next(m["content"] for m in messages
                if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


def test_registered_but_restricted_deferred_name_must_not_dispatch_different_visible_tool():
    """A registered desktop tool is unavailable to this restricted session."""
    from agent.tool_executor import _tool_search_scoped_names

    deferred = "read_terminal"
    other = "mcp__pytest_hc_restricted__read"
    added = []
    for name, toolset in ((deferred, "desktop_ui"), (other, "mcp-pytest_hc_restricted")):
        if registry.get_entry(name) is None:
            registry.register(name=name, toolset=toolset, schema=_td(name)["function"],
                              handler=lambda args, **kw: "{}")
            added.append(name)
    try:
        with _agent(["terminal", other], ["terminal"],
                    enabled_toolsets=["terminal", "mcp-pytest_hc_restricted"],
                    disabled_toolsets=["desktop_ui"]) as agent:
            assert deferred not in _tool_search_scoped_names(agent)
            assert other in _tool_search_scoped_names(agent)
            assert agent._repair_tool_call(deferred) == "terminal"
            with patch("model_tools.handle_function_call", return_value="{}") as dispatch:
                out = _run(agent, [_resp([_tc(deferred, "restricted")]), _resp()])
            dispatched = [c.args[0] for c in dispatch.call_args_list if c.args]
            assert "terminal" not in dispatched, dispatched
            assert "tool_call" not in _result(out["messages"], "restricted")
    finally:
        for name in added:
            registry.deregister(name)


def test_real_toolset_selection_excludes_desktop_tool_but_keeps_bridge_candidate():
    """No catalog mock: this is the production selection shape for the restricted probe."""
    import model_tools

    other = "mcp__pytest_hc_selection__read"
    toolset = "mcp-pytest_hc_selection"
    registry.register(name=other, toolset=toolset, schema=_td(other)["function"],
                      handler=lambda args, **kw: "{}")
    try:
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=["terminal", toolset], disabled_toolsets=["desktop_ui"],
            quiet_mode=True, skip_tool_search_assembly=True)
        names = {td["function"]["name"] for td in defs}
        assert "terminal" in names
        assert other in names
        assert "read_terminal" not in names
    finally:
        registry.deregister(other)


def test_exact_deferred_hyphen_name_must_not_be_repaired_to_visible_underscore_name():
    """Hyphen is valid in a function name; an exact registered name wins over spelling repair."""
    from agent.tool_executor import _tool_search_scoped_names

    deferred = "write-file"
    visible = "write_file"
    registry.register(name=deferred, toolset="pytest_hc_plugin", schema=_td(deferred)["function"],
                      handler=lambda args, **kw: "{}")
    try:
        with _agent([visible, deferred], [visible]) as agent:
            assert deferred in _tool_search_scoped_names(agent)
            assert agent._repair_tool_call(deferred) == visible
            with patch("model_tools.handle_function_call", return_value="{}") as dispatch:
                out = _run(agent, [_resp([_tc(deferred, "collision")]), _resp()])
            dispatched = [c.args[0] for c in dispatch.call_args_list if c.args]
            assert visible not in dispatched, dispatched
            assert "tool_call" in _result(out["messages"], "collision")
    finally:
        registry.deregister(deferred)


def test_registry_generation_refreshes_scope_after_add_and_remove():
    from agent.tool_executor import _tool_search_scoped_names

    a = "mcp__pytest_hc_generation__a"
    b = "mcp__pytest_hc_generation__b"
    toolset = "mcp-pytest_hc_generation"
    registry.register(name=a, toolset=toolset, schema=_td(a)["function"],
                      handler=lambda args, **kw: "{}")
    try:
        agent = SimpleNamespace(enabled_toolsets=[toolset], disabled_toolsets=None)
        assert a in _tool_search_scoped_names(agent)
        registry.register(name=b, toolset=toolset, schema=_td(b)["function"],
                          handler=lambda args, **kw: "{}")
        assert b in _tool_search_scoped_names(agent)
        registry.deregister(a)
        assert a not in _tool_search_scoped_names(agent)
        assert b in _tool_search_scoped_names(agent)
    finally:
        registry.deregister(a)
        registry.deregister(b)


def test_unknown_scope_preserves_legitimate_visible_name_repair():
    with _agent(["write_file", "read_terminal"], ["write_file"]) as agent:
        with (patch("model_tools.get_tool_definitions", side_effect=OSError("scope read failed")),
              patch("model_tools.handle_function_call", return_value="{}") as dispatch):
            _run(agent, [_resp([_tc("write file", "visible")]), _resp()])
        dispatched = [c.args[0] for c in dispatch.call_args_list if c.args]
        assert dispatched == ["write_file"]


def test_no_connector_guidance_without_manage_connections():
    name = "connectors__gmail__SEND_EMAIL"
    with _agent(["terminal", "read_terminal"], ["terminal"]) as agent:
        out = _run(agent, [_resp([_tc(name, "connector")]), _resp()])
        content = _result(out["messages"], "connector")
        assert content.startswith(f"Tool '{name}' does not exist."), content
        assert "invoke them through tool_call" not in content, content


def test_ungranted_connector_name_gets_conditional_guidance_only():
    from tools.tool_search import dispatch_tool_describe

    name = "connectors__gmail__SEND_EMAIL"
    with _agent(["manage_connections", "read_terminal"], ["manage_connections"]) as agent:
        described = json.loads(dispatch_tool_describe(
            {"names": [name]}, current_tool_defs=[_td("manage_connections")],
            connector_describe=lambda names: {"tools": {}}))
        assert name in described["not_found"]
        out = _run(agent, [_resp([_tc(name, "ungranted")]), _resp()])
        content = _result(out["messages"], "ungranted")
        assert "tool_describe first to confirm" in content, content
        assert "exists and is available" not in content, content


def test_mixed_batch_corrects_connector_and_executes_only_visible_tool():
    name = "connectors__gmail__SEND_EMAIL"
    with _agent(["manage_connections", "terminal", "read_terminal"],
                ["manage_connections", "terminal"]) as agent:
        with patch("model_tools.handle_function_call", return_value="{}") as dispatch:
            out = _run(agent, [_resp([_tc("terminal", "visible"), _tc(name, "connector")]),
                               _resp()])
        dispatched = [c.args[0] for c in dispatch.call_args_list if c.args]
        assert dispatched == ["terminal"], dispatched
        assert "invoke them through tool_call" in _result(out["messages"], "connector")


def test_malformed_connector_prefix_is_not_advertised_as_connector_tool():
    """A prefix alone is neither a connector tool nor a bridge-callable target."""
    name = "connectors__"
    with _agent(["manage_connections", "read_terminal"], ["manage_connections"]) as agent:
        out = _run(agent, [_resp([_tc(name, "malformed")]), _resp()])
        content = _result(out["messages"], "malformed")
        assert "connector tool name" not in content, content
        assert "tool_call" not in content, content


def test_three_invalid_deferred_direct_calls_end_partial_without_dispatch():
    deferred = "mcp__pytest_hc_three_strikes__read"
    registry.register(name=deferred, toolset="mcp-pytest_hc_three_strikes",
                      schema=_td(deferred)["function"], handler=lambda args, **kw: "{}")
    try:
        with _agent(["terminal", deferred], ["terminal"]) as agent:
            with patch("model_tools.handle_function_call", return_value="{}") as dispatch:
                out = _run(agent, [_resp([_tc(deferred, "one")]),
                                   _resp([_tc(deferred, "two")]),
                                   _resp([_tc(deferred, "three")])])
            assert out["partial"] is True
            assert not dispatch.called
            assert "tool_call" in _result(out["messages"], "one")
            assert "tool_call" in _result(out["messages"], "two")
            assert out["api_calls"] == 3
    finally:
        registry.deregister(deferred)
