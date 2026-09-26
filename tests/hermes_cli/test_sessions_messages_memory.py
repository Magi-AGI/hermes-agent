"""The desktop's transcript read of a huge session stays bounded (checklist D5).

The 09-12 desktop OOM went ``GET /api/sessions/{id}/messages`` -> ``_read`` -> ``get_messages`` ->
``_read_all`` -> ``fetchall()`` on one 102 MB session: the route had a ``_limit`` in scope that the read
did not bound. Driven through the real route handler with the desktop's default (no limit, latest page,
compaction-archived history included), on a read-only store with and without the display-order
backfill, the response is one page and peak Python allocation scales with that page, not the
transcript (the legacy scan keeps only a fixed-size identity per row).
"""

import asyncio
import tracemalloc

import pytest

from hermes_constants import get_hermes_home
from hermes_state import SessionDB

ROWS, PAYLOAD, PAGE = 12_000, 4_000, 500  # ~48 MB of message content; the default page is 500 rows


@pytest.mark.parametrize("backfilled", [False, True], ids=["legacy_rows", "backfilled"])
def test_default_transcript_page_of_a_huge_session_is_bounded(backfilled):
    from hermes_cli.web_routers import sessions

    db = SessionDB(get_hermes_home() / "state.db")
    db.create_session("huge", source="desktop")
    rows = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"{i:05d}" + "x" * PAYLOAD}
            for i in range(ROWS)]
    db.append_messages_batch("huge", rows)
    if not backfilled:
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET display_order = NULL, display_identity = NULL WHERE session_id = 'huge'"))
    db.close()

    tracemalloc.start()
    try:
        body = asyncio.run(sessions.get_session_messages(
            "huge", profile=None, limit=None, offset=0, order=None, include_compacted=True))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 6 * PAGE * PAYLOAD, (
        f"peak {peak / 1e6:.1f} MB for a {PAGE * PAYLOAD / 1e6:.0f} MB page of a {ROWS * PAYLOAD / 1e6:.0f} MB transcript")
    assert body["pagination"]["limit"] == PAGE
    assert [m["content"][:5] for m in body["messages"]] == [f"{i:05d}" for i in range(ROWS - PAGE, ROWS)]
