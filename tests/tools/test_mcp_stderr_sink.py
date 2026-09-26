"""stdio MCP stderr is bounded (gate item G9: a Unity bridge retrying ``connect ENOENT`` grew
mcp-stderr.log to 101 MB; children held the file, so it could never be rotated on Windows)."""

import subprocess
import sys
import textwrap

import pytest

from tools import mcp_tool_stderr

_FLOOD = """
import sys
for _ in range(200000):
    sys.stderr.write("connect ENOENT pipe" + chr(10))
"""

_STEADY = """
import sys, time
for _ in range(40):
    sys.stderr.write("x" * 1000 + chr(10))
    sys.stderr.flush()
    time.sleep(0.01)
"""


def _run_child(sink, code: str, tmp_path) -> None:
    script = tmp_path / f"{sink.server_name}_child.py"
    script.write_text(textwrap.dedent(code), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], stderr=sink.writer, stdin=subprocess.DEVNULL)
    sink.release_writer()
    proc.wait(timeout=60)
    sink.thread.join(timeout=30)
    sink.writer_thread.join(timeout=30)  # the log is complete only once the writer has finished
    assert not sink.thread.is_alive() and not sink.writer_thread.is_alive()


def test_a_flooding_server_is_rate_capped_and_the_loss_is_reported(tmp_path):
    sink = mcp_tool_stderr.open_stderr_sink("unity", home=tmp_path)
    _run_child(sink, _FLOOD, tmp_path)  # ~4 MB
    text = (tmp_path / "logs" / "mcp-stderr.log").read_text(encoding="utf-8")
    assert len(text) < 1024 * 1024
    assert "[unity] connect ENOENT pipe" in text
    assert "bytes of stderr dropped" in text


def test_the_log_rotates_by_size_while_a_server_is_running(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_tool_stderr, "_MAX_BYTES", 4096)
    monkeypatch.setattr(mcp_tool_stderr, "_RATE_BYTES_PER_S", 10 ** 9)
    sink = mcp_tool_stderr.open_stderr_sink("chatty", home=tmp_path)
    # The child keeps its stderr open for the whole run; rotation must not wait for it to close.
    _run_child(sink, _STEADY, tmp_path)
    logs = tmp_path / "logs"
    assert (logs / "mcp-stderr.log.1").exists()
    assert (logs / "mcp-stderr.log").stat().st_size <= 4096 + 2048
    assert not (logs / f"mcp-stderr.log.{mcp_tool_stderr._BACKUPS + 1}").exists()


_MEGABYTE = """
import sys
sys.stderr.write("x" * (1024 * 1024))
sys.stderr.flush()
"""


def test_a_stalled_log_write_never_backs_the_pipe_up_into_the_server(tmp_path):
    """The drain only reads; disk I/O happens elsewhere. Holding the log (another sink's append, a
    slow filesystem) used to stop the reads, fill the pipe and block the server's stderr write."""
    sink = mcp_tool_stderr.open_stderr_sink("stalled", home=tmp_path)
    script = tmp_path / "megabyte.py"
    script.write_text(textwrap.dedent(_MEGABYTE), encoding="utf-8")
    with mcp_tool_stderr._append_lock:
        proc = subprocess.Popen([sys.executable, str(script)], stderr=sink.writer, stdin=subprocess.DEVNULL)
        sink.release_writer()
        try:
            proc.wait(timeout=20)
        finally:
            proc.kill()
    sink.thread.join(timeout=30)
    sink.close()


def test_profile_shutdown_waits_for_an_in_flight_append_before_the_directory_moves(tmp_path, monkeypatch):
    """Profile rename runs shutdown and renames at once; a sink still inside its final append held
    the log open and the rename failed on Windows."""
    import os
    import threading

    from hermes_constants import hermes_home_key
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers

    home = tmp_path / "profiles" / "moving"
    (home / "logs").mkdir(parents=True)
    in_append, release = threading.Event(), threading.Event()
    real_append = mcp_tool_stderr.append_to_log

    def slow_append(path, text):
        if "last words" not in text:
            return real_append(path, text)
        with open(path, "a", encoding="utf-8") as fh:  # the handle a rename trips over
            in_append.set()
            release.wait(10)
            fh.write(text)

    monkeypatch.setattr(mcp_tool_stderr, "append_to_log", slow_append)
    sink = mcp_tool_stderr.open_stderr_sink("closing", home=home)
    sink.writer.write("last words\n")
    sink.release_writer()
    assert in_append.wait(10)
    threading.Timer(0.5, release.set).start()
    shutdown_mcp_servers(scope=hermes_home_key(home))
    os.rename(home, tmp_path / "profiles" / "moved")  # raised PermissionError while the append was open
    assert "last words" in (tmp_path / "profiles" / "moved" / "logs" / "mcp-stderr.log").read_text(encoding="utf-8")


_TRICKLE = """
import sys, time
for i in range(40):
    sys.stderr.write("line %d" % i + chr(10))
    sys.stderr.flush()
    time.sleep(0.01)
"""


def test_end_of_stream_is_seen_even_when_the_queue_is_full(tmp_path, monkeypatch):
    """Round 2 (Codex): EOF was a queued sentinel, dropped when the queue was full, and the writer then
    waited forever with the sink still registered."""
    import threading

    from hermes_constants import hermes_home_key

    monkeypatch.setattr(mcp_tool_stderr, "_QUEUE_MAX", 4)
    release = threading.Event()
    real_append = mcp_tool_stderr.append_to_log

    def held_append(path, text):
        release.wait(20)  # the disk stalls until every line has been read and the queue is full
        real_append(path, text)

    monkeypatch.setattr(mcp_tool_stderr, "append_to_log", held_append)
    sink = mcp_tool_stderr.open_stderr_sink("trickle", home=tmp_path)
    script = tmp_path / "trickle.py"
    script.write_text(textwrap.dedent(_TRICKLE), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], stderr=sink.writer, stdin=subprocess.DEVNULL)
    sink.release_writer()
    proc.wait(timeout=30)
    sink.thread.join(timeout=30)
    assert not sink.thread.is_alive() and sink._queue.full()
    release.set()
    sink.writer_thread.join(timeout=10)
    assert not sink.writer_thread.is_alive()
    assert mcp_tool_stderr.sinks_released(hermes_home_key(tmp_path))


def test_a_profile_rename_refuses_while_a_stderr_append_still_holds_the_log(tmp_path, monkeypatch):
    """Round 2 (Codex): past the close timeout, shutdown returned with the append still open and the
    rename failed with WinError 5 midway. The rename now refuses cleanly (tombstone rolled back) and
    succeeds once the append is done."""
    import threading
    from pathlib import Path

    from hermes_cli import profiles
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_cleanup_gateway_service", lambda *_: None)
    monkeypatch.setattr(profiles, "_notify_multiplexer", lambda *_: None)
    monkeypatch.setattr(profiles, "_live_default_multiplexer", lambda: False)
    monkeypatch.setattr(mcp_tool_stderr, "_CLOSE_JOIN_S", 0.3)
    old_dir = profiles.create_profile("mcp-slow-log", no_alias=True)
    in_append, release = threading.Event(), threading.Event()

    def stuck_append(path, text):
        if "last words" not in text:
            return
        with open(path, "a", encoding="utf-8") as fh:
            in_append.set()
            release.wait(30)
            fh.write(text)

    monkeypatch.setattr(mcp_tool_stderr, "append_to_log", stuck_append)
    token = set_hermes_home_override(old_dir)
    try:
        sink = mcp_tool_stderr.open_stderr_sink("slow")
    finally:
        reset_hermes_home_override(token)
    sink.writer.write("last words" + chr(10))
    sink.release_writer()
    assert in_append.wait(10)
    try:
        with pytest.raises(RuntimeError, match="still being written"):
            profiles.rename_profile("mcp-slow-log", "mcp-slow-log-2")
        assert old_dir.is_dir()
    finally:
        release.set()
    sink.writer_thread.join(timeout=10)
    new_dir = profiles.rename_profile("mcp-slow-log", "mcp-slow-log-2")
    assert new_dir.is_dir() and not old_dir.exists()
