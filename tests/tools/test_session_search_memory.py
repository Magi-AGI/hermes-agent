"""session_search reads of a huge session stay bounded (checklist D5).

The 09-12 desktop OOM was an unbounded message read of one 102 MB session (45,687 rows). The tool's read
shape shows only the first 20 + last 10 messages and a title hit only needs the first message id, yet
both loaded the whole transcript. Peak Python allocation while they run must stay a small fraction of
the transcript (tracemalloc traces every thread).
"""

import json
import tracemalloc

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import session_search

ROWS, PAYLOAD = 1200, 20_000  # ~24 MB of message content


@pytest.fixture
def huge(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("huge", source="cli")
    db.set_session_title("huge", "Hyperon wiki finalization")
    db.append_messages_batch("huge", [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i:05d}" + "x" * PAYLOAD}
        for i in range(ROWS)])
    # A small session with the same title shape: warms the tool's lazy imports, which would otherwise
    # land in the first measured call's peak.
    db.create_session("warm", source="cli")
    db.set_session_title("warm", "Warm-up session")
    db.append_message("warm", "user", "hello")
    session_search(session_id="warm", db=db)
    session_search(query="Warm-up session", db=db)
    yield db
    db.close()


def _peak(fn):
    tracemalloc.start()
    try:
        result = fn()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("call", [
    lambda db: session_search(session_id="huge", db=db),
    lambda db: session_search(query="Hyperon wiki finalization", db=db),
], ids=["read", "title_match"])
def test_reading_a_huge_session_never_materializes_the_transcript(huge, call):
    result, peak = _peak(lambda: json.loads(call(huge)))
    assert result.get("success", True) is not False, result
    assert peak < ROWS * PAYLOAD / 10, f"peak {peak / 1e6:.1f} MB for a {ROWS * PAYLOAD / 1e6:.0f} MB transcript"


def test_read_shape_still_reports_the_whole_session(huge):
    result = json.loads(session_search(session_id="huge", db=huge))
    ids = [m["content"][:5] for m in result["messages"]]
    assert result["message_count"] == ROWS and result["truncated"] is True
    assert ids == [f"{i:05d}" for i in [*range(20), *range(ROWS - 10, ROWS)]]
