"""Tool-call validation for the conversation turn loop: unknown tool names (with
auto-repair and the 3-strike partial exit) and malformed JSON arguments (retry, then
recovery tool results).

Role alternation is preserved on every path: an invalid batch is answered with tool-role
error results (never a user message), and the exits close any open tool-result tail
(#48879). Nothing here imports ``agent.conversation_loop`` at module level (cycle).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from agent.message_metadata import append_message
from agent.message_sanitization import close_interrupted_tool_sequence, coalesce_tool_call_id
from agent.turn_failure_copy import site_copy, stamp_failure

logger = logging.getLogger("agent.conversation_loop")


@dataclass
class ToolValidationVerdict:
    """Outcome of ``validate_tool_calls``.

    ``action``: ``"ok"`` (dispatch the calls), ``"continue"`` (re-issue the API call —
    error results / retry state were recorded) or ``"return"`` (terminal partial
    result in ``result``). ``mixed_invalid_batch`` is True when the batch contains BOTH
    valid and unknown tool names: only the invalid calls get error results, the valid
    ones run."""

    action: str
    result: Optional[Dict[str, Any]]
    mixed_invalid_batch: bool


def _preview_name(name: str) -> str:
    return name[:80] + "..." if len(name) > 80 else name


def _append_tool_error_results(messages, tool_calls, content_for) -> None:
    """One tool-role result per call so every tool_call keeps a matching result."""
    for tc in tool_calls:
        append_message(messages, {
            "role": "tool",
            "name": tc.function.name,
            "tool_call_id": coalesce_tool_call_id(tc),
            "content": content_for(tc),
        })


def _partial_exit(agent, messages, conversation_history, api_call_count, final_response: str) -> Dict[str, Any]:
    """Terminal partial result. Prior retries or an earlier tool batch leave a tool-result
    tail; close it as interrupt aborts do so the next turn is not tool→user (#48879).
    This path never reaches finalize_turn, so persist here."""
    close_interrupted_tool_sequence(messages, final_response)
    agent._persist_session(messages, conversation_history)
    return stamp_failure({
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "partial": True,
        "error": final_response,
    }, "truncated", True)


def _deferred_reachable_names(agent: Any, valid_names, tool_calls) -> Optional[frozenset]:
    """Scoped deferred names (``tool_call``-reachable) — computed only when the bridge is
    visible and some call name is not directly valid, so the common path pays nothing.
    None = the scope could not be determined (repair must then fail safe, not proceed)."""
    try:
        from tools import tool_search as _ts
        if _ts.TOOL_CALL_NAME not in valid_names or all(tc.function.name in valid_names for tc in tool_calls):
            return frozenset()
        from agent.tool_executor import _tool_search_scope
        return _tool_search_scope(agent)
    except Exception:
        logger.debug("deferred-name scope lookup failed; scope unknown", exc_info=True)
        return None


def _name_variants(name: str) -> set:
    """The spellings ``repair_tool_call`` itself would try: XML-fragment trim, lowercase,
    ``-``/space -> ``_``, camelCase -> snake_case."""
    import re

    for sep in ('"', "'", "<", ">"):
        idx = name.find(sep)
        if idx > 0:
            name = name[:idx]
    lowered = name.lower()
    normalized = lowered.replace("-", "_").replace(" ", "_")
    camel = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().replace("-", "_").replace(" ", "_")
    return {v for v in (name, lowered, normalized, camel, camel.replace("__", "_")) if v}


def _registered_tool_names() -> Optional[frozenset]:
    """Every tool name registered in this process, in or out of this session's scope; None if
    the registry cannot be read. Used only as evidence AGAINST repair, never in a message."""
    try:
        from tools.registry import registry

        return frozenset(registry.get_all_tool_names())
    except Exception:
        logger.debug("registry name lookup failed; repair guard fails safe", exc_info=True)
        return None


def _repair_guard(name: str, valid_names, deferred: Optional[frozenset]) -> tuple:
    """``(guarded, exact)`` for one call name that is not a visible tool.

    The rule (R5): auto-repair exists to fix a typo of a VISIBLE tool, so it may only rename a
    call when nothing else the model plausibly meant is at least as close. "Something else" is
    any NON-VISIBLE name this process knows: a deferred tool the bridge can reach, AND any
    registered tool outside this session's scope (desktop tools a restricted session disabled,
    another profile's MCP tools, plugin tools), because excluding a tool from a session must not
    turn its name into permission to run a different one. Concretely, a call is guarded (never
    repaired) when
      1. it is a connector name (remote tools are never local repair targets), or
      2. it IS a known non-visible name, exactly (``write-file`` registered as a deferred plugin
         tool beside a visible ``write_file``), or
      3. one of the spellings repair itself would try IS a known non-visible name
         (``Read_Terminal`` for ``read_terminal``), or
      4. its closest known name by the same fuzzy measure repair uses is non-visible, or ties
         with the closest visible one (``read_termnal``).
    Only when the name, every normalised spelling, and the fuzzy winner all point at visible
    tools does repair run (``write file`` -> ``write_file``).

    ``exact`` is the reachable deferred tool the call denotes, when that is unambiguous: the
    call then gets the tool_call correction under that name. A guarded name that is not a
    reachable deferred tool keeps its own name and the generic error, which never mentions
    tool_call for it (no scope leak). With the reachable scope unknown (``deferred`` None) or
    the registry unreadable, guarding still works from whatever is known; with neither known,
    every non-visible name is guarded (repair can wait; a wrong tool cannot be undone)."""
    from difflib import SequenceMatcher, get_close_matches

    from tools.connectors import is_connector_name

    if is_connector_name(name):
        return True, None
    visible = set(valid_names)
    registered = _registered_tool_names()
    if registered is None and deferred is None:
        return True, None
    reachable = deferred or frozenset()
    hidden = (set(registered or ()) | set(reachable)) - visible

    def _exact_for(candidates) -> Optional[str]:
        hits = sorted(c for c in candidates if c in reachable)
        return hits[0] if len(hits) == 1 else None

    if name in hidden:  # rule 2
        return True, (name if name in reachable else None)
    variants = _name_variants(name)
    hidden_hits = variants & hidden
    if hidden_hits:  # rule 3
        return True, (_exact_for(hidden_hits) if not variants & visible else None)
    if variants & visible:
        return False, None
    lowered = name.lower()
    best_hidden = get_close_matches(lowered, list(hidden), n=1, cutoff=0.7)
    if best_hidden:  # rule 4
        best_visible = get_close_matches(lowered, list(visible), n=1, cutoff=0.7)
        if not best_visible:
            return True, None
        ratio = lambda other: SequenceMatcher(None, lowered, other).ratio()  # noqa: E731
        if ratio(best_hidden[0]) >= ratio(best_visible[0]):
            return True, None
    return False, None


def validate_tool_calls(
    agent: Any, assistant_message: Any, finish_reason: str, *, messages: List[Dict[str, Any]],
    conversation_history: Any, api_call_count: int, effective_task_id: Any,
) -> ToolValidationVerdict:
    """Validate ``assistant_message.tool_calls`` in place (ids uniquified, names
    repaired, dict/empty args normalized to JSON strings). Strikes for invalid names
    advance only when a turn has NO valid call, so a degenerate model still halts at
    3; args cut off mid-stream (routers rewrite ``length`` → ``tool_calls``) are refused
    outright rather than retried."""
    from agent.conversation_loop import _invalid_tool_name_error_content

    tool_calls = assistant_message.tool_calls
    valid_names = agent.valid_tool_names

    def _verdict(action: str, result: Optional[Dict[str, Any]] = None) -> ToolValidationVerdict:
        return ToolValidationVerdict(action=action, result=result, mixed_invalid_batch=_mixed_invalid_batch)

    # Uniquify duplicate tool-call ids BEFORE any downstream consumer: the
    # pre-API sanitizer keeps only the first call/result per id.
    agent._uniquify_tool_call_ids(tool_calls)

    # Repair mismatched tool names before validating (model hallucinations), but only into a
    # visible tool the model plausibly meant: a name that is, spells, or is closest to a
    # non-visible known tool (reachable deferred, or registered but out of this session's scope)
    # is never repaired into a different one (_repair_guard). A reachable deferred tool is left
    # invalid under its exact name so it gets the tool_call correction from
    # _invalid_tool_name_error_content; anything else guarded keeps the generic error.
    deferred_reachable = _deferred_reachable_names(agent, valid_names, tool_calls)
    for tc in tool_calls:
        if tc.function.name in valid_names:
            continue
        guarded, exact = _repair_guard(tc.function.name, valid_names, deferred_reachable)
        if guarded:
            if exact and exact != tc.function.name:
                agent._vprint(f"{agent.log_prefix}🔧 Deferred tool name normalised: '{tc.function.name}' -> "
                              f"'{exact}' (answered with the tool_call correction)", force=True, diagnostic=True)
                tc.function.name = exact
            continue
        repaired = agent._repair_tool_call(tc.function.name)
        if repaired:
            agent._vprint(f"{agent.log_prefix}🔧 Auto-repaired tool name: '{tc.function.name}' -> '{repaired}'",
                          force=True, diagnostic=True)
            tc.function.name = repaired
    invalid_tool_calls = [tc.function.name for tc in tool_calls if tc.function.name not in valid_names]
    # Mixed batch: error-result ONLY the invalid calls and run the valid
    # ones; voiding the turn discards real work. Strikes advance only when a
    # turn has NO valid call, so a degenerate model still halts at 3.
    _mixed_invalid_batch = bool(invalid_tool_calls) and any(
        tc.function.name in valid_names for tc in tool_calls
    )
    if _mixed_invalid_batch:
        agent._invalid_tool_retries = 0
        _n_valid = sum(1 for tc in tool_calls if tc.function.name in valid_names)
        agent._buffer_vprint(
            f"⚠️  Unknown tool '{_preview_name(invalid_tool_calls[0])}' in batch — erroring that call, "
            f"executing {_n_valid} valid call(s)"
        )
    elif invalid_tool_calls:
        agent._invalid_tool_retries += 1
        # Return helpful error to model — model can agent-correct next turn
        invalid_preview = _preview_name(invalid_tool_calls[0])
        agent._buffer_vprint(f"⚠️  Unknown tool '{invalid_preview}' — sending error to model for agent-correction ({agent._invalid_tool_retries}/3)")

        if agent._invalid_tool_retries >= 3:
            agent._flush_status_buffer()
            agent._vprint(f"{agent.log_prefix}❌ Max retries (3) for invalid tool calls exceeded. Stopping as partial.", force=True, diagnostic=True)
            agent._invalid_tool_retries = 0
            return _verdict("return", _partial_exit(
                agent, messages, conversation_history, api_call_count,
                f"Model generated invalid tool call: {invalid_preview}",
            ))

        append_message(messages, agent._build_assistant_message(assistant_message, finish_reason))
        # See _invalid_tool_name_error_content for the blank-name anti-priming rationale (#47967).
        _append_tool_error_results(
            messages, tool_calls,
            lambda tc: (
                _invalid_tool_name_error_content(tc.function.name, valid_names, agent)
                if tc.function.name not in valid_names
                else "Skipped: another tool call in this turn used an invalid name. Please retry this tool call."
            ),
        )
        return _verdict("continue")
    # Reset retry counter on successful tool call validation
    agent._invalid_tool_retries = 0

    # Validate tool call arguments are valid JSON; empty strings become empty
    # objects (common model quirk).
    invalid_json_args = []
    for tc in tool_calls:
        args = tc.function.arguments
        if isinstance(args, (dict, list)):
            tc.function.arguments = json.dumps(args)
            continue
        if args is not None and not isinstance(args, str):
            tc.function.arguments = args = str(args)
        if not args or not args.strip():
            tc.function.arguments = "{}"
            continue
        try:
            json.loads(args)
        except json.JSONDecodeError as e:
            # A mixed-batch invalid-name call never executes (error result later);
            # don't let its broken args trigger the whole-turn JSON retry.
            if not (_mixed_invalid_batch and tc.function.name not in valid_names):
                invalid_json_args.append((tc.function.name, str(e)))

    if invalid_json_args:
        invalid_names = {n for n, _ in invalid_json_args}
        # Routers may rewrite finish_reason "length" → "tool_calls", hiding
        # truncation; args not ending in } or ] (stripped) were cut off
        # mid-stream.
        _truncated = any(
            not (tc.function.arguments or "").rstrip().endswith(("}", "]"))
            for tc in tool_calls if tc.function.name in invalid_names
        )
        if _truncated:
            agent._vprint(
                f"{agent.log_prefix}⚠️  Truncated tool call arguments detected "
                f"(finish_reason={finish_reason!r}) — refusing to execute.",
                force=True, diagnostic=True,
            )
            agent._invalid_json_retries = 0
            agent._cleanup_task_resources(effective_task_id)
            return _verdict("return", _partial_exit(
                agent, messages, conversation_history, api_call_count, site_copy("truncated"),
            ))

        agent._invalid_json_retries += 1
        tool_name, error_msg = invalid_json_args[0]
        agent._buffer_vprint(f"⚠️  Invalid JSON in tool call arguments for '{tool_name}': {error_msg}")

        if agent._invalid_json_retries < 3:
            agent._buffer_vprint(f"🔄 Retrying API call ({agent._invalid_json_retries}/3)...")
            # Don't add anything to messages, just retry the API call
            return _verdict("continue")
        # Instead of returning partial, inject tool error results so the model can recover.
        # Using tool results (not user messages) preserves role alternation.
        agent._buffer_vprint("⚠️  Injecting recovery tool results for invalid JSON...")
        agent._invalid_json_retries = 0  # Reset for next attempt
        # Append the assistant message with its (broken) tool_calls, then one
        # error result per call.
        append_message(messages, agent._build_assistant_message(assistant_message, finish_reason))

        def _json_error_result(tc) -> str:
            if tc.function.name not in invalid_names:
                return "Skipped: other tool call in this response had invalid JSON."
            err = next(e for n, e in invalid_json_args if n == tc.function.name)
            return (
                f"Error: Invalid JSON arguments. {err}. "
                f"For tools with no required parameters, use an empty object: {{}}. "
                f"Please retry with valid JSON."
            )

        _append_tool_error_results(messages, tool_calls, _json_error_result)
        return _verdict("continue")

    # Reset retry counter on successful JSON validation
    agent._invalid_json_retries = 0
    return _verdict("ok")
