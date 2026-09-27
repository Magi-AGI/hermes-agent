"""Unit coverage for the background-review aux-model selector + routed digest.

Covers the two behaviors this change adds:
  • _resolve_review_runtime — auto/same-model → not routed (main model, warm
    cache); a configured different model → routed with resolved credentials.
  • _digest_history — compact replay used ONLY on the routed path (recent tail
    verbatim + a digest of older turns), preserving role alternation.

Pure-function / config-driven; no live model calls.
"""
from typing import Any
from unittest.mock import patch

from agent import background_review as br


def _msg(role, content, tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


# ---------------------------------------------------------------------------
# _resolve_review_runtime — the aux-model selector
# ---------------------------------------------------------------------------

class _FakeAgent:
    def __init__(self, provider="openai-codex", model="gpt-5.5"):
        self.provider = provider
        self.model = model
        self._credential_pool: Any = None
        self.request_overrides = {}
        self.max_tokens: int | None = None

    def _current_main_runtime(self):
        return {
            "api_key": "parent-key",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "codex_app_server",
        }


def test_routing_auto_inherits_parent_and_downgrades_codex_app_server():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {"provider": "auto", "model": ""}}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"
    assert rt["model"] == "gpt-5.5"
    assert rt["api_mode"] == "codex_responses"  # downgraded so agent-loop tools dispatch


def test_routing_to_different_model_marks_routed_and_resolves_credentials():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    fake_rp = {
        "provider": "openrouter", "api_key": "or-key",
        "base_url": "https://openrouter.ai/api/v1", "api_mode": "chat_completions",
        "credential_pool": "routed-pool",
        "request_overrides": {"extra_body": {"store": False}},
        "max_output_tokens": 2048,
    }
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=fake_rp):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is True
    assert rt["provider"] == "openrouter"
    assert rt["model"] == "google/gemini-3-flash-preview"
    assert rt["api_key"] == "or-key"
    assert rt["credential_pool"] == "routed-pool"
    assert rt["request_overrides"] == {"extra_body": {"store": False}}
    assert rt.get("max_tokens") is None


def test_unrouted_runtime_keeps_parent_pool_and_overrides():
    agent = _FakeAgent()
    agent._credential_pool = "parent-pool"
    agent.request_overrides = {"service_tier": "priority"}
    agent.max_tokens = 4096
    with patch("hermes_cli.config.load_config", return_value={}), patch("hermes_cli.config.load_config_readonly", return_value={}):
        rt = br._resolve_review_runtime(agent)
    assert rt["credential_pool"] == "parent-pool"
    assert rt["request_overrides"] == {"service_tier": "priority"}
    assert rt["max_tokens"] == 4096


def test_routing_same_model_as_parent_is_not_routed():
    agent = _FakeAgent(provider="openrouter", model="anthropic/claude-opus-4.8")
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "anthropic/claude-opus-4.8",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False  # same model/provider → keep full-replay path


def test_routing_resolution_failure_falls_back_to_parent():
    agent = _FakeAgent()
    cfg = {"auxiliary": {"background_review": {
        "provider": "openrouter", "model": "google/gemini-3-flash-preview",
    }}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=RuntimeError("boom")):
        rt = br._resolve_review_runtime(agent)
    assert rt["routed"] is False
    assert rt["provider"] == "openai-codex"


# ---------------------------------------------------------------------------
# _digest_history — routed-path compact replay
# ---------------------------------------------------------------------------

def test_digest_under_tail_returns_full():
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    assert br._digest_history(msgs, tail=24) == msgs


def test_digest_collapses_old_keeps_tail_verbatim():
    msgs = []
    for i in range(60):
        msgs.append(_msg("user", f"u{i} " + "x" * 50))
        msgs.append(_msg("assistant", f"a{i} " + "y" * 50))
    out = br._digest_history(msgs, tail=10)
    # First message is the synthetic digest (user role → alternation preserved).
    assert out[0]["role"] == "user"
    assert out[0]["content"].startswith("[Earlier conversation digest")
    # Recent tail preserved verbatim.
    assert out[-1] == msgs[-1]
    assert len(out) == 11  # 1 digest + 10 tail


def test_digest_does_not_open_tail_on_a_tool_message():
    msgs = []
    for i in range(40):
        msgs.append(_msg("user", "u" + "x" * 50))
        msgs.append(_msg("assistant", "", tool_calls=[
            {"function": {"name": "terminal", "arguments": "{}"}}]))
        msgs.append({"role": "tool", "content": "result " + "w" * 50})
    out = br._digest_history(msgs, tail=2)
    # The verbatim tail (after the digest) must not begin on a bare tool message.
    assert out[1]["role"] != "tool"


def test_digest_records_tool_names_in_arc():
    old = [
        _msg("user", "do the thing"),
        _msg("assistant", "", tool_calls=[
            {"function": {"name": "skill_view", "arguments": "{}"}},
            {"function": {"name": "patch", "arguments": "{}"}}]),
    ]
    msgs = old + [_msg("user", f"tail{i}") for i in range(30)]
    out = br._digest_history(msgs, tail=10)
    digest = out[0]["content"]
    assert "USER: do the thing" in digest
    assert "tools: skill_view, patch" in digest


# ---------------------------------------------------------------------------
# Cost / configurability controls (issue #87250)
# ---------------------------------------------------------------------------

def test_enabled_defaults_true():
    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        assert br.load_background_review_settings()[0] is True


def test_enabled_false_disables_automatic_review():
    cfg = {"auxiliary": {"background_review": {"enabled": False}}}
    with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        assert br.load_background_review_settings()[0] is False


def test_unresolvable_review_provider_falls_back_with_visible_warning(caplog):
    """The fork silently ran on the main model with only a debug line (#116055): the fallback must
    name the configured provider and reason at WARNING and reach the agent's user-visible warning rail."""
    import logging

    agent = _FakeAgent()
    emitted = []
    agent._emit_warning = emitted.append
    cfg = {"auxiliary": {"background_review": {"provider": "no-such-provider", "model": "review-model"}}}
    with patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        with caplog.at_level(logging.WARNING, logger="agent.background_review"):
            rt = br._resolve_review_runtime(agent)
            br._resolve_review_runtime(agent)

    assert rt["routed"] is False and rt["model"] == "gpt-5.5"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and all("no-such-provider" in w and "review-model" in w for w in warnings)
    assert len(emitted) == 1 and "no-such-provider" in emitted[0]  # once per agent on the user rail


def test_digest_labels_runtime_inserted_user_rows_as_not_the_user():
    """E5 (T6): a compaction summary or task-list restore sits in the user role but is runtime text.
    The routed digest must not present it as `USER:` (the learner treats USER lines as the user's own
    corrections); a row the user typed is still `USER:`."""
    msgs = [
        _msg("user", "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns... exact approval phrase required"),
        _msg("assistant", "ok"),
        _msg("user", "[Your active task list was preserved across context compression]\n- [ ] a"),
        _msg("assistant", "ok"),
        _msg("user", "please stop asking me to confirm every step"),
        _msg("assistant", "understood"),
    ] + [_msg("user", f"u{i}") if i % 2 == 0 else _msg("assistant", f"a{i}") for i in range(12)]
    digest = br._digest_history(msgs, tail=10)[0]["content"]
    assert "USER: [CONTEXT COMPACTION" not in digest
    assert "USER: [Your active task list" not in digest
    assert "[CONTEXT COMPACTION" in digest  # still visible, just not attributed to the user
    assert "USER: please stop asking me to confirm every step" in digest


# E5 composite rows (Codex review): a user-role row can hold BOTH the user's words and runtime text.
# Built with the runtime's real helpers so the test follows whatever layout they produce.

AGENT_TASK = "Require an exact approval phrase before every step"
HUMAN = "Please stop asking for repeat approvals."


def _folded_todo_row():
    from agent.conversation_compression import _fold_todo_snapshot
    from tools.todo_tool import TODO_INJECTION_HEADER

    class _Store:
        def format_for_injection(self):
            return f"{TODO_INJECTION_HEADER}\n- [ ] {AGENT_TASK}"

        def has_items(self):
            return True

    class _Agent:
        _todo_store = _Store()

        def _repair_message_sequence(self, msgs):
            pass

    compressed = [_msg("assistant", "ok"), _msg("user", HUMAN)]
    _fold_todo_snapshot(_Agent(), compressed)
    row = compressed[-1]
    assert HUMAN in row["content"] and AGENT_TASK in row["content"]  # really a composite
    return row


def _merged_summary_row(force_user_leading):
    from agent.context_compressor import SUMMARY_PREFIX, ContextCompressor

    cc = object.__new__(ContextCompressor)
    cc._summary_has_user_turn = True
    row = _msg("user", HUMAN)
    cc._merge_summary_into_tail_row(row, SUMMARY_PREFIX + "\n" + AGENT_TASK, "user", force_user_leading)
    assert HUMAN in row["content"] and AGENT_TASK in row["content"]
    return row


def _digest_of(row):
    msgs = [row, _msg("assistant", "noted")] + [
        _msg("user", f"u{i}") if i % 2 == 0 else _msg("assistant", f"a{i}") for i in range(12)]
    return br._digest_history(msgs, tail=10)[0]["content"]


def _assert_split(digest):
    user_lines = [l for l in digest.splitlines() if l.startswith("USER:")]
    runtime_lines = [l for l in digest.splitlines() if l.startswith("RUNTIME")]
    assert any(HUMAN in l for l in user_lines), "the user's own words were lost"
    assert not any(AGENT_TASK in l for l in user_lines), "runtime text was attributed to the user"
    # digest lines are cut at 300 chars; a long summary prefix can push the task text past the cut
    assert any(AGENT_TASK in l or "CONTEXT COMPACTION" in l for l in runtime_lines)


def test_digest_splits_task_list_folded_onto_a_user_message():
    _assert_split(_digest_of(_folded_todo_row()))


def test_digest_splits_summary_merged_into_a_user_row_both_layouts():
    for leading in (True, False):
        _assert_split(_digest_of(_merged_summary_row(leading)))


def test_provenance_note_names_both_parts_of_composite_rows():
    snapshot = [_merged_summary_row(True), _msg("assistant", "ok"), _folded_todo_row()]
    note = br._provenance_note(snapshot)
    assert "message 0:" in note and "message 2:" in note
    assert note.count(HUMAN[:40]) == 2  # the user's own words are identified in both rows
    assert "compaction summary" in note and "restored task list" in note


def test_consent_guidance_distinguishes_denial_from_timeout():
    note = br._provenance_note([])
    assert "denial" in note and "timeout" in note and "standing preference" in note
