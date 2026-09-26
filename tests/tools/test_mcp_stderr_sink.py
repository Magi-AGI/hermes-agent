"""stdio MCP stderr is bounded (gate item G9: a Unity bridge retrying ``connect ENOENT`` grew
mcp-stderr.log to 101 MB; children held the file, so it could never be rotated on Windows)."""

import subprocess
import sys
import textwrap

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
    assert not sink.thread.is_alive()


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
