"""Caller-authoritative turns never import another chat's context (review: Codex, A10 round 2).

Header-less API chats that open with the same system prompt and first message share one derived
session id (upstream #7127 grouping, kept by decision). When chat A compresses that shared session,
chat B's next turn may follow the compression tip for PERSISTENCE, but the model must only ever see
chat B's own history and a system prompt built for B's turn — never A's messages or the system
prompt A's compression stored on the child row.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent

_SID = "api-0123456789abcdef"
_CHILD = "api-0123456789abcdef-child"
_A_PRIVATE = "CHAT-A PRIVATE: the launch code is 0000"
_A_CHILD_PROMPT = "CHAT-A CHILD SYSTEM PROMPT (stored by A's compression)"


def _response(content):
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None, reasoning=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="test/model")
    resp.usage = None
    return resp


def _chat_a_compressed_the_shared_session(db: SessionDB) -> None:
    """Chat A's two turns, then A's rotation compression: the shared parent ends by compression and
    a live child carries A's compacted transcript and A's rebuilt system prompt."""
    db.create_session(_SID, source="api_server", system_prompt="shared parent prompt")
    for role, content in (("user", "hi"), ("assistant", "A reply"), ("user", _A_PRIVATE),
                          ("assistant", "noted")):
        db.append_message(_SID, role=role, content=content)
    db.create_session(_CHILD, source="api_server", parent_session_id=_SID, system_prompt=_A_CHILD_PROMPT)
    db.append_message(_CHILD, role="user", content=f"[CONTEXT COMPACTION] summary: {_A_PRIVATE}")
    db.append_message(_CHILD, role="assistant", content="A continues")
    db.end_session(_SID, "compression")
    assert db.get_compression_tip(_SID) == _CHILD


def _chat_b_agent(db: SessionDB) -> AIAgent:
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", model="test/model",
            quiet_mode=True, skip_context_files=True, skip_memory=True, session_db=db, session_id=_SID,
            platform="api_server",
        )
    agent.client = MagicMock()
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session_db = SessionDB(tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


def _request_text(call) -> str:
    return repr(call.kwargs.get("messages")) + repr(call.kwargs.get("system"))


def test_chat_b_model_request_never_contains_chat_a_after_a_compressed(db):
    """REAL turn setup (turn-start compression recovery, system-prompt restore, the loop); only the
    provider call is faked."""
    _chat_a_compressed_the_shared_session(db)
    agent = _chat_b_agent(db)
    agent.client.chat.completions.create.side_effect = [_response("B answer")]
    chat_b = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "B reply"}]

    result = agent.run_conversation(
        "chat B: what did I say?", conversation_history=chat_b, caller_history_authoritative=True)

    assert result["final_response"] == "B answer"
    calls = agent.client.chat.completions.create.call_args_list
    assert len(calls) == 1
    request = _request_text(calls[0])
    assert "B reply" in request and "chat B: what did I say?" in request
    assert "CHAT-A" not in request, "chat B's model saw chat A's messages or A's child system prompt"
    assert "A continues" not in request
    assert agent._cached_system_prompt != _A_CHILD_PROMPT
    # Persistence still followed the live tip: B's turn landed on the child, not the ended parent.
    assert agent.session_id == _CHILD
    child_rows = [m["content"] for m in db.get_messages(_CHILD)]
    assert child_rows[-2:] == ["chat B: what did I say?", "B answer"]
    assert "B reply" not in child_rows  # the caller's history is not re-persisted
    assert agent._caller_history_authoritative is False  # reset after the turn


def test_durable_continuation_still_adopts_the_compression_child(db):
    """Control: without caller authority (Desktop/gateway/CLI continuation) the turn still adopts the
    live child's transcript, as before."""
    _chat_a_compressed_the_shared_session(db)
    agent = _chat_b_agent(db)
    agent.client.chat.completions.create.side_effect = [_response("continued")]

    agent.run_conversation("next", conversation_history=[{"role": "user", "content": "stale"}])

    request = _request_text(agent.client.chat.completions.create.call_args_list[0])
    assert "A continues" in request
    assert agent.session_id == _CHILD


def test_in_loop_compression_adoption_paths_respect_caller_authority():
    """Unit: the two adoption paths inside compress_context — the grown durable parent and the
    already-rotated parent — never substitute the model's messages for a caller-authoritative turn."""
    from agent import conversation_compression as cc

    agent = SimpleNamespace(_caller_history_authoritative=True, _cached_system_prompt="B prompt",
                            session_id=_SID, context_compressor=None, _memory_manager=None)
    durable = MagicMock()
    durable.get_messages_as_conversation.side_effect = AssertionError("durable parent was read")
    lease = SimpleNamespace(db=durable, sid=_SID)
    mine = [{"role": "user", "content": "B only"}]
    assert cc._adopt_grown_durable_parent(agent, lease, mine) is None

    class _RotatedDB:
        def get_compression_tip(self, sid):
            return _CHILD

        def get_session(self, sid):
            return {"id": sid, "ended_at": None, "system_prompt": _A_CHILD_PROMPT}

        def get_messages_as_conversation(self, sid, **kw):
            raise AssertionError("tip transcript was imported")

    released = []
    rotated = SimpleNamespace(db=_RotatedDB(), sid=_SID, release=lambda: released.append(True))
    with patch.object(cc, "_session_was_rotated_by_compression", return_value=True):
        out = cc._adopt_if_parent_rotated(agent, rotated, mine, "sys")
    assert out == (mine, "B prompt")
    assert agent.session_id == _CHILD and released == [True]
    assert agent._cached_system_prompt == "B prompt"
