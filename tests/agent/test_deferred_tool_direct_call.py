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


# ---------------------------------------------------------------------------
# Sibling: name auto-repair must not rewrite an exact deferred name into a
# DIFFERENT visible tool. ``read_terminal`` / ``close_terminal`` are on the default
# deferral list and fuzzy-match ``terminal`` (difflib ratio >= 0.7), so a direct
# call used to be rewritten to ``terminal`` and dispatched there.
# ---------------------------------------------------------------------------

@pytest.fixture()
def deferred_gui_agent():
    from tools.registry import registry

    gui = "read_terminal"
    registry.register(name=gui, toolset="desktop_ui", schema=_td(gui)["function"],
                      handler=lambda args, **kw: json.dumps({"ok": True}))
    visible = [_td("terminal")]
    full = visible + [_td(gui)]
    assembled = visible + bridge_tool_schemas(1)

    def _defs(*_a, **kw):
        return list(full if kw.get("skip_tool_search_assembly") else assembled)

    with (
        patch("model_tools.get_tool_definitions", side_effect=_defs),
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
            yield agent, gui
        finally:
            registry.deregister(gui)


def test_exact_deferred_name_is_not_auto_repaired_into_another_tool(deferred_gui_agent):
    agent, gui = deferred_gui_agent
    assert agent._repair_tool_call(gui) == "terminal", "precondition: fuzzy repair collides"
    with patch("model_tools.handle_function_call", return_value=json.dumps({"ok": True})) as dispatch:
        result = _run(agent, [_resp([_tc(gui, {"pane_id": "p1"}, "g1")]), _resp(content="done")])
    dispatched = [c.args[0] if c.args else c.kwargs.get("function_name") for c in dispatch.call_args_list]
    assert "terminal" not in dispatched, f"deferred {gui} was rewritten and dispatched as terminal"
    content = _tool_result(result["messages"], "g1")["content"]
    assert "tool_call" in content and gui in content, content


# ---------------------------------------------------------------------------
# R4-1: separator / case / camelCase variants of a reachable deferred name.
# ``repair_tool_call`` normalises before its fuzzy step, so ``read-terminal`` etc. were not
# exact members of the deferred set and fell through to difflib -> ``terminal``.
# ---------------------------------------------------------------------------

@pytest.fixture()
def deferred_variants_agent():
    from tools.registry import registry

    deferred = ("read_terminal", "close_terminal")
    for name in deferred:
        registry.register(name=name, toolset="desktop_ui", schema=_td(name)["function"],
                          handler=lambda args, **kw: json.dumps({"ok": True}))
    visible = [_td("terminal"), _td("write_file")]
    full = visible + [_td(n) for n in deferred]
    assembled = visible + bridge_tool_schemas(len(deferred))

    def _defs(*_a, **kw):
        return list(full if kw.get("skip_tool_search_assembly") else assembled)

    with (
        patch("model_tools.get_tool_definitions", side_effect=_defs),
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
            yield agent
        finally:
            for name in deferred:
                registry.deregister(name)


@pytest.mark.parametrize("variant,exact", [
    ("read-terminal", "read_terminal"),
    ("close-terminal", "close_terminal"),
    ("Read_Terminal", "read_terminal"),
    ("readTerminal", "read_terminal"),
    ("read terminal", "read_terminal"),
    ("READ_TERMINAL", "read_terminal"),
])
def test_deferred_name_variants_get_the_correction_not_a_different_tool(deferred_variants_agent, variant, exact):
    agent = deferred_variants_agent
    with patch("model_tools.handle_function_call", return_value=json.dumps({"ok": True})) as dispatch:
        result = _run(agent, [_resp([_tc(variant, {"pane_id": "p1"}, "v1")]), _resp(content="done")])
    dispatched = [c.args[0] if c.args else c.kwargs.get("function_name") for c in dispatch.call_args_list]
    assert "terminal" not in dispatched, f"{variant!r} was rewritten and dispatched as terminal"
    content = _tool_result(result["messages"], "v1")["content"]
    assert "tool_call" in content and f"'{exact}'" in content, content


def test_legitimate_visible_repair_still_works_next_to_deferred_names(deferred_variants_agent):
    agent = deferred_variants_agent
    with patch("model_tools.handle_function_call", return_value=json.dumps({"ok": True})) as dispatch:
        _run(agent, [_resp([_tc("write file", {"path": "a"}, "w1")]), _resp(content="done")])
    dispatched = [c.args[0] if c.args else c.kwargs.get("function_name") for c in dispatch.call_args_list]
    assert dispatched == ["write_file"], dispatched


# ---------------------------------------------------------------------------
# R4-5: an individually unavailable tool is outside the scope set, so it never gets the
# "invoke via tool_call" hint (which tool_call would then refuse). Real get_tool_definitions.
# ---------------------------------------------------------------------------

def test_unavailable_mcp_tool_in_enabled_toolset_is_not_in_scope():
    from types import SimpleNamespace

    from agent.tool_executor import _tool_search_scoped_names
    from tools import mcp_tool
    from tools.registry import registry

    server = "pytest_r4_scope"
    ok, off = f"mcp__{server}__ok", f"mcp__{server}__off"
    registry.register(name=ok, toolset=f"mcp-{server}", schema=_td(ok)["function"],
                      handler=lambda args, **kw: "{}")
    registry.register(name=off, toolset=f"mcp-{server}", schema=_td(off)["function"],
                      handler=lambda args, **kw: "{}", check_fn=lambda: False)
    with mcp_tool._lock:
        for name in (ok, off):
            mcp_tool._mcp_tool_server_names[name] = server
    try:
        agent = SimpleNamespace(enabled_toolsets=[f"mcp-{server}"], disabled_toolsets=None)
        names = _tool_search_scoped_names(agent)
        assert ok in names, sorted(names)
        assert off not in names, "an unavailable tool must not be advertised as tool_call-reachable"
    finally:
        with mcp_tool._lock:
            for name in (ok, off):
                mcp_tool._mcp_tool_server_names.pop(name, None)
        for name in (ok, off):
            registry.deregister(name)


# ---------------------------------------------------------------------------
# R4-7: the scope set is computed once and reused across validation and every error result.
# ---------------------------------------------------------------------------

def test_scope_set_is_computed_once_for_several_invalid_calls(deferred_mcp_agent):
    agent, _ = deferred_mcp_agent
    seen = []
    real = _get_tool_definitions

    def counting(*a, **kw):
        if kw.get("skip_tool_search_assembly"):
            seen.append(1)
        return real(*a, **kw)

    with patch("model_tools.get_tool_definitions", side_effect=counting):
        _run(agent, [
            _resp([_tc(_TOOL, {"q": "a"}, "x1"), _tc(_OTHER, {"q": "b"}, "x2")]),
            _resp([_tc(_TOOL, {"q": "c"}, "x3")]),
            _resp(content="done"),
        ])
    assert len(seen) <= 1, f"scope recomputed {len(seen)} times"


def test_near_miss_of_a_deferred_name_is_not_repaired_into_a_visible_tool(deferred_variants_agent):
    """``read_termnal`` is closer to the deferred ``read_terminal`` than to ``terminal``: never
    dispatched as ``terminal``, and not renamed either (the correction must not assert a tool
    the model did not name)."""
    agent = deferred_variants_agent
    with patch("model_tools.handle_function_call", return_value=json.dumps({"ok": True})) as dispatch:
        result = _run(agent, [_resp([_tc("read_termnal", {}, "n1")]), _resp(content="done")])
    dispatched = [c.args[0] if c.args else c.kwargs.get("function_name") for c in dispatch.call_args_list]
    assert "terminal" not in dispatched, dispatched
    assert _tool_result(result["messages"], "n1")["content"].startswith("Tool 'read_termnal' does not exist.")
