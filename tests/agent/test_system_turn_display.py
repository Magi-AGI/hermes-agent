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
