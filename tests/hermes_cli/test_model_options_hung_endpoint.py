"""A hung current custom endpoint never holds the model picker; its probe finishes in the background.

Found by the shared-backend concurrency harness (2026-09-25): a normal picker open probes the CURRENT
custom endpoint live, and every window of a profile opens the picker together; with that endpoint hung
(a stalled local model server) model.options took ~12.5 s for everyone. Real payload build, real
discovery, against a loopback endpoint that stalls its /models reply.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    """A payload build starts background catalog refreshes (e.g. GitHub models): keep them off the network."""
    real = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if self.family in (socket.AF_INET, socket.AF_INET6) and not str(host).startswith(("127.", "::1")):
            raise ConnectionRefusedError(f"test: no network ({address!r})")
        return real(self, address)
    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture()
def stalled_endpoint():
    state = {"hits": 0, "release": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):
            state["hits"] += 1
            state["release"].wait(timeout=20)
            body = json.dumps({"data": [{"id": "live-model-x"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", state
    state["release"].set()
    server.shutdown()


def _models(payload: dict) -> set:
    return {m for row in payload["providers"] for m in (row.get("models") or [])}


def test_a_hung_current_endpoint_never_holds_the_picker(tmp_path, monkeypatch, stalled_endpoint):
    base_url, state = stalled_endpoint
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli.inventory import build_model_options_payload, load_picker_context

    # Warm the build's imports first (a running backend has them): only the endpoint wait is under test.
    (home / "config.yaml").write_text("model:\n  provider: openrouter\n  default: a/b\n", encoding="utf-8")
    build_model_options_payload(load_picker_context())
    (home / "config.yaml").write_text(
        f"model:\n  provider: custom\n  default: declared-model\n  base_url: {base_url}\n", encoding="utf-8")

    results, elapsed = [], []

    def open_picker():
        started = time.monotonic()
        results.append(build_model_options_payload(load_picker_context()))
        elapsed.append(time.monotonic() - started)

    windows = [threading.Thread(target=open_picker) for _ in range(5)]
    for t in windows:
        t.start()
    for t in windows:
        t.join(timeout=30)

    assert len(results) == 5
    # Five separate builds contend for the GIL here (production coalesces them); the budget bounds each.
    assert max(elapsed) < 4.0, f"the picker waited {max(elapsed):.1f}s on a hung endpoint"
    assert state["hits"] <= 2, f"{state['hits']} probes of one endpoint for 5 concurrent picker opens"

    # The endpoint recovers; the background probe completes and warms the cache for the next open.
    state["release"].set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "live-model-x" not in _models(build_model_options_payload(load_picker_context())):
        time.sleep(0.2)
    assert "live-model-x" in _models(build_model_options_payload(load_picker_context()))
