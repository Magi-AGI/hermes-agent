#!/usr/bin/env python3
"""Opt-in backfill: type historical system-injected user rows (``system_notice``) and pure compaction handoffs (``hidden``).

Rows written before ``agent/system_turn_display.py`` existed carry ``display_kind=NULL``, so the
desktop still paints old max-iterations requests, background-process notices and todo snapshots as
the user's own messages. This re-uses the SAME classifier the live write path uses, so a row is only
typed if new code would have typed it at write time. Only ``display_kind``/``display_metadata`` change;
role and content (what the model replays) are never touched.

Safe by construction:
  * dry run unless ``--apply``; prints counts per notice type either way;
  * idempotent — only rows with an empty ``display_kind`` are considered, so re-runs are no-ops;
  * ``--db`` is required (no default path), so it cannot hit a live profile by accident.

Run it with Hermes CLOSED: a running backend holds untyped in-memory copies of these rows, and the
tui rewind heal compares live vs durable display markers until the session is reloaded.

    python scripts/backfill_system_notice_display_kind.py --db <profile>/state.db            # dry run
    python scripts/backfill_system_notice_display_kind.py --db <profile>/state.db --apply    # write
Back the database up first (``VACUUM INTO`` or a cold copy of state.db + -wal + -shm).
"""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Longer rows can only match by prefix (every exact-text notice is far shorter than this).
_HEAD_CHARS = 8192
_BATCH = 500


def _classify(raw: str, compressed_summary: bool):
    """``(display_kind, display_metadata, count_label)`` for a row live writes would have typed, else None."""
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, user_originated_turn_view
    from agent.system_turn_display import classify_system_injected_user_turn
    from hermes_state import SessionDB
    msg = {"role": "user", "content": SessionDB._decode_content(raw)}
    if compressed_summary:
        # A PURE handoff is persisted "hidden" by live writes (_summary_display_kind). A carrier with a live
        # view (e.g. the #100818 in-flight replay) stays untyped: any kind would drop it from the user-turn
        # ordinals rewind addresses; display projections flag it instead (compaction_restated).
        msg[COMPRESSED_SUMMARY_METADATA_KEY] = True
        return ("hidden", None, "compaction_handoff") if user_originated_turn_view(msg) is None else None
    tag = classify_system_injected_user_turn(msg)
    return None if tag is None else (tag["display_kind"], tag["display_metadata"], tag["display_metadata"]["notice"])


def backfill(db_path: Path, *, apply: bool, session_id: str | None = None) -> collections.Counter:
    """Return per-notice counts of rows typed (``apply=True``) or that would be typed."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        sql = ("SELECT id, length(content), substr(content, 1, ?), _compressed_summary FROM messages "
               "WHERE role = 'user' AND (display_kind IS NULL OR display_kind = '') AND content IS NOT NULL")
        params: list = [_HEAD_CHARS]
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        from hermes_state import SessionDB
        pending: list[tuple[str, str | None, int]] = []
        counts: collections.Counter = collections.Counter()
        for row_id, length, head, compressed in conn.execute(sql, params):  # streamed: state.db can be GBs
            raw = head
            # Handoffs need the whole body (the live view follows the final end marker); so do JSON parts.
            if length > _HEAD_CHARS and (compressed or head.startswith(SessionDB._CONTENT_JSON_PREFIX)):
                raw = conn.execute("SELECT content FROM messages WHERE id = ?", (row_id,)).fetchone()[0]
            tag = _classify(raw, bool(compressed))
            if tag is None:
                continue
            kind, metadata, label = tag
            counts[label] += 1
            pending.append((kind, None if metadata is None else json.dumps(metadata), row_id))
        if apply:
            for start in range(0, len(pending), _BATCH):
                with conn:
                    conn.executemany(
                        "UPDATE messages SET display_kind = ?, display_metadata = ? "
                        "WHERE id = ? AND (display_kind IS NULL OR display_kind = '')",
                        pending[start:start + _BATCH])
        return counts
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, type=Path, help="path to a profile's state.db")
    parser.add_argument("--apply", action="store_true", help="write the tags (default: dry run)")
    parser.add_argument("--session", help="limit to one session id")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error(f"no such database: {args.db}")
    counts = backfill(args.db, apply=args.apply, session_id=args.session)
    verb = "typed" if args.apply else "would type (dry run; pass --apply to write)"
    print(f"{sum(counts.values())} row(s) {verb} in {args.db}")
    for notice, n in counts.most_common():
        print(f"  {n:7d}  {notice}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
