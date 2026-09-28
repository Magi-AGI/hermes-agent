"""The compute host's fallback session build works.

When ``_init_session``'s side machinery is unavailable, ``compute_host`` builds a minimal
host-owned session instead of failing after the expensive agent build. That fallback called
``server._sanitize_client_source``, which does not exist, so the fallback itself raised
AttributeError and the turn failed anyway (still present on upstream/main 2026-09-23).
"""

from __future__ import annotations

import io

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tests.tui_gateway.test_compute_host_borrowed_lease import (  # noqa: F401  (fixture)
    _make_frame, isolated_env)


def test_the_fallback_session_build_does_not_crash(isolated_env, monkeypatch):
    def _broken_init_session(*_a, **_k):
        raise RuntimeError("slash worker unavailable")

    monkeypatch.setattr(server, "_init_session", _broken_init_session)
    server._sessions.pop("s1", None)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        session = host._build_server_session(server, _make_frame("s1", history_version=4), "s1")
    finally:
        host.close()

    assert session is server._sessions["s1"]
    assert session["source"], "the fallback resolves a session source"
    assert int(session["history_version"]) >= 4


def test_a_late_setup_failure_keeps_the_registered_session(isolated_env, monkeypatch):
    """_init_session registers the full record before its side steps; one of those failing must not
    swap in the minimal record (which would orphan whatever the step had already started)."""
    registered = {}

    def _init_then_fail(sid, key, agent, history, **_k):
        registered["record"] = server._sessions[sid] = {
            "agent": agent, "session_key": key, "history": list(history),
            "history_lock": __import__("threading").Lock(), "history_version": 0, "_notif_stop": object()}
        raise RuntimeError("notification poller failed to start")

    monkeypatch.setattr(server, "_init_session", _init_then_fail)
    server._sessions.pop("s1", None)
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        session = host._build_server_session(server, _make_frame("s1", history_version=4), "s1")
    finally:
        host.close()

    assert session is registered["record"] is server._sessions["s1"]
    assert int(session["history_version"]) >= 4


def test_a_real_agent_turn_through_the_fallback_session_completes(monkeypatch, tmp_path):
    """Deploy gate A6: a turn through the fallback session was once seen to stall after message.start.
    With a real AIAgent streaming from a loopback model server it completes (deltas, message.complete,
    turn.end); kept as the regression guard for that report."""
    import json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import run_agent

    class _Model(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for delta, finish in (({"content": "hel"}, None), ({"content": "lo"}, None), ({}, "stop")):
                chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    model = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
    model.daemon_threads = True
    threading.Thread(target=lambda: model.serve_forever(poll_interval=0.05), daemon=True).start()
    base_url = f"http://127.0.0.1:{model.server_address[1]}/v1"
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, **_kw: run_agent.AIAgent(
        api_key="test-key", base_url=base_url, model="m", provider="custom", quiet_mode=True,
        skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=2, session_id=key))
    monkeypatch.setattr(server, "_init_session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    server._sessions.pop("s-real", None)
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        host.handle_frame(_make_frame("s-real", session_key="real-key", cwd=str(tmp_path)))
        deadline = time.monotonic() + 60
        frames: list = []
        while time.monotonic() < deadline and not any(f["type"] in ("turn.end", "turn.error") for f in frames):
            time.sleep(0.1)
            frames = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    finally:
        host.close()
        server._sessions.pop("s-real", None)
        model.shutdown()
    events = {(f["message"].get("params") or {}).get("type"): (f["message"].get("params") or {}).get("payload")
              for f in frames if f["type"] == "rpc"}
    assert frames and frames[-1]["type"] == "turn.end", [f["type"] for f in frames]
    assert events["message.complete"]["text"] == "hello"
