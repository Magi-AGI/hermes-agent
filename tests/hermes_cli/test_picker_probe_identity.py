"""A current-endpoint picker probe is shared only by callers that would have made the SAME request.

T1 round 3 (Codex): the single-flight key for the bounded current-endpoint probe held the URL, the
explicit credential and headers, but not the owning profile or the credentials each profile resolves
implicitly; two profiles pointing at one endpoint got the first profile's private catalog. And a
command credential was keyed by object id, so every picker build (a fresh CommandTokenSource each
time) started its own probe. Real payload builds under two real profile homes.
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
    real = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if self.family in (socket.AF_INET, socket.AF_INET6) and host not in ("127.0.0.1", "::1"):
            raise ConnectionRefusedError(f"test: no network ({address!r})")
        return real(self, address)
    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture()
def private_catalogs():
    """One endpoint; each profile's credential sees only its own private model. Profile A's request is
    held until released, so B asks while A's probe is in flight."""
    state = {"a_in_flight": threading.Event(), "release": threading.Event(), "auth": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            return

        def do_GET(self):
            credential = self.headers.get("Authorization") or ""
            state["auth"].append(credential)
            if credential == "Bearer fixture-A":
                state["a_in_flight"].set()
                state["release"].wait(timeout=10)
            owner = "A" if credential == "Bearer fixture-A" else "B" if credential == "Bearer fixture-B" else "anon"
            body = json.dumps({"models": [{"name": f"{owner}-private-model"}]}).encode()
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


def _profile(root, name: str, base_url: str):
    home = root / f"profile-{name}"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({
        "model": {"provider": "custom", "base_url": base_url},
        "providers": {"ollama": {"base_url": base_url, "key_env": "OLLAMA_T1_KEY"}}}), encoding="utf-8")
    (home / ".env").write_text(f"OLLAMA_T1_KEY=fixture-{name}\n", encoding="utf-8")
    return home


def test_two_profiles_on_one_endpoint_never_share_a_probe(tmp_path, monkeypatch, private_catalogs):
    """Concurrent A/B, then A -> B -> A, through the discovery the picker rows use, with each profile's
    home, secret and terminal scopes bound as a multiplexed backend binds them."""
    import contextlib

    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_multiplex_active, set_secret_scope
    from hermes_cli import model_switch_providers as providers
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope

    url, state = private_catalogs
    homes = {n: _profile(tmp_path, n, url) for n in ("A", "B")}
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    (tmp_path / "launch").mkdir()

    @contextlib.contextmanager
    def scope(name):
        home = set_hermes_home_override(homes[name])
        secret = set_secret_scope(build_profile_secret_scope(homes[name]))
        terminal = install_profile_terminal_scope(homes[name])
        try:
            yield
        finally:
            reset_terminal_scope(terminal)
            reset_secret_scope(secret)
            reset_hermes_home_override(home)

    results: dict = {}

    def discover(name, label):
        with scope(name):
            results[label] = providers._discover_endpoint_models(
                "", url, "custom", False, headers=None, api_mode=None, probe_live=True,
                discovery_allowed=True, for_picker=False, foreground_budget_s=4)[0]

    set_multiplex_active(True)
    try:
        a = threading.Thread(target=discover, args=("A", "A-concurrent"))
        b = threading.Thread(target=discover, args=("B", "B-concurrent"))
        a.start()
        assert state["a_in_flight"].wait(5)
        b.start()
        time.sleep(0.2)
        state["release"].set()
        a.join(10)
        b.join(10)
        discover("B", "B-alone")
        discover("A", "A-again")
    finally:
        set_multiplex_active(False)

    assert "A-private-model" not in (results.get("B-concurrent") or []), "B joined A's in-flight probe"
    assert "B-private-model" in (results.get("B-concurrent") or [])
    assert "B-private-model" in (results.get("B-alone") or [])
    assert "A-private-model" in (results.get("A-again") or [])


def test_a_command_credential_keeps_one_probe_identity_across_picker_builds(tmp_path, monkeypatch):
    from agent.command_token_source import CommandTokenSource
    from hermes_cli.model_switch_providers import _probe_identity

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Kept alive together, so their object ids are distinct (as for sequential picker builds).
    sources = [CommandTokenSource("echo token"), CommandTokenSource("echo token"), CommandTokenSource("echo other")]
    first, second, other = (_probe_identity(src, "http://127.0.0.1:9/v1", "custom", False, None, None) for src in sources)
    assert first == second, "each picker build's fresh CommandTokenSource started its own probe"
    assert first != other
