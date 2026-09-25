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
