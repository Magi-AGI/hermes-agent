"""hardening-sweep S4 (gate H4, bounded half): the file writer exposes its write lag.

Measured live (2026-10-28): agent.log ran ~30 minutes behind the wall clock — the cross-process
`concurrent_log_handler` lock plus a reopen per record made the single queue-listener writer slower
than record production, silently corrupting every log-based timeline. The writer-layout redesign
(per-process files vs the shared lock) is a recorded product decision; this half makes the lag
VISIBLE: a module metric updated on every successful write, and a rate-limited stderr warning once
the lag passes a minute (stderr, because warning into the lagging log would be read 30 minutes
late).
"""

from __future__ import annotations

import logging
import time

import hermes_logging


def _file_handler(tmp_path):
    return hermes_logging._ManagedRotatingFileHandler(
        str(tmp_path / "agent.log"), maxBytes=1_000_000, backupCount=1, encoding="utf-8")


def _record(age_seconds: float) -> logging.LogRecord:
    record = logging.LogRecord("hermes", logging.INFO, __file__, 1, "late line", (), None)
    record.created = time.time() - age_seconds
    return record


def test_write_lag_metric_tracks_the_emitted_records_age(tmp_path):
    handler = _file_handler(tmp_path)
    try:
        handler.emit(_record(120.0))
    finally:
        handler.close()
    lag = hermes_logging.last_write_lag_seconds()
    assert lag is not None and lag >= 119.0, f"write lag not tracked: {lag!r}"


def test_a_laggy_writer_warns_once_on_stderr(tmp_path, capfd, monkeypatch):
    monkeypatch.setattr(hermes_logging, "_LAG_WARNED_AT", 0.0)
    handler = _file_handler(tmp_path)
    try:
        handler.emit(_record(300.0))
        handler.emit(_record(301.0))  # within the warn interval: no second line
    finally:
        handler.close()
    err = capfd.readouterr().err
    assert err.count("log writer is") == 1, f"expected exactly one lag warning, got: {err!r}"
    assert "behind" in err


def test_a_fresh_writer_reports_near_zero_lag_and_stays_quiet(tmp_path, capsys):
    handler = _file_handler(tmp_path)
    try:
        handler.emit(_record(0.0))
    finally:
        handler.close()
    assert hermes_logging.last_write_lag_seconds() < 5.0
    assert "behind" not in capsys.readouterr().err


def test_the_lag_warning_never_leaks_into_redirected_stderr(tmp_path, monkeypatch, capfd):
    """agy R2-4: the slash worker captures sys.stderr into the user's chat reply. The warning must
    go to the process's REAL stderr (sys.__stderr__), bypassing redirect_stderr."""
    import contextlib
    import io as _io

    monkeypatch.setattr(hermes_logging, "_LAG_WARNED_AT", 0.0)
    handler = _file_handler(tmp_path)
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            handler.emit(_record(300.0))
    finally:
        handler.close()
    assert "behind" not in buf.getvalue(), "the lag warning leaked into a captured stderr"
    assert "behind" in capfd.readouterr().err, "the lag warning never reached the real stderr"


def test_a_headless_process_with_no_stderr_streams_stays_silent(tmp_path, monkeypatch, capfd):
    """agy R3-4: with both stderr streams gone (daemon/GUI), print(file=None) would fall through
    to STDOUT — a protocol channel for stdio JSON-RPC. The warning must simply be skipped."""
    monkeypatch.setattr(hermes_logging, "_LAG_WARNED_AT", 0.0)
    monkeypatch.setattr(hermes_logging.sys, "__stderr__", None)
    monkeypatch.setattr(hermes_logging.sys, "stderr", None)
    handler = _file_handler(tmp_path)
    try:
        handler.emit(_record(300.0))
    finally:
        handler.close()
    captured = capfd.readouterr()
    assert "behind" not in captured.out, "the lag warning leaked onto stdout"
