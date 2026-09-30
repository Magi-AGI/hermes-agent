"""``mcp.interactive_oauth: false`` and the model-started ``manage_connections`` MCP flows.

Before this contract, a model's ``manage_connections(action="authorize")`` started the OAuth flow in
the card's *prepare* step, before the card was even drawn: ``mcp_oauth.start`` bound a loopback
callback listener and ran the worker under ``force_interactive_oauth()`` (forced beats the key), so
the key never applied. On the desktop the tool call then blocked the turn until the card settled.

Contract under test, key false:
- a model-started ``authorize`` starts no OAuth work at all: no worker, no listener, no card; the
  call returns at once with the actionable ``hermes mcp login`` / Login-button error;
- off the desktop the same, and a model-started OAuth ``install`` is refused the same way;
- a card ``install`` of an OAuth entry is click-gated: OAuth starts only on the user's approval;
- the key absent: the card still starts OAuth in prepare (upstream behaviour, pinned here).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.connectors import live
from tools.connectors.contract import TargetState
from tools.connectors.mcp import apply_answer
from tools.connectors.tool import manage_connections

SERVER = "cardy"
SERVER_CFG = {"url": "https://mcp.example.invalid/mcp", "auth": "oauth"}
PROMPT_RETURN_SECONDS = 5.0


def _write_config(text: str) -> None:
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_live():
    live.reset_for_tests()
    yield
    live.reset_for_tests()


@pytest.fixture()
def oauth_world(monkeypatch):
    """The real card backend and the real ``mcp_oauth.start``/``run_worker``; only the SDK probe is
    faked (no network). It behaves like the SDK: publish the URL, then wait for the callback."""
    import hermes_cli.mcp_config as mcp_config
    from tools.connectors import mcp as connectors_mcp
    from tools.connectors import mcp_oauth

    seen = {"listeners": [], "workers": [], "forced_interactive": [], "webbrowser": []}
    monkeypatch.setattr(connectors_mcp, "_catalog_names", lambda: [])
    monkeypatch.setattr(connectors_mcp, "_configured_names", lambda: [SERVER])
    monkeypatch.setattr(connectors_mcp, "_catalog_instructions", lambda name: "")
    monkeypatch.setattr(mcp_config, "_get_mcp_servers", lambda: {SERVER: dict(SERVER_CFG)})
    monkeypatch.setattr(mcp_oauth, "_reuse_saved_authorization", lambda *a, **k: False)
    monkeypatch.setattr("webbrowser.open", lambda *a, **k: seen["webbrowser"].append(a) or True)

    real_receiver = mcp_oauth._start_loopback_receiver

    def receiver(flow):
        httpd = real_receiver(flow)
        seen["listeners"].append(httpd.server_address)
        return httpd

    monkeypatch.setattr(mcp_oauth, "_start_loopback_receiver", receiver)

    real_worker = mcp_oauth.run_worker

    def worker(*args, **kwargs):
        seen["workers"].append(args[1])
        return real_worker(*args, **kwargs)

    monkeypatch.setattr(mcp_oauth, "run_worker", worker)

    def fake_probe(server_name, cfg, hermes_home, flow, reconnect_live, *, on_commit=None):
        import asyncio

        from tools.mcp_oauth import _active_dashboard_flow, _is_interactive

        seen["forced_interactive"].append(_is_interactive())
        dashboard = _active_dashboard_flow()
        asyncio.run(dashboard.publish_authorization_url(
            "https://idp.example.invalid/authorize?client_id=x&state=card-state"))
        asyncio.run(dashboard.wait_for_callback(timeout=2))

    monkeypatch.setattr(mcp_oauth, "probe_with_rollback", fake_probe)
    return seen


def _call_in_thread(args, *, session_id, callback):
    box = {}

    def run():
        with patch("tools.connectors.run.WATCH_INTERVAL_SECONDS", 0.05):
            box["out"] = manage_connections(args, connection_callback=callback, session_id=session_id)

    thread = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(PROMPT_RETURN_SECONDS)
    return thread, box, time.monotonic() - started


def _settle_open(session_id):
    operation = live.current(session_id)
    if operation is not None:
        apply_answer(operation, json.dumps({"settled_by": "continue"}))


def _recording_card():
    seen = []

    def callback(payload):
        seen.append(payload)
        return None

    callback.seen = seen
    return callback


AUTHORIZE = {"action": "authorize", "connectors": [{"name": SERVER, "mcp": True}]}


# --- key false: a model-started authorize starts nothing and never blocks the turn -------------

def test_key_off_desktop_authorize_starts_no_oauth_and_returns_at_once(oauth_world):
    _write_config("mcp:\n  interactive_oauth: false\n")
    card = _recording_card()
    thread, box, elapsed = _call_in_thread(AUTHORIZE, session_id="card-off", callback=card)
    try:
        assert not thread.is_alive(), "the model turn is blocked on the card / OAuth callback"
        assert elapsed < PROMPT_RETURN_SECONDS
        out = json.loads(box["out"])
        assert "hermes mcp login" in out["error"] and "Login" in out["error"]
        assert oauth_world["listeners"] == [], "a loopback listener was bound before any click"
        assert oauth_world["workers"] == [], "an OAuth worker started before any click"
        assert oauth_world["webbrowser"] == []
        assert card.seen == [], "no card is drawn for a refused authorization"
        assert live.current("card-off") is None
    finally:
        _settle_open("card-off")
        thread.join(PROMPT_RETURN_SECONDS)


def test_key_off_off_desktop_authorize_starts_no_oauth_and_returns_at_once(oauth_world):
    _write_config("mcp:\n  interactive_oauth: false\n")
    thread, box, elapsed = _call_in_thread(AUTHORIZE, session_id="plain-off", callback=None)
    assert not thread.is_alive()
    out = json.loads(box["out"])
    assert "hermes mcp login" in out["error"]
    assert "connect_url" not in json.dumps(out)
    assert oauth_world["listeners"] == [] and oauth_world["workers"] == []
    assert oauth_world["webbrowser"] == []


def test_key_off_unreadable_config_also_refuses(oauth_world, monkeypatch):
    """A config that cannot be read fails closed here too (same predicate as ``_is_interactive``)."""
    import hermes_cli.config as config_mod

    def boom(*_a, **_k):
        raise OSError("config unreadable")

    monkeypatch.setattr(config_mod, "read_config_value_fresh", boom)
    thread, box, _ = _call_in_thread(AUTHORIZE, session_id="broken-off", callback=None)
    assert not thread.is_alive()
    assert "hermes mcp login" in json.loads(box["out"])["error"]
    assert oauth_world["listeners"] == [] and oauth_world["workers"] == []


# --- key absent: upstream card behaviour, pinned (OAuth starts in prepare, before any click) ---

def test_key_absent_card_starts_oauth_in_prepare_before_any_click(oauth_world):
    card = _recording_card()
    thread, box, _ = _call_in_thread(AUTHORIZE, session_id="card-on", callback=card)
    try:
        # The card was drawn and nobody clicked, yet the flow is already live behind it.
        assert card.seen, "the card was never drawn"
        (row,) = card.seen[0]["targets"]
        assert row["state"] == TargetState.initiated.value
        assert oauth_world["listeners"], "upstream binds the loopback listener in prepare"
        assert oauth_world["workers"] == [SERVER]
        assert oauth_world["forced_interactive"] == [True]
        assert oauth_world["webbrowser"] == [], "the card path publishes the link; it opens nothing"
        assert thread.is_alive(), "upstream: the tool call waits for the card to settle"
    finally:
        _settle_open("card-on")
        thread.join(PROMPT_RETURN_SECONDS)
    assert not thread.is_alive()
    assert json.loads(box["out"])["status"] == "settled"


# --- install: the card path is click-gated; the no-card path is refused under key false ---------

class _OAuthInstallBackend:
    def __init__(self):
        self.calls = []

    def required_env(self, name):
        return []

    def installs_with_oauth(self, name):
        return True

    def start_install_oauth(self, name, env):
        self.calls.append(("start_install_oauth", name))

        class _Attempt:
            auth_url = f"https://idp.example.invalid/{name}"
            detail = ""

            def poll(self):
                return {"status": "pending", "error": "", "tools": [], "discovery_error": ""}

        return _Attempt()


@pytest.fixture()
def install_catalog(monkeypatch):
    from tools.connectors import mcp as connectors_mcp

    monkeypatch.setattr(connectors_mcp, "_catalog_names", lambda: ["figma"])
    monkeypatch.setattr(connectors_mcp, "_configured_names", lambda: [])
    monkeypatch.setattr(connectors_mcp, "_catalog_instructions", lambda name: "")


INSTALL = {"action": "install", "connectors": [{"name": "figma", "mcp": True}]}


def test_key_off_card_install_starts_oauth_only_on_the_users_click(install_catalog):
    _write_config("mcp:\n  interactive_oauth: false\n")
    backend = _OAuthInstallBackend()
    clicked = threading.Event()

    def card(payload):
        assert backend.calls == [], "OAuth started before the user clicked"
        clicked.set()

        def click():
            operation = live.get("card-install", payload["op_id"])
            apply_answer(operation, json.dumps({"targets": [{"name": "figma", "status": "approved"}]}))
            apply_answer(operation, json.dumps({"settled_by": "continue"}))

        threading.Timer(0.05, click).start()

    with patch("tools.connectors.run.WATCH_INTERVAL_SECONDS", 0.05):
        manage_connections(INSTALL, connection_callback=card, session_id="card-install", mcp_backend=backend)
    assert clicked.is_set()
    assert backend.calls == [("start_install_oauth", "figma")]


def test_key_off_off_desktop_oauth_install_is_refused_at_once(install_catalog):
    _write_config("mcp:\n  interactive_oauth: false\n")
    backend = _OAuthInstallBackend()
    started = time.monotonic()
    out = json.loads(manage_connections(INSTALL, session_id="plain-install", mcp_backend=backend))
    assert time.monotonic() - started < PROMPT_RETURN_SECONDS
    assert backend.calls == []
    (row,) = out["targets"]
    assert row["state"] == TargetState.failed.value
    assert "hermes mcp login" in row["detail"]
    assert not row.get("connect_url")
