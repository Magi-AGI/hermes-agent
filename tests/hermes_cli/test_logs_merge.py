"""Layout-agnostic log readers (gate H4): one timeline merged by timestamp from the legacy shared
file(s) and the Windows per-process segments. Every reader — ``hermes logs``, the dashboard
``/api/logs``, ``hermes debug share`` — goes through ``hermes_cli.logs_merge``."""
import asyncio
import threading
import time
from pathlib import Path

import pytest

from hermes_cli.logs_merge import (
    follow_merged,
    log_sources,
    read_merged_snapshot,
    read_merged_tail,
)


def _write(path: Path, *lines: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{l}\n" for l in lines), encoding="utf-8")
    return path


def test_sources_run_oldest_rotation_first_then_live_file_then_segments_by_start_time(tmp_path):
    logs = tmp_path / "logs"
    _write(logs / "agent.log.2", "r2")
    _write(logs / "agent.log.1", "r1")
    _write(logs / "agent.log", "live")
    _write(logs / "agent.d" / "900-20261030-100000.1.log", "late start, low pid")
    _write(logs / "agent.d" / "7-20261030-090000.2.log", "early start, seq 2")
    _write(logs / "agent.d" / "7-20261030-090000.1.log", "early start, seq 1")
    _write(logs / "agent.d" / "notes.txt", "not a segment")
    _write(logs / "errors.d" / "7-20261030-090000.1.log", "other stem")

    names = [p.relative_to(logs).as_posix() for p in log_sources(logs, "agent.log")]
    assert names == [
        "agent.log.2", "agent.log.1", "agent.log",
        "agent.d/7-20261030-090000.1.log", "agent.d/7-20261030-090000.2.log",
        "agent.d/900-20261030-100000.1.log",
    ]


def test_a_log_without_segments_is_just_its_file(tmp_path):
    logs = tmp_path / "logs"
    _write(logs / "desktop.log", "x")
    assert log_sources(logs, "desktop.log") == [logs / "desktop.log"]
    assert log_sources(logs, "missing.log") == []


def test_merge_interleaves_sources_by_timestamp_including_milliseconds(tmp_path):
    logs = tmp_path / "logs"
    _write(logs / "agent.log",
           "2026-10-30 10:00:00,100 INFO a: first",
           "2026-10-30 10:00:01,000 INFO a: third")
    _write(logs / "agent.d" / "5-20261030-095900.1.log",
           "2026-10-30 10:00:00,500 INFO b: second")
    lines = read_merged_tail(logs, "agent.log", 10)
    assert [l.split(": ")[-1].strip() for l in lines] == ["first", "second", "third"]


def test_continuation_lines_stay_attached_to_their_record(tmp_path):
    logs = tmp_path / "logs"
    _write(logs / "agent.d" / "1-20261030-100000.1.log",
           "2026-10-30 10:00:00,000 ERROR a: boom",
           "Traceback (most recent call last):",
           '  File "x.py", line 1, in <module>',
           "RuntimeError: boom")
    _write(logs / "agent.d" / "2-20261030-100000.1.log",
           "2026-10-30 10:00:00,200 INFO b: between")
    lines = [l.rstrip("\n") for l in read_merged_tail(logs, "agent.log", 10)]
    assert lines == [
        "2026-10-30 10:00:00,000 ERROR a: boom",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "RuntimeError: boom",
        "2026-10-30 10:00:00,200 INFO b: between",
    ]


def test_tail_honours_num_lines_after_filters_across_sources(tmp_path):
    logs = tmp_path / "logs"
    _write(logs / "agent.log",
           "2026-10-30 10:00:00 WARNING a: w1",
           "2026-10-30 10:00:02 INFO a: i2",
           "2026-10-30 10:00:04 WARNING a: w4")
    _write(logs / "agent.d" / "3-20261030-100000.1.log",
           "2026-10-30 10:00:01 ERROR b: e1",
           "2026-10-30 10:00:03 INFO b: i3",
           "2026-10-30 10:00:05 ERROR b: e5")
    lines = read_merged_tail(logs, "agent.log", 2, has_filters=True, min_level="WARNING")
    assert [l.split(": ")[-1].strip() for l in lines] == ["w4", "e5"]


def test_follow_picks_up_a_segment_created_after_it_started(tmp_path):
    logs = tmp_path / "logs"
    _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 10:00:00 INFO a: old")
    seen: list[str] = []
    got = threading.Event()
    stop = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        got.set()

    worker = threading.Thread(
        target=follow_merged,
        args=(logs, "agent.log", emit),
        kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1),
        daemon=True,
    )
    worker.start()
    try:
        time.sleep(0.2)  # past the initial seek-to-end of the existing segment
        _write(logs / "agent.d" / "2-20261030-100100.1.log", "2026-10-30 10:01:00 INFO b: new")
        assert got.wait(5.0), "a segment created after follow started was never read"
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["2026-10-30 10:01:00 INFO b: new"]


def test_snapshot_merges_sources_within_the_byte_budget_and_flags_truncation(tmp_path):
    logs = tmp_path / "logs"
    line = "A" * 99
    _write(logs / "agent.d" / "1-20261030-100000.1.log",
           *(f"2026-10-30 10:00:{i:02d} INFO a: {line}" for i in range(0, 50)))
    _write(logs / "agent.d" / "2-20261030-100000.1.log",
           *(f"2026-10-30 10:01:{i:02d} INFO b: {line}" for i in range(0, 50)))
    all_text, full_text, truncated = read_merged_snapshot(
        log_sources(logs, "agent.log"), max_bytes=2000, tail_lines=5)
    assert truncated is True
    assert len(full_text.encode("utf-8")) <= 2000
    assert full_text.startswith("2026-10-30 10:")  # cut on a line boundary
    assert full_text.rstrip("\n").endswith("INFO b: " + line)  # newest record last
    assert all_text.rstrip("\n").endswith("INFO b: " + line)
    assert len(all_text.splitlines()) >= 5


def test_snapshot_of_small_sources_is_whole_and_not_truncated(tmp_path):
    logs = tmp_path / "logs"
    _write(logs / "agent.log", "2026-10-30 10:00:00 INFO a: one")
    _write(logs / "agent.d" / "2-20261030-100000.1.log", "2026-10-30 10:00:01 INFO b: two")
    all_text, full_text, truncated = read_merged_snapshot(
        log_sources(logs, "agent.log"), max_bytes=10_000, tail_lines=5)
    assert truncated is False
    assert full_text == all_text
    assert [l.split(": ")[-1] for l in all_text.splitlines()] == ["one", "two"]


def test_dashboard_api_logs_reads_a_segments_only_home(hermes_home_with_segments):
    from hermes_cli.web_routers.status import get_logs

    result = asyncio.run(get_logs(file="agent", lines=10))
    assert [l.split(": ")[-1].strip() for l in result["lines"]] == ["seg one", "seg two"]


def test_debug_share_snapshot_reads_a_segments_only_home(hermes_home_with_segments):
    from hermes_cli.debug import _capture_log_snapshot

    snap = _capture_log_snapshot("agent", tail_lines=1, redact=False)
    assert snap.full_text is not None
    assert "seg one" in snap.full_text and "seg two" in snap.full_text
    assert snap.tail_text.endswith("seg two")



def test_filters_apply_to_whole_entries_so_tracebacks_follow_their_header(tmp_path):
    """--session / --component / search decide on the stamped header; the entry's continuation
    lines (tracebacks) go with it instead of being torn away line by line."""
    logs = tmp_path / "logs"
    _write(logs / "agent.d" / "1-20261030-100000.1.log",
           "2026-10-30 10:00:00,000 ERROR [sess_a] agent.x: boom",
           "Traceback (most recent call last):",
           "RuntimeError: boom",
           "2026-10-30 10:00:02,000 INFO [sess_b] tools.y: fine")
    entry = ["2026-10-30 10:00:00,000 ERROR [sess_a] agent.x: boom",
             "Traceback (most recent call last):", "RuntimeError: boom"]

    def read(**filters):
        return [l.rstrip("\n") for l in read_merged_tail(logs, "agent.log", 10, has_filters=True, **filters)]

    assert read(session_filter="sess_a") == entry
    assert read(component_prefixes=("tools",)) == ["2026-10-30 10:00:02,000 INFO [sess_b] tools.y: fine"]
    assert read(search="RUNTIMEERROR") == entry  # case-insensitive, matched anywhere in the entry
    assert read(min_level="WARNING") == entry


def test_a_tail_that_starts_mid_traceback_keeps_those_lines_first_without_crashing(tmp_path):
    """A bounded tail read of a big source can begin inside a traceback: those stampless lines are
    the oldest that source still holds, so they lead the merge instead of raising on sort."""
    logs = tmp_path / "logs"
    _write(logs / "agent.d" / "1-20261030-100000.1.log",
           '  File "x.py", line 1, in <module>', "RuntimeError: cut",
           "2026-10-30 10:00:05 INFO a: later")
    _write(logs / "agent.d" / "2-20261030-100000.1.log", "2026-10-30 10:00:01 INFO b: earlier")
    lines = [l.rstrip("\n") for l in read_merged_tail(logs, "agent.log", 10)]
    assert lines == ['  File "x.py", line 1, in <module>', "RuntimeError: cut",
                     "2026-10-30 10:00:01 INFO b: earlier", "2026-10-30 10:00:05 INFO a: later"]


def test_sources_order_segment_sequences_and_rotations_numerically(tmp_path):
    logs = tmp_path / "logs"
    for seq in (9, 10, 11):
        _write(logs / "agent.d" / f"7-20261030-090000.{seq}.log", f"seq {seq}")
    for n in (2, 9, 10):
        _write(logs / f"agent.log.{n}", f"rotation {n}")
    names = [p.name for p in log_sources(logs, "agent.log")]
    assert names == ["agent.log.10", "agent.log.9", "agent.log.2",
                     "7-20261030-090000.9.log", "7-20261030-090000.10.log", "7-20261030-090000.11.log"]


def test_merge_orders_seconds_only_and_millisecond_stamps_together(tmp_path):
    logs = tmp_path / "logs"
    _write(logs / "agent.log", "2026-10-30 10:00:00 INFO a: s0", "2026-10-30 10:00:01 INFO a: s1")
    _write(logs / "agent.d" / "2-20261030-100000.1.log", "2026-10-30 10:00:00,500 INFO b: ms")
    lines = read_merged_tail(logs, "agent.log", 10)
    assert [l.split(": ")[-1].strip() for l in lines] == ["s0", "ms", "s1"]


def test_dashboard_api_logs_search_keeps_a_matching_entrys_traceback(tmp_path, monkeypatch):
    from hermes_cli.web_routers.status import get_logs

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write(home / "logs" / "agent.d" / "1-20261030-100000.1.log",
           "2026-10-30 10:00:00 ERROR a: boom", "Traceback (most recent call last):", "RuntimeError: boom",
           "2026-10-30 10:00:01 INFO b: quiet")
    result = asyncio.run(get_logs(file="agent", lines=10, search="boom"))
    assert [l.rstrip("\n") for l in result["lines"]] == [
        "2026-10-30 10:00:00 ERROR a: boom", "Traceback (most recent call last):", "RuntimeError: boom"]



def _follow_until(logs, *, n, timeout=5.0, start_positions=None, **filters):
    """Run follow_merged in a thread until *n* lines were emitted (or timeout); return the lines."""
    seen: list[str] = []
    got = threading.Event()
    stop = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        if len(seen) >= n:
            got.set()

    kwargs = dict(stop=stop, poll_s=0.05, rescan_s=0.1, **filters)
    if start_positions is not None:
        kwargs["start_positions"] = start_positions
    worker = threading.Thread(target=follow_merged, args=(logs, "agent.log", emit), kwargs=kwargs, daemon=True)
    worker.start()
    try:
        got.wait(timeout)
    finally:
        stop.set()
        worker.join(5.0)
    return seen


def test_follow_filters_whole_entries_so_a_traceback_follows_its_header(tmp_path):
    """`hermes logs -f --session X` must print the matching entry's traceback lines, not just its
    header; an unrelated entry's lines stay out (H4-R1-2a)."""
    logs = tmp_path / "logs"
    seg = _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 09:59:59 INFO [sess_b] b: old")
    import time as _t
    stop_after = threading.Event()

    def writer():
        _t.sleep(0.3)
        with open(seg, "a", encoding="utf-8") as handle:
            handle.write("2026-10-30 10:00:00,000 ERROR [sess_a] a: boom\n")
            handle.write("Traceback (most recent call last):\n")
            handle.write("RuntimeError: boom\n")
            handle.write("2026-10-30 10:00:01,000 INFO [sess_b] b: other\n")
            handle.write("  continuation of other\n")
            handle.write("2026-10-30 10:00:02,000 INFO [sess_a] a: tail\n")

    threading.Thread(target=writer, daemon=True).start()
    seen = _follow_until(logs, n=4, session_filter="sess_a")
    assert seen == ["2026-10-30 10:00:00,000 ERROR [sess_a] a: boom", "Traceback (most recent call last):",
                    "RuntimeError: boom", "2026-10-30 10:00:02,000 INFO [sess_a] a: tail"]


def test_tail_reports_the_extents_it_read_and_follow_resumes_from_them(tmp_path):
    """Records appended between the initial tail and the follow loop are neither lost nor printed
    twice: read_merged_tail reports the byte extent it consumed per source and follow_merged resumes
    there (H4-R1-2b). A trailing partial line is left for the follow to complete."""
    logs = tmp_path / "logs"
    seg = _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 10:00:00 INFO a: one")
    with open(seg, "a", encoding="utf-8") as handle:
        handle.write("2026-10-30 10:00:01 INFO a: partial")  # no newline yet
    extents: dict = {}
    lines = [l.rstrip("\n") for l in read_merged_tail(logs, "agent.log", 10, extents=extents)]
    assert lines == ["2026-10-30 10:00:00 INFO a: one"]
    assert extents[seg] == len("2026-10-30 10:00:00 INFO a: one\n".encode()) or seg.read_bytes()[:extents[seg]].endswith(b"\n")
    # Between tail and follow: the partial line completes and another record lands.
    with open(seg, "a", encoding="utf-8") as handle:
        handle.write(" done\n2026-10-30 10:00:02 INFO a: two\n")
    seen = _follow_until(logs, n=2, start_positions=extents)
    assert seen == ["2026-10-30 10:00:01 INFO a: partial done", "2026-10-30 10:00:02 INFO a: two"]


def test_tail_lines_exclude_a_trailing_partial_line_on_both_read_paths(tmp_path):
    from hermes_cli.logs_merge import _tail_lines

    small = tmp_path / "small.log"
    small.write_bytes(b"2026-10-30 10:00:00 INFO a: one\n2026-10-30 10:00:01 INFO a: partial")
    lines, extent = _tail_lines(small, 10)
    assert [l.rstrip("\n") for l in lines] == ["2026-10-30 10:00:00 INFO a: one"]
    assert small.read_bytes()[:extent].endswith(b"one\n")

    big = tmp_path / "big.log"
    body = b"".join(f"2026-10-30 10:00:{i % 60:02d} INFO a: {'x' * 80}\r\n".encode() for i in range(15000))
    big.write_bytes(body + b"2026-10-30 10:59:59 INFO a: tail-partial")
    lines, extent = _tail_lines(big, 3)
    assert len(lines) == 3 and all(l.endswith("\n") and not l.endswith("\r\n") for l in lines)
    assert extent == len(body)


def test_snapshot_is_not_truncated_when_only_crlf_normalisation_shrank_the_bytes(tmp_path):
    """Windows segments are CRLF on disk; the merged snapshot is LF. truncated must mean bytes were
    dropped, not that the raw files were bigger than the LF text (H4-R1-4b)."""
    logs = tmp_path / "logs"
    line = "2026-10-30 10:00:00 INFO a: " + "y" * 60
    for pid in (1, 2):
        path = logs / "agent.d" / f"{pid}-20261030-100000.1.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
            for _ in range(100):
                handle.write(line + "\n")
    on_disk = sum(p.stat().st_size for p in (logs / "agent.d").glob("*.log"))
    as_lf = 200 * (len(line) + 1)
    budget = (on_disk + as_lf) // 2  # between the two sizes
    all_text, full_text, truncated = read_merged_snapshot(log_sources(logs, "agent.log"), max_bytes=budget, tail_lines=5)
    assert truncated is False and full_text == all_text


def test_snapshot_of_one_giant_line_keeps_its_tail_instead_of_reporting_empty(tmp_path):
    logs = tmp_path / "logs"
    giant = logs / "agent.d" / "1-20261030-100000.1.log"
    giant.parent.mkdir(parents=True)
    giant.write_bytes(b"2026-10-30 10:00:00 INFO a: " + b"z" * 9000 + b"\n")
    all_text, full_text, truncated = read_merged_snapshot([giant], max_bytes=1000, tail_lines=5)
    assert all_text and full_text and truncated is True
    assert full_text.endswith("z\n") and len(full_text.encode()) <= 1000


def test_debug_share_reports_file_empty_for_a_zero_byte_segment(tmp_path, monkeypatch):
    """A home whose only agent log is a 0-byte segment has a log that is empty, not missing (H4-R1-5)."""
    from hermes_cli.debug import _capture_log_snapshot

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    empty = home / "logs" / "agent.d" / "1-20261030-100000.1.log"
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")
    snap = _capture_log_snapshot("agent", tail_lines=5)
    assert snap.tail_text == "(file empty)" and snap.full_text is None



def test_follow_never_replays_rotated_backups_after_a_posix_style_rollover(tmp_path):
    """Rotated <stem>.log.N files are history: a follow must not poll them, or a rollover that
    renames the live file over .1 replays megabytes of already-seen lines (H4-R2-1)."""
    logs = tmp_path / "logs"
    _write(logs / "agent.log.1", *(f"2026-10-30 09:00:{i:02d} INFO a: old-{i}" for i in range(30)))
    _write(logs / "agent.log", *(f"2026-10-30 09:30:{i:02d} INFO a: live-{i}" for i in range(20)))
    extents: dict = {}
    read_merged_tail(logs, "agent.log", 5, extents=extents)
    stop = threading.Event()
    seen: list[str] = []
    got = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        got.set()

    worker = threading.Thread(target=follow_merged, args=(logs, "agent.log", emit),
                              kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1, start_positions=extents), daemon=True)
    worker.start()
    try:
        time.sleep(0.3)
        # stdlib-style rollover: live file renamed over .1 (smaller than the old .1), fresh live file.
        (logs / "agent.log.1").unlink()
        (logs / "agent.log").rename(logs / "agent.log.1")
        _write(logs / "agent.log", "2026-10-30 10:00:00 INFO a: after-rollover")
        assert got.wait(5.0)
        time.sleep(0.3)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["2026-10-30 10:00:00 INFO a: after-rollover"]


def test_a_zero_or_negative_line_count_prints_nothing_but_still_reports_extents(tmp_path):
    """`hermes logs -n 0 -f` is the idiom for "no history, just follow"; Python's [-0:] would
    dump everything (H4-R2-2)."""
    logs = tmp_path / "logs"
    seg = _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 10:00:00 INFO a: one", "2026-10-30 10:00:01 INFO a: two")
    extents: dict = {}
    assert read_merged_tail(logs, "agent.log", 0, extents=extents) == []
    assert extents[seg] == seg.stat().st_size
    assert read_merged_tail(logs, "agent.log", -1) == []


def test_dashboard_api_logs_clamps_a_non_positive_line_count(tmp_path, monkeypatch):
    from hermes_cli.web_routers.status import get_logs

    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write(home / "logs" / "agent.d" / "1-20261030-100000.1.log", *(f"2026-10-30 10:00:{i:02d} INFO a: l{i}" for i in range(20)))
    assert asyncio.run(get_logs(file="agent", lines=0))["lines"] == []
    assert asyncio.run(get_logs(file="agent", lines=-1))["lines"] == []


def test_follow_suppresses_continuations_of_an_unseen_header_but_keeps_a_seeded_verdict(tmp_path):
    """Under a filter, continuation lines whose header the follow never saw must not leak; the
    verdict of the entry the initial tail printed last is seeded so its traceback still follows
    (H4-R2-3a)."""
    logs = tmp_path / "logs"
    a = _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 10:00:00 ERROR [sess_a] a: boom")
    b = _write(logs / "agent.d" / "2-20261030-100000.1.log", "2026-10-30 10:00:00 ERROR [sess_b] b: other")
    extents: dict = {}
    verdicts: dict = {}
    read_merged_tail(logs, "agent.log", 10, has_filters=True, session_filter="sess_a", extents=extents, verdicts=verdicts)
    assert verdicts == {a: True, b: False}
    seen: list[str] = []
    stop = threading.Event()
    got = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        if len(seen) >= 2:
            got.set()

    worker = threading.Thread(target=follow_merged, args=(logs, "agent.log", emit),
                              kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1, start_positions=extents,
                                          start_passing=verdicts, session_filter="sess_a"), daemon=True)
    worker.start()
    try:
        time.sleep(0.3)
        with open(b, "a", encoding="utf-8") as handle:
            handle.write("Traceback for other\n")
        with open(a, "a", encoding="utf-8") as handle:
            handle.write("Traceback for boom\n2026-10-30 10:00:05 INFO [sess_a] a: later\n")
        assert got.wait(5.0)
        time.sleep(0.2)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["Traceback for boom", "2026-10-30 10:00:05 INFO [sess_a] a: later"]


def test_a_segment_born_between_tail_and_follow_is_read_from_its_start(tmp_path):
    """With the tail's extents in hand, any non-rotation source absent from them was born after
    the tail and must be read from 0, not from its current end (H4-R2-3b)."""
    logs = tmp_path / "logs"
    _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 10:00:00 INFO a: old")
    extents: dict = {}
    read_merged_tail(logs, "agent.log", 10, extents=extents)
    _write(logs / "agent.d" / "2-20261030-100100.1.log", "2026-10-30 10:01:00 INFO b: born-1", "2026-10-30 10:01:01 INFO b: born-2")
    seen: list[str] = []
    stop = threading.Event()
    got = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        if len(seen) >= 2:
            got.set()

    worker = threading.Thread(target=follow_merged, args=(logs, "agent.log", emit),
                              kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1, start_positions=extents), daemon=True)
    worker.start()
    try:
        assert got.wait(5.0)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["2026-10-30 10:01:00 INFO b: born-1", "2026-10-30 10:01:01 INFO b: born-2"]


def test_bounded_reads_prefer_recent_sources_by_bytes_and_report_what_they_dropped(tmp_path, monkeypatch):
    """Bounded reads spend a byte budget newest-first instead of a file count, so 300 one-line
    segments cannot hide an older, bigger process segment; a non-empty source left outside the
    budget is reported (extents at its size, snapshot truncated) (H4-R2-4, H4-R2-5)."""
    import hermes_cli.logs_merge as merge
    import os
    import time as _t

    logs = tmp_path / "logs"
    now = _t.time()
    big = _write(logs / "agent.d" / "1-20261030-080000.1.log", *(f"2026-10-30 08:00:{i % 60:02d} INFO serve: {'s' * 50}" for i in range(200)))
    os.utime(big, (now - 600, now - 600))
    for i in range(300):
        path = _write(logs / "agent.d" / f"{i + 2}-20261030-100000.1.log", f"2026-10-30 10:00:00 INFO cli{i}: one")
        os.utime(path, (now - 10, now - 10))
    # Budget comfortably holds every source: the older big segment is still read.
    lines = read_merged_tail(logs, "agent.log", 500, has_filters=True, component_prefixes=("serve",))
    assert len(lines) == 200
    # A budget that cannot hold everything: it is spent newest-first on bytes actually read, so the
    # older big segment gets only what is left (or nothing), and every byte is accounted for.
    monkeypatch.setattr(merge, "READ_BUDGET_BYTES", 300 * 64)
    read_bytes: list[int] = []
    real_tail = merge._tail_lines

    def counting_tail(path, n, *args, **kwargs):
        meta = kwargs.setdefault("meta", {})
        out = real_tail(path, n, *args, **kwargs)
        read_bytes.append(meta.get("read", 0))
        return out

    monkeypatch.setattr(merge, "_tail_lines", counting_tail)
    extents: dict = {}
    lines = read_merged_tail(logs, "agent.log", 500, has_filters=True, component_prefixes=("serve",), extents=extents)
    assert sum(read_bytes) <= 300 * 64 + 65536
    assert 0 <= len(lines) < 200 and extents[big] is not None
    all_text, full_text, truncated = read_merged_snapshot(log_sources(logs, "agent.log"), max_bytes=10_000_000, tail_lines=5)
    assert truncated is True



def test_orphan_continuation_lines_cannot_pass_a_level_or_since_filter(tmp_path):
    """A tail window that starts inside a big INFO entry holds headerless lines; under --level
    or --since they must fail (no header can vouch for them) instead of leaking as if they were
    the requested level, and the verdict handed to the follow must say so (H4-R3-1a)."""
    logs = tmp_path / "logs"
    seg = logs / "agent.d" / "1-20261030-100000.1.log"
    seg.parent.mkdir(parents=True)
    with open(seg, "w", encoding="utf-8") as handle:
        handle.write("2026-10-30 08:00:00 INFO a: big dump follows\n")
        for i in range(2500):
            handle.write(f"  row {i}\n")
        handle.write("2026-10-30 10:00:00 INFO a: done\n")
    verdicts: dict = {}
    lines = read_merged_tail(logs, "agent.log", 50, has_filters=True, min_level="WARNING", verdicts=verdicts)
    assert lines == []
    assert verdicts == {seg: False}
    # The same orphan lines are still searchable: search needs no header.
    assert [l.rstrip("\n") for l in read_merged_tail(logs, "agent.log", 1, search="row 2499")] == ["  row 2499"]


def test_an_unstamped_log_is_filtered_line_by_line(tmp_path):
    """mcp-stderr.log carries no timestamps at all: every line is its own record, so --session
    (substring) and search work per line instead of collapsing the whole file into one entry
    (H4-R3-1b)."""
    logs = tmp_path / "logs"
    _write(logs / "mcp-stderr.log",
           "===== [2026-10-30 10:00:00] starting MCP server 'srv' =====",
           "[srv] ready", "[other] ready", "[srv] timeout talking upstream")
    lines = [l.rstrip("\n") for l in read_merged_tail(logs, "mcp-stderr.log", 10, has_filters=True, session_filter="[srv]")]
    assert lines == ["[srv] ready", "[srv] timeout talking upstream"]
    lines = [l.rstrip("\n") for l in read_merged_tail(logs, "mcp-stderr.log", 10, search="TIMEOUT")]
    assert lines == ["[srv] timeout talking upstream"]


def test_follow_filters_an_unstamped_log_line_by_line(tmp_path):
    logs = tmp_path / "logs"
    path = _write(logs / "mcp-stderr.log", "[srv] old")
    seen: list[str] = []
    stop = threading.Event()
    got = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        if len(seen) >= 2:
            got.set()

    worker = threading.Thread(target=follow_merged, args=(logs, "mcp-stderr.log", emit),
                              kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1, session_filter="[srv]"), daemon=True)
    worker.start()
    try:
        time.sleep(0.3)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("[other] noise\n[srv] one\n[other] more\n[srv] two\n")
        assert got.wait(5.0)
        time.sleep(0.2)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["[srv] one", "[srv] two"]


def test_a_source_that_cannot_be_opened_keeps_its_extent_so_follow_never_replays_it(tmp_path, monkeypatch):
    """A transient open failure on one segment (scanner holding it on Windows) must leave its
    extent at its size — otherwise the follow treats it as born-after-tail and dumps it from
    byte 0 (H4-R3-2a)."""
    import hermes_cli.logs_merge as merge

    logs = tmp_path / "logs"
    locked = _write(logs / "agent.d" / "1-20261030-100000.1.log", *(f"2026-10-30 09:00:{i:02d} INFO a: history-{i}" for i in range(40)))
    fine = _write(logs / "agent.d" / "2-20261030-100000.1.log", "2026-10-30 10:00:00 INFO b: fine")
    real = merge._tail_lines

    def flaky(path, n, *args, **kwargs):
        if path == locked:
            raise PermissionError(13, "sharing violation")
        return real(path, n, *args, **kwargs)

    monkeypatch.setattr(merge, "_tail_lines", flaky)
    extents: dict = {}
    lines = read_merged_tail(logs, "agent.log", 10, extents=extents)
    assert [l.rstrip("\n") for l in lines] == ["2026-10-30 10:00:00 INFO b: fine"]
    assert extents[locked] == locked.stat().st_size and extents[fine] == fine.stat().st_size


def test_budget_ties_on_mtime_keep_the_newest_sources_and_stat_once(tmp_path, monkeypatch):
    """Equal mtimes (a copied log directory) must not spend the budget oldest-first (H4-R3-3)."""
    import os
    import time as _t
    import hermes_cli.logs_merge as merge
    from hermes_cli.logs_merge import _within_budget

    logs = tmp_path / "logs"
    paths = [_write(logs / "agent.d" / f"{i}-20261030-10000{i}.1.log", "2026-10-30 10:00:00 INFO a: " + "q" * 100) for i in range(1, 4)]
    now = _t.time()
    for p in paths:
        os.utime(p, (now, now))
    monkeypatch.setattr(merge, "READ_BUDGET_BYTES", 1)  # the first source always fits; nothing else does
    selected, dropped, _allowance = _within_budget(log_sources(logs, "agent.log"), 1_000_000)
    assert selected == [paths[-1]]
    assert set(dropped) == set(paths[:-1])


def test_filters_passed_without_the_has_filters_flag_still_apply(tmp_path):
    """Callers should not need a second flag to say "I passed filters" (H4-R3-4b)."""
    logs = tmp_path / "logs"
    _write(logs / "agent.log", "2026-10-30 10:00:00 INFO a: quiet", "2026-10-30 10:00:01 ERROR a: loud")
    lines = [l.rstrip("\n") for l in read_merged_tail(logs, "agent.log", 10, min_level="ERROR")]
    assert lines == ["2026-10-30 10:00:01 ERROR a: loud"]



def test_a_stamped_log_whose_window_holds_only_continuation_lines_is_still_a_stamped_log(tmp_path):
    """Stamped-ness is the writer's format (agent/errors/gateway/gui are always stamped), not a
    guess from whatever the tail window happened to catch: a window inside a huge INFO dump must
    fail --level and seed a False verdict, never be judged as an unstamped log (H4-R4-1b)."""
    logs = tmp_path / "logs"
    seg = logs / "agent.d" / "1-20261030-100000.1.log"
    seg.parent.mkdir(parents=True)
    with open(seg, "w", encoding="utf-8") as handle:
        handle.write("2026-10-30 08:00:00 INFO a: big dump follows\n")
        for i in range(2500):
            handle.write(f"  row {i}\n")
    verdicts: dict = {}
    assert read_merged_tail(logs, "agent.log", 50, min_level="WARNING", verdicts=verdicts) == []
    assert verdicts == {seg: False}


def test_follow_of_a_stamped_log_never_judges_a_continuation_line_on_its_own(tmp_path):
    """Under --level, a stamped source's continuation lines inherit their header's verdict and
    lines seen before any header are dropped — the follow must not fall back to per-line rules
    just because it has not seen a header yet (H4-R4-1a)."""
    logs = tmp_path / "logs"
    seg = _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 09:59:00 INFO a: old")
    seen: list[str] = []
    stop = threading.Event()
    got = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        if len(seen) >= 2:
            got.set()

    worker = threading.Thread(target=follow_merged, args=(logs, "agent.log", emit),
                              kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1, min_level="ERROR"), daemon=True)
    worker.start()
    try:
        time.sleep(0.3)
        with open(seg, "a", encoding="utf-8") as handle:
            handle.write("  orphan continuation of old\n")
            handle.write("2026-10-30 10:00:00 INFO a: quiet\n  quiet detail\n")
            handle.write("2026-10-30 10:00:01 ERROR a: boom\nTraceback for boom\n")
        assert got.wait(5.0)
        time.sleep(0.2)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["2026-10-30 10:00:01 ERROR a: boom", "Traceback for boom"]


def test_a_headerless_lead_cannot_pass_a_session_or_component_filter_either(tmp_path):
    """No header, no vouching: a stampless lead whose first line merely contains the session id
    must not pass --session (H4-R4-2a)."""
    logs = tmp_path / "logs"
    seg = logs / "agent.d" / "1-20261030-100000.1.log"
    seg.parent.mkdir(parents=True)
    with open(seg, "w", encoding="utf-8") as handle:
        handle.write("2026-10-30 08:00:00 INFO [sess_b] b: dump\n")
        for i in range(2100):
            handle.write("  active sessions: sess_a, sess_b\n")
        handle.write("2026-10-30 10:00:00 INFO [sess_a] a: mine\n")
    lines = [l.rstrip("\n") for l in read_merged_tail(logs, "agent.log", 50, session_filter="sess_a")]
    assert lines == ["2026-10-30 10:00:00 INFO [sess_a] a: mine"]


def test_an_unreadable_source_among_readable_ones_is_reported(tmp_path, monkeypatch):
    """When the live file refuses but a rotation reads, the caller learns which source was
    skipped instead of silently seeing stale history (H4-R4-3)."""
    import hermes_cli.logs_merge as merge

    logs = tmp_path / "logs"
    old = _write(logs / "agent.log.1", "2026-10-30 09:00:00 INFO a: old")
    live = _write(logs / "agent.log", "2026-10-30 10:00:00 INFO a: live")
    real = merge._tail_lines

    def flaky(path, n, *args, **kwargs):
        if path == live:
            raise PermissionError(13, "denied")
        return real(path, n, *args, **kwargs)

    monkeypatch.setattr(merge, "_tail_lines", flaky)
    problems: list = []
    lines = read_merged_tail(logs, "agent.log", 10, problems=problems)
    assert [l.rstrip("\n") for l in lines] == ["2026-10-30 09:00:00 INFO a: old"]
    assert [p for p, _ in problems] == [live]



def test_a_tail_read_never_reads_more_than_its_byte_ceiling(tmp_path):
    """Source selection charges at most 1 MiB per source, so the read itself must stop there
    too — a file of giant records must not be read whole to find one newline (Codex LL-C2)."""
    from hermes_cli.logs_merge import _read_backward

    big = tmp_path / "big.log"
    big.write_bytes(b"2026-10-30 10:00:00 INFO a: " + b"g" * (3 * 1_048_576) + b"\n")
    with open(big, "rb") as handle:
        raw, start, exhausted = _read_backward(handle, big.stat().st_size, want_lines=1, max_bytes=65536)
    assert len(raw) <= 2 * 65536 and start > 0 and exhausted


def test_growth_reads_are_bounded_per_poll_and_resume(tmp_path):
    from hermes_cli.logs_merge import _read_growth

    path = tmp_path / "seg.log"
    path.write_bytes(b"".join(f"2026-10-30 10:00:{i % 60:02d} INFO a: line-{i}\n".encode() for i in range(100)))
    lines, position = _read_growth(path, 0, max_bytes=500)
    assert 0 < len(lines) < 100 and position <= 500
    rest, end = _read_growth(path, position, max_bytes=10 ** 9)
    assert len(lines) + len(rest) == 100 and end == path.stat().st_size


def test_follow_reads_the_rotated_suffix_and_the_whole_replacement_when_it_grows_past_the_old_offset(tmp_path):
    """Rotation is a source transition, not a size change: the lines appended after the tail
    but before the rename live in the rotated file; the replacement starts at 0 even when it is
    already bigger than the old offset (Codex LL-C3)."""
    logs = tmp_path / "logs"
    _write(logs / "agent.log", *(f"2026-10-30 09:00:{i:02d} INFO a: seen-{i}" for i in range(30)))
    extents: dict = {}
    identities: dict = {}
    read_merged_tail(logs, "agent.log", 5, extents=extents, identities=identities)
    live = logs / "agent.log"
    with open(live, "a", encoding="utf-8") as handle:
        for i in range(5):
            handle.write(f"2026-10-30 09:30:{i:02d} INFO a: late-{i}\n")
    live.rename(logs / "agent.log.1")
    _write(live, *(f"2026-10-30 10:00:{i:02d} INFO a: new-{i}" for i in range(45)))  # bigger than the old offset
    seen: list[str] = []
    stop = threading.Event()
    got = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n").split(": ")[-1])
        if len(seen) >= 50:
            got.set()

    worker = threading.Thread(target=follow_merged, args=(logs, "agent.log", emit),
                              kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1, start_positions=extents,
                                          start_identities=identities), daemon=True)
    worker.start()
    try:
        assert got.wait(5.0), seen
        time.sleep(0.2)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == [f"late-{i}" for i in range(5)] + [f"new-{i}" for i in range(45)]



def test_a_giant_final_record_is_still_shown_bounded_and_its_extent_is_the_file_end(tmp_path):
    """A >1 MiB final record must not make the tail read as empty: the bounded window's tail is
    shown and the extent is the end of the file (the record is complete) (H4-R6-2a)."""
    from hermes_cli.logs_merge import _tail_lines

    big = tmp_path / "big.log"
    big.write_bytes(b"2026-10-30 10:00:00 INFO a: head\n" + b"2026-10-30 10:00:01 INFO a: " + b"g" * (2 * 1_048_576) + b"\n")
    lines, extent = _tail_lines(big, 1)
    assert len(lines) == 1 and lines[0].endswith("g\n") and len(lines[0]) <= 1_048_576 + 1
    assert extent == big.stat().st_size


def test_an_unfinished_giant_line_is_not_skipped_by_the_follow(tmp_path):
    """A file ending with >1 MiB of a line that has no newline yet: the tail shows nothing of it
    and reports an extent INSIDE it, so the follow emits it once the newline lands instead of
    starting past it (H4-R6-2b)."""
    from hermes_cli.logs_merge import _read_growth, _tail_lines

    path = tmp_path / "agent.log"
    path.write_bytes(b"2026-10-30 10:00:00 INFO a: head\n" + b"2026-10-30 10:00:01 INFO a: " + b"u" * (2 * 1_048_576))
    lines, extent = _tail_lines(path, 5)
    assert lines == [] or all("u" * 10 not in l for l in lines)
    assert extent < path.stat().st_size
    with open(path, "ab") as handle:
        handle.write(b" done\n")
    emitted: list[str] = []
    position = extent
    while True:
        chunk, position = _read_growth(path, position)
        emitted.extend(chunk)
        if position >= path.stat().st_size or not chunk:
            break
    assert "".join(emitted).endswith("done\n")


def test_follow_keeps_a_filtered_entrys_traceback_across_a_rotation(tmp_path):
    """The lines a rotated file received after the tail are judged as the SAME source: an ERROR
    entry's traceback that straddles the rollover follows its header under --level ERROR, and the
    replacement starts clean (H4-R6-1)."""
    logs = tmp_path / "logs"
    live = _write(logs / "agent.log", "2026-10-30 09:00:00 INFO a: quiet", "2026-10-30 09:00:01 ERROR a: boom")
    extents: dict = {}
    verdicts: dict = {}
    identities: dict = {}
    read_merged_tail(logs, "agent.log", 5, min_level="ERROR", extents=extents, verdicts=verdicts, identities=identities)
    with open(live, "a", encoding="utf-8") as handle:
        handle.write("Traceback for boom\nRuntimeError: boom\n")
    live.rename(logs / "agent.log.1")
    _write(live, "2026-10-30 10:00:00 INFO a: fresh", "  fresh detail",
           "2026-10-30 10:00:01 ERROR a: again", "Traceback for again")
    seen: list[str] = []
    stop = threading.Event()
    got = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        if len(seen) >= 4:
            got.set()

    worker = threading.Thread(target=follow_merged, args=(logs, "agent.log", emit),
                              kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1, start_positions=extents,
                                          start_passing=verdicts, start_identities=identities, min_level="ERROR"), daemon=True)
    worker.start()
    try:
        assert got.wait(5.0), seen
        time.sleep(0.2)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["Traceback for boom", "RuntimeError: boom", "2026-10-30 10:00:01 ERROR a: again", "Traceback for again"]


def test_follow_reads_through_a_double_rotation(tmp_path):
    """Two rollovers between polls: the old file's suffix is read wherever it went (.2 by then)
    and nothing is duplicated or lost (H4-R6-1c)."""
    logs = tmp_path / "logs"
    live = _write(logs / "agent.log", *(f"2026-10-30 09:00:{i:02d} INFO a: old-{i}" for i in range(10)))
    extents: dict = {}
    identities: dict = {}
    read_merged_tail(logs, "agent.log", 5, extents=extents, identities=identities)
    seen: list[str] = []
    stop = threading.Event()
    got = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n").split(": ")[-1])
        if len(seen) >= 6:
            got.set()

    worker = threading.Thread(target=follow_merged, args=(logs, "agent.log", emit),
                              kwargs=dict(stop=stop, poll_s=0.5, rescan_s=5.0, start_positions=extents,
                                          start_identities=identities), daemon=True)
    worker.start()
    try:
        time.sleep(0.2)
        with open(live, "a", encoding="utf-8") as handle:
            handle.write("2026-10-30 09:30:00 INFO a: late-a\n")
        live.rename(logs / "agent.log.1")
        _write(live, "2026-10-30 09:40:00 INFO a: mid-a", "2026-10-30 09:40:01 INFO a: mid-b")
        (logs / "agent.log.1").rename(logs / "agent.log.2")
        live.rename(logs / "agent.log.1")
        _write(live, "2026-10-30 09:50:00 INFO a: new-a", "2026-10-30 09:50:01 INFO a: new-b", "2026-10-30 09:50:02 INFO a: new-c")
        assert got.wait(8.0), seen
        time.sleep(0.6)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["late-a", "mid-a", "mid-b", "new-a", "new-b", "new-c"]



def test_tail_reads_honour_the_aggregate_budget_even_when_files_grow_after_selection(tmp_path, monkeypatch):
    """The budget is spent on bytes actually read, not on sizes seen at selection: sources that
    grow between selection and open cannot blow past it (Codex LL-C2)."""
    import hermes_cli.logs_merge as merge

    logs = tmp_path / "logs"
    paths = [_write(logs / "agent.d" / f"{i}-20261030-10000{i}.1.log", f"2026-10-30 10:00:0{i} INFO a: small-{i}") for i in range(1, 5)]
    monkeypatch.setattr(merge, "READ_BUDGET_BYTES", 64 * 1024)
    real_select = merge._within_budget

    def select_then_grow(*args, **kwargs):
        result = real_select(*args, **kwargs)
        for p in paths:  # every selected source grows well past the budget before it is opened
            with open(p, "ab") as handle:
                handle.write(b"".join(f"2026-10-30 10:01:{i % 60:02d} INFO a: {'g' * 100}\n".encode() for i in range(2000)))
        return result

    monkeypatch.setattr(merge, "_within_budget", select_then_grow)
    read_bytes: list[int] = []
    real_tail = merge._tail_lines

    def counting_tail(path, n, *args, **kwargs):
        meta = kwargs.setdefault("meta", {})
        out = real_tail(path, n, *args, **kwargs)
        read_bytes.append(meta.get("read", 0))
        return out

    monkeypatch.setattr(merge, "_tail_lines", counting_tail)
    lines = read_merged_tail(logs, "agent.log", 50)
    assert lines  # still answers
    assert read_bytes and all(r > 0 for r in read_bytes)  # the reader reports what it read
    assert sum(read_bytes) <= 64 * 1024 + 4 * 65536  # the budget plus one chunk of slack per source at most


def test_snapshot_reads_honour_the_aggregate_budget_and_flag_what_they_could_not_read(tmp_path, monkeypatch):
    import hermes_cli.logs_merge as merge

    logs = tmp_path / "logs"
    for i in range(1, 5):
        path = logs / "agent.d" / f"{i}-20261030-10000{i}.1.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(f"2026-10-30 10:0{i}:{j % 60:02d} INFO a: {'s' * 100}\n".encode() for j in range(1000)))
    monkeypatch.setattr(merge, "READ_BUDGET_BYTES", 100 * 1024)
    read_bytes: list[int] = []
    real = merge._tail_bytes

    def counting(path, *args, **kwargs):
        out = real(path, *args, **kwargs)
        read_bytes.append(out[3])
        return out

    monkeypatch.setattr(merge, "_tail_bytes", counting)
    all_text, full_text, truncated = read_merged_snapshot(log_sources(logs, "agent.log"), max_bytes=1_000_000, tail_lines=5)
    assert all_text and truncated is True
    assert sum(read_bytes) <= 100 * 1024 + 65536



def test_snapshot_window_reads_the_full_requested_bytes_and_starts_on_a_line_boundary(tmp_path):
    """`hermes debug share` asks for ~500 KB; the read must deliver that much (not stop at the
    first chunk once it has enough lines) and must not drop a complete first line when the cut
    already lands on a boundary (H4-R8-1)."""
    from hermes_cli.logs_merge import _tail_bytes

    big = tmp_path / "agent.log"
    line = ("2026-10-30 10:00:00 INFO a: " + "x" * 71 + "\n").encode()  # 100 bytes
    big.write_bytes(line * 20000)  # 2 MB
    raw, size, cut, read = _tail_bytes(big, 512_000, 100)
    assert size == 2_000_000 and cut is True
    assert read >= 512_000
    assert len(raw) >= 512_000 - 100 and len(raw) % 100 == 0  # whole lines, nothing stripped
    assert raw.startswith(b"2026-10-30") and raw.endswith(b"\n")


def test_a_source_that_was_empty_at_selection_is_still_read_and_not_called_truncation(tmp_path, monkeypatch):
    """Budget is spent on bytes actually read with a per-source cap; a 0-byte source at selection
    time (a segment just opened) that fills before the read is read, and an empty source never
    makes the snapshot claim truncation (H4-R8-2)."""
    import hermes_cli.logs_merge as merge

    logs = tmp_path / "logs"
    _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 10:00:00 INFO a: old")
    fresh = logs / "agent.d" / "2-20261030-100001.1.log"
    fresh.write_bytes(b"")
    real_select = merge._within_budget

    def select_then_fill(*args, **kwargs):
        out = real_select(*args, **kwargs)
        fresh.write_text("2026-10-30 10:00:01 INFO b: startup-1\n2026-10-30 10:00:02 INFO b: startup-2\n", encoding="utf-8")
        return out

    monkeypatch.setattr(merge, "_within_budget", select_then_fill)
    extents: dict = {}
    lines = [l.rstrip("\n").split(": ")[-1] for l in read_merged_tail(logs, "agent.log", 10, extents=extents)]
    assert lines == ["old", "startup-1", "startup-2"]
    assert extents[fresh] == fresh.stat().st_size
    monkeypatch.setattr(merge, "_within_budget", real_select)
    empty = logs / "agent.d" / "3-20261030-100002.1.log"
    empty.write_bytes(b"")
    all_text, full_text, truncated = read_merged_snapshot(log_sources(logs, "agent.log"), max_bytes=1_000_000, tail_lines=5)
    assert truncated is False and "startup-2" in all_text


def test_tail_bytes_with_no_allowance_reads_nothing(tmp_path):
    from hermes_cli.logs_merge import _tail_bytes

    path = _write(tmp_path / "agent.log", "2026-10-30 10:00:00 INFO a: one")
    raw, size, cut, read = _tail_bytes(path, 1000, 5, allowance=0)
    assert raw == b"" and read == 0 and cut is True and size == path.stat().st_size


@pytest.fixture
def hermes_home_with_segments(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    logs = home / "logs"
    _write(logs / "agent.d" / "1-20261030-100000.1.log", "2026-10-30 10:00:00 INFO a: seg one")
    _write(logs / "agent.d" / "2-20261030-100001.1.log", "2026-10-30 10:00:01 INFO b: seg two")
    return home


@pytest.mark.windows_only
def test_follow_holds_no_handle_on_a_source_between_polls(tmp_path):
    """Windows refuses to unlink a file another handle holds open, so a follow that keeps one
    handle per source pins EVERY dead-process segment against retention pruning for as long as
    `hermes logs -f` runs. Follow must read growth without holding the file open."""
    logs = tmp_path / "logs"
    old = _write(logs / "agent.d" / "1-20261030-090000.1.log", "2026-10-30 09:00:00 INFO p1: old")
    seen: list[str] = []
    got = threading.Event()
    stop = threading.Event()

    def emit(line: str) -> None:
        seen.append(line.rstrip("\n"))
        got.set()

    worker = threading.Thread(
        target=follow_merged, args=(logs, "agent.log", emit),
        kwargs=dict(stop=stop, poll_s=0.05, rescan_s=0.1), daemon=True,
    )
    worker.start()
    try:
        time.sleep(0.3)
        old.unlink()  # a pruner (another process) removes the dead segment mid-follow
        _write(logs / "agent.d" / "2-20261030-110000.1.log", "2026-10-30 11:00:00 INFO new: born")
        assert got.wait(5.0)
    finally:
        stop.set()
        worker.join(5.0)
    assert seen == ["2026-10-30 11:00:00 INFO new: born"]
