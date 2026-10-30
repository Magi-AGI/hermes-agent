"""Per-process segmented log files (gate H4 writer layout).

Contract: a process owns ``<stem>.d/<pid>-<start>.<seq>.log`` outright — no cross-process lock,
no rename, no shared file — and rollover simply opens the next segment. The handler itself is
host-independent (the owner is data); only ``setup_logging``'s choice of it is Windows-specific and
is pinned by the ``windows_only`` test in ``tests/test_hermes_logging.py``.
"""
import io
import logging
from pathlib import Path

import pytest

import hermes_logging
from hermes_logging_segments import _SegmentedFileHandler, segment_dir


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 0, msg, (), None)


def _handler(stem: Path, *, owner, max_bytes=0, backups=0) -> _SegmentedFileHandler:
    handler = _SegmentedFileHandler(
        str(stem), maxBytes=max_bytes, backupCount=backups, encoding="utf-8", owner=owner,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def test_records_land_in_the_owners_segment_and_never_in_the_stem_file(tmp_path):
    stem = tmp_path / "logs" / "agent.log"
    handler = _handler(stem, owner=(4242, "20261030-110000"))
    try:
        handler.handle(_record("hello segment"))
        handler.flush()
    finally:
        handler.close()
    segment = segment_dir(stem) / "4242-20261030-110000.1.log"
    assert segment.read_text(encoding="utf-8") == "hello segment\n"
    assert not stem.exists()
    assert not list(stem.parent.glob(".__*.lock"))


def test_rollover_opens_the_next_segment_without_renaming_and_keeps_backup_count_old_ones(tmp_path):
    stem = tmp_path / "logs" / "errors.log"
    owner = (7, "20261030-120000")
    handler = _handler(stem, owner=owner, max_bytes=40, backups=1)
    try:
        for i in range(6):
            handler.handle(_record(f"line-{i} {'x' * 20}"))
        handler.flush()
    finally:
        handler.close()
    d = segment_dir(stem)
    names = sorted(p.name for p in d.glob("*.log"))
    assert names == ["7-20261030-120000.5.log", "7-20261030-120000.6.log"], names
    # No rename: the line written into a segment is still in THAT segment afterwards.
    assert "line-4" in (d / "7-20261030-120000.5.log").read_text(encoding="utf-8")
    assert "line-5" in (d / "7-20261030-120000.6.log").read_text(encoding="utf-8")


def test_two_owners_never_share_or_prune_each_others_segments(tmp_path):
    stem = tmp_path / "logs" / "agent.log"
    a = _handler(stem, owner=(1, "20261030-100000"), max_bytes=40, backups=1)
    b = _handler(stem, owner=(2, "20261030-100001"), max_bytes=0, backups=0)
    try:
        b.handle(_record("b-only"))
        for i in range(6):
            a.handle(_record(f"a-{i} {'y' * 20}"))
        a.flush()
        b.flush()
    finally:
        a.close()
        b.close()
    d = segment_dir(stem)
    b_files = sorted(p.name for p in d.glob("2-*.log"))
    assert b_files == ["2-20261030-100001.1.log"]
    assert (d / b_files[0]).read_text(encoding="utf-8") == "b-only\n"
    assert all("a-" not in (d / n).read_text(encoding="utf-8") for n in b_files)
    assert len(list(d.glob("1-*.log"))) == 2  # a's own backup policy only touched a's files


def test_a_handler_recreated_in_the_same_process_continues_its_own_sequence(tmp_path):
    """Profile routing rebuilds per-home handlers mid-process; the rebuilt one must append to the
    newest own segment, not restart at .1 behind the segments already written."""
    stem = tmp_path / "logs" / "agent.log"
    owner = (99, "20261030-130000")
    first = _handler(stem, owner=owner, max_bytes=40, backups=5)
    try:
        for i in range(4):
            first.handle(_record(f"first-{i} {'z' * 20}"))
    finally:
        first.close()
    second = _handler(stem, owner=owner)
    try:
        second.handle(_record("second"))
        second.flush()
        assert second.current_segment.name == sorted(p.name for p in segment_dir(stem).glob("*.log"))[-1]
    finally:
        second.close()
    assert "second" in second.current_segment.read_text(encoding="utf-8")
    assert not (segment_dir(stem) / "99-20261030-130000.1.log").read_text(encoding="utf-8").endswith("second\n")


def test_a_successful_emit_updates_the_write_lag_metric(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_logging, "_LAST_WRITE_LAG", None)
    handler = _handler(tmp_path / "logs" / "agent.log", owner=(5, "20261030-140000"))
    try:
        handler.handle(_record("fresh"))
    finally:
        handler.close()
    lag = hermes_logging.last_write_lag_seconds()
    assert lag is not None and 0.0 <= lag < 5.0


def test_eio_from_a_segment_names_the_segment_once_then_recovers(tmp_path, capsys):
    class _SickStream(io.StringIO):
        def write(self, *_a):
            raise OSError(5, "Input/output error")

        seek = tell = flush = write

    handler = _handler(tmp_path / "logs" / "agent.log", owner=(8, "20261030-150000"))
    try:
        handler._open = lambda: _SickStream()  # every reopen is still sick
        for i in range(25):
            handler.handle(_record(f"sick {i}"))
        err = capsys.readouterr().err
        assert "--- Logging error ---" not in err
        assert err.count(str(handler.current_segment)) == 1
    finally:
        handler.close()



def test_backup_count_zero_keeps_every_own_segment(tmp_path):
    stem = tmp_path / "logs" / "agent.log"
    handler = _handler(stem, owner=(3, "20261030-160000"), max_bytes=40, backups=0)
    try:
        for i in range(4):
            handler.handle(_record(f"line-{i} {'q' * 20}"))
    finally:
        handler.close()
    assert sorted(p.name for p in segment_dir(stem).glob("*.log")) == [
        f"3-20261030-160000.{seq}.log" for seq in (1, 2, 3, 4)]


@pytest.mark.windows_only
def test_a_segment_held_open_at_one_rollover_is_pruned_at_the_next(tmp_path):
    """Windows refuses the unlink while a reader holds the segment; the next rollover sweeps every
    own segment beyond the backup count again, so the miss is not permanent."""
    stem = tmp_path / "logs" / "agent.log"
    handler = _handler(stem, owner=(4, "20261030-170000"), max_bytes=40, backups=1)
    d = segment_dir(stem)
    try:
        handler.handle(_record(f"line-0 {'w' * 20}"))
        handler.handle(_record(f"line-1 {'w' * 20}"))  # rolls to .2
        with open(d / "4-20261030-170000.1.log", "rb"):
            handler.handle(_record(f"line-2 {'w' * 20}"))  # rolls to .3; .1 is held → survives
            assert (d / "4-20261030-170000.1.log").exists()
        handler.handle(_record(f"line-3 {'w' * 20}"))  # rolls to .4; sweep removes .1 and .2
    finally:
        handler.close()
    assert sorted(p.name for p in d.glob("*.log")) == ["4-20261030-170000.3.log", "4-20261030-170000.4.log"]


# ── Retention (S2) ──────────────────────────────────────────────────────────────────────────────

def _old(path: Path, days: float, *, now: float) -> None:
    stamp = now - days * 86400
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    import os
    os.utime(path, (stamp, stamp))


def test_prune_removes_only_segments_older_than_retention(tmp_path):
    import time
    from hermes_logging_segments import prune_stale_segments

    now = time.time()
    logs = tmp_path / "logs"
    stale_agent = logs / "agent.d" / "11-20261001-000000.1.log"
    stale_errors = logs / "errors.d" / "11-20261001-000000.3.log"
    fresh = logs / "agent.d" / "12-20261029-000000.1.log"
    legacy = logs / "agent.log"
    legacy_backup = logs / "agent.log.1"
    stranger = logs / "agent.d" / "notes.txt"
    for p in (stale_agent, stale_errors, legacy, legacy_backup, stranger):
        _old(p, 10, now=now)
    _old(fresh, 1, now=now)

    removed = prune_stale_segments(logs, retention_days=7, now=now)

    assert sorted(removed) == sorted([stale_agent, stale_errors])
    assert not stale_agent.exists() and not stale_errors.exists()
    assert fresh.exists() and legacy.exists() and legacy_backup.exists() and stranger.exists()


def test_prune_is_off_when_retention_is_zero(tmp_path):
    import time
    from hermes_logging_segments import prune_stale_segments

    now = time.time()
    stale = tmp_path / "logs" / "agent.d" / "11-20261001-000000.1.log"
    _old(stale, 400, now=now)
    assert prune_stale_segments(tmp_path / "logs", retention_days=0, now=now) == []
    assert stale.exists()



def test_prune_ignores_a_non_finite_retention(tmp_path):
    """`retention_days: .nan` is valid YAML; it must disable pruning, not delete everything
    (every mtime compares False against NaN) (H4-R1-3a)."""
    import math
    import time
    from hermes_logging_segments import prune_stale_segments

    now = time.time()
    stale = tmp_path / "logs" / "agent.d" / "11-20261001-000000.1.log"
    _old(stale, 400, now=now)
    assert prune_stale_segments(tmp_path / "logs", retention_days=math.nan, now=now) == []
    assert stale.exists()


@pytest.mark.windows_only
def test_prune_skips_a_segment_another_handle_still_holds_open(tmp_path):
    """Liveness without a process scan: Windows refuses to unlink a file a live writer holds
    open, so an owner's current segment survives any pruner."""
    import time
    from hermes_logging_segments import prune_stale_segments

    now = time.time()
    held = tmp_path / "logs" / "agent.d" / "11-20261001-000000.1.log"
    _old(held, 30, now=now)
    with open(held, "a", encoding="utf-8"):
        removed = prune_stale_segments(tmp_path / "logs", retention_days=7, now=now)
        assert removed == []
        assert held.exists()
