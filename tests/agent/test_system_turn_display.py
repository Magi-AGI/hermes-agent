"""System-injected user rows are typed when persisted, and the model's view is unchanged.

Scaffolding the loop rides on ``role='user'`` (the max-iterations wrap-up request, background-process
notices, the todo snapshot re-injected after compaction) used to persist with ``display_kind=NULL``,
so the desktop painted it as the user's own words. A real user turn must stay untyped.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
from hermes_state import SessionDB
from run_agent import AIAgent
from tools.todo_tool import TODO_INJECTION_HEADER

BACKGROUND_NOTICE = "[IMPORTANT: Background process proc_7 completed (exit code 0).]\nOutput tail: ok"
TODO_SNAPSHOT = TODO_INJECTION_HEADER + "\n- [ ] ship the fix"


@pytest.fixture()
def agent_db():
    tmp = tempfile.mkdtemp(prefix="system_turn_display_")
    db = SessionDB(Path(tmp) / "state.db")
    sid = "sess-system-turns"
    db.create_session(session_id=sid, source="desktop", model="test-model")
    agent = AIAgent(
        api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
        skip_context_files=True, skip_memory=True, session_db=db, session_id=sid,
    )
    agent._session_db_created = True
    try:
        yield agent, db, sid
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize(("content", "notice"), [
    (MAX_ITERATIONS_SUMMARY_REQUEST, "max_iterations"),
    (BACKGROUND_NOTICE, "background_process"),
    (TODO_SNAPSHOT, "todo_snapshot"),
])
def test_synthetic_user_rows_persist_typed_with_content_untouched(agent_db, content, notice):
    agent, db, sid = agent_db
    messages = [
        {"role": "user", "content": "please look into the flaky test"},
        {"role": "assistant", "content": "on it"},
        {"role": "user", "content": content},
    ]

    agent._flush_messages_to_session_db(messages)

    rows = [r for r in db.get_messages_as_conversation(sid) if r["role"] == "user"]
    real, synthetic = rows
    assert real.get("display_kind") is None
    assert synthetic["display_kind"] == "system_notice"
    assert synthetic["display_metadata"] == {"notice": notice}
    assert synthetic["content"] == content
    # Live memory agrees with the durable row (rewind healing compares display-marker status).
    assert messages[2]["display_kind"] == "system_notice"
    assert messages[0].get("display_kind") is None


def test_typed_rows_never_reach_the_wire(agent_db):
    """display_* is timeline metadata; the API copy must be byte-identical to an untyped row's."""
    from agent.turn_context import build_api_messages

    agent, _db, _sid = agent_db
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST}]
    untyped = [dict(m) for m in messages]
    agent._flush_messages_to_session_db(messages)
    assert messages[2]["display_kind"] == "system_notice"

    def wire(msgs):
        api_messages, _system = build_api_messages(
            agent, msgs, current_turn_user_idx=-1, ext_prefetch_cache=None, plugin_user_context=None,
            moa_config=None, active_system_prompt="SYSTEM")
        # Underscore keys (the flush's persistence marker) are transport-stripped bookkeeping.
        return [{k: v for k, v in m.items() if not k.startswith("_")} for m in api_messages]
    assert wire(messages) == wire(untyped)


def test_backfill_types_legacy_rows_idempotently_and_dry_runs_by_default(tmp_path):
    """Historical NULL-typed notices get the same tag live writes produce; real turns and content stay put."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "backfill_system_notice_display_kind",
        Path(__file__).resolve().parents[2] / "scripts" / "backfill_system_notice_display_kind.py")
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session(session_id="s1", source="desktop", model="m")
    db.append_message("s1", role="user", content="real ask")
    db.append_message("s1", role="assistant", content="ok")
    db.append_message("s1", role="user", content=BACKGROUND_NOTICE)
    db.append_message("s1", role="user", content=MAX_ITERATIONS_SUMMARY_REQUEST)
    db.close()

    assert backfill.backfill(db_path, apply=False) == {"background_process": 1, "max_iterations": 1}
    db = SessionDB(db_path)
    assert all(not r.get("display_kind") for r in db.get_messages_as_conversation("s1"))
    db.close()

    backfill.backfill(db_path, apply=True)
    assert backfill.backfill(db_path, apply=True) == {}  # idempotent
    db = SessionDB(db_path)
    rows = db.get_messages_as_conversation("s1")
    db.close()
    assert [r.get("display_kind") for r in rows] == [None, None, "system_notice", "system_notice"]
    assert [r["content"] for r in rows] == ["real ask", "ok", BACKGROUND_NOTICE, MAX_ITERATIONS_SUMMARY_REQUEST]


def test_backfill_hides_pure_handoffs_but_leaves_restated_request_carriers_addressable(tmp_path):
    """Only a handoff with no live view gets the "hidden" live writes give it; a #100818 replay carrier is a
    user turn for rewind ordinals and must stay untyped."""
    import importlib.util

    import agent.context_compressor as cc

    spec = importlib.util.spec_from_file_location(
        "backfill_system_notice_display_kind",
        Path(__file__).resolve().parents[2] / "scripts" / "backfill_system_notice_display_kind.py")
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)

    handoff = cc.ContextCompressor._with_summary_prefix("## Goal\n" + "x" * 9000) + "\n\n" + cc._SUMMARY_END_MARKER
    carrier = handoff + "\n\n" + cc._INFLIGHT_TASK_REPLAY_HEADER + "\n\nContinue with C2-2."
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session(session_id="s1", source="desktop", model="m")
    db.append_message("s1", role="user", content=handoff, _compressed_summary=True)
    db.append_message("s1", role="assistant", content="ok")
    db.append_message("s1", role="user", content=carrier, _compressed_summary=True)
    db.close()

    assert backfill.backfill(db_path, apply=True) == {"compaction_handoff": 1}
    db = SessionDB(db_path)
    rows = db.get_messages_as_conversation("s1")
    db.close()
    assert [r.get("display_kind") for r in rows] == ["hidden", None, None]


def test_legacy_untagged_notices_are_typed_at_display_projection():
    """Codex review #13: rows written before write-time tagging must still project as system
    notices on both display paths, so desktop and backend rewind counting agree without a backfill."""
    from hermes_cli.web_routers.sessions import _project_for_display
    from tui_gateway import server

    legacy = [{"role": "user", "content": "real ask"}, {"role": "assistant", "content": "ok"},
              {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST}]
    rest = _project_for_display([dict(m) for m in legacy])
    assert [m.get("display_kind") for m in rest] == [None, None, "system_notice"]
    gateway = server._history_to_messages([dict(m) for m in legacy])
    kinds = [m.get("display_kind") for m in gateway if m.get("role") == "user"]
    assert kinds == [None, "system_notice"]
    assert legacy[2].get("display_kind") is None, "projection must not mutate the stored row"


def test_notice_unwrapped_from_a_compaction_carrier_is_typed():
    """Codex review round 3 #2: a system notice preserved after a compaction handoff is exposed
    by unwrapping; it must project as a notice (not a user turn) on both paths, while a genuine
    user turn after it stays a user turn."""
    import agent.context_compressor as cc
    from hermes_cli.web_routers.sessions import _project_for_display
    from tui_gateway import server

    handoff = cc.ContextCompressor._with_summary_prefix("## Goal\nship it") + "\n\n" + cc._SUMMARY_END_MARKER
    carrier = {"role": "user", "content": handoff + "\n\n" + MAX_ITERATIONS_SUMMARY_REQUEST,
               "_compressed_summary": True}
    msgs = [carrier, {"role": "assistant", "content": "summary"}, {"role": "user", "content": "real ask"}]
    rest = _project_for_display([dict(m) for m in msgs])
    assert [m.get("display_kind") for m in rest if m["role"] == "user"] == ["system_notice", None]
    gateway = server._history_to_messages([dict(m) for m in msgs])
    assert [m.get("display_kind") for m in gateway if m.get("role") == "user"] == ["system_notice", None]
