"""Adopted from the Codex round-5 closure review of 1a17623a12 (codex-closure-r5/hc): H-C end-to-end probes."""

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
def _agent(full_names, visible_names, *, bridge=True):
    full = [_td(name) for name in full_names]
    assembled = [_td(name) for name in visible_names]
    if bridge:
        assembled += bridge_tool_schemas(len(set(full_names) - set(visible_names)))

    def defs(*_args, **kwargs):
        return list(full if kwargs.get("skip_tool_search_assembly") else assembled)

    with (patch("model_tools.get_tool_definitions", side_effect=defs),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        yield agent


def _run(agent, calls):
    agent.client.chat.completions.create.side_effect = [_resp(calls), _resp()]
    with (patch.object(agent, "_persist_session"),
          patch.object(agent, "_save_trajectory"),
          patch.object(agent, "_cleanup_task_resources"),
          patch("model_tools.handle_function_call", return_value="{}") as dispatch):
        result = agent.run_conversation("use the tool")
    return result, [c.args[0] for c in dispatch.call_args_list if c.args]


def _result(messages, call_id):
    return next(m["content"] for m in messages
                if m.get("role") == "tool" and m.get("tool_call_id") == call_id)


def test_visible_variant_cannot_skip_equally_close_hidden_name():
    """A space is just as plausible as either a hyphen or underscore."""
    registry.register(name="write-file", toolset="pytest_hc_r5_hidden",
                      schema=_td("write-file")["function"], handler=lambda args, **kw: "{}")
    try:
        with _agent(["write_file", "write-file"], ["write_file"]) as agent:
            assert agent._repair_tool_call("write file") is None  # round 6: no separator repair
            _out, dispatched = _run(agent, [_tc("write file", "ambiguous")])
            assert "write_file" not in dispatched, dispatched
    finally:
        registry.deregister("write-file")


def test_suffix_repair_cannot_rename_spelling_of_hidden_camel_name():
    """The repair routine strips '_tool' while the guard misses hidden runTool."""
    registry.register(name="runTool", toolset="pytest_hc_r5_hidden",
                      schema=_td("runTool")["function"], handler=lambda args, **kw: "{}")
    try:
        with _agent(["run", "runTool"], ["run"]) as agent:
            assert agent._repair_tool_call("run_tool") is None  # round 6: no suffix stripping
            _out, dispatched = _run(agent, [_tc("run_tool", "suffix")])
            assert "run" not in dispatched, dispatched
    finally:
        registry.deregister("runTool")


def test_registry_read_failure_does_not_authorize_repair_without_bridge():
    """An unknown registered-name set must not mean there are no competitors."""
    added = registry.get_entry("read_terminal") is None
    if added:
        registry.register(name="read_terminal", toolset="pytest_hc_r5_hidden",
                          schema=_td("read_terminal")["function"], handler=lambda args, **kw: "{}")
    try:
        with _agent(["terminal"], ["terminal"], bridge=False) as agent:
            assert "tool_call" not in agent.valid_tool_names
            assert agent._repair_tool_call("read_terminal") is None  # round 6: no fuzzy repair
            # Round 6 reads the all-scope name set; fail both enumerations.
            with patch.object(registry, "get_all_tool_names", side_effect=OSError("registry read failed")), \
                    patch.object(registry, "get_tool_names_all_scopes", side_effect=OSError("registry read failed")):
                _out, dispatched = _run(agent, [_tc("read_terminal", "registry")])
            assert "terminal" not in dispatched, dispatched
    finally:
        if added:
            registry.deregister("read_terminal")


def test_other_profile_registration_is_negative_repair_evidence():
    """A process-scoped registry contains tools from profiles other than the active one."""
    other_scope = "pytest-hc-r5-other-profile"
    registry.register(name="write-file", toolset="pytest_hc_r5_hidden",
                      schema=_td("write-file")["function"], handler=lambda args, **kw: "{}",
                      scope=other_scope)
    try:
        with patch.object(registry, "current_scope_key", return_value="pytest-hc-r5-active-profile"):
            assert "write-file" not in registry.get_all_tool_names()
            with _agent(["write_file"], ["write_file"], bridge=False) as agent:
                _out, dispatched = _run(agent, [_tc("write-file", "other-profile")])
                assert "write_file" not in dispatched, dispatched
    finally:
        registry.deregister("write-file", scope=other_scope)


def test_ordinary_visible_typo_still_repairs_without_competitor():
    """Round 6 (Lake): separator typos are no longer repaired even without a competitor; the
    call gets an error naming the callable tools. The case-only fix still applies."""
    with _agent(["write_file"], ["write_file"], bridge=False) as agent:
        out, dispatched = _run(agent, [_tc("write file", "ordinary")])
        assert dispatched == [], dispatched
        assert "write_file" in _result(out["messages"], "ordinary")
    with _agent(["write_file"], ["write_file"], bridge=False) as agent:
        _out, dispatched = _run(agent, [_tc("Write_File", "case")])
        assert dispatched == ["write_file"], dispatched


def test_mixed_batch_keeps_ambiguous_name_from_executing():
    registry.register(name="write-file", toolset="pytest_hc_r5_hidden",
                      schema=_td("write-file")["function"], handler=lambda args, **kw: "{}")
    try:
        with _agent(["terminal", "write_file", "write-file"],
                    ["terminal", "write_file"]) as agent:
            out, dispatched = _run(agent, [_tc("terminal", "valid"),
                                           _tc("write file", "ambiguous")])
            assert dispatched == ["terminal"], dispatched
            assert "does not exist" in _result(out["messages"], "ambiguous")
    finally:
        registry.deregister("write-file")
