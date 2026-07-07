"""R5: the /api/ws JSON-RPC transport must reject an unauthenticated upgrade
BEFORE any request reaches the dispatcher, so mcp.call (and every other RPC) is
unreachable without a valid dashboard session credential."""

import asyncio

import hermes_cli.web_server as web_server


class FakeWebSocket:
    """Minimal WebSocket double recording accept()/close() without a real peer."""

    def __init__(self):
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed_code = code


def test_gateway_ws_rejects_unauthenticated_before_dispatch(monkeypatch):
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    # Simulate an unauthenticated upgrade.
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda ws: False)

    # If dispatch were reachable, gateway_ws would `from tui_gateway.ws import
    # handle_ws` and call it. Poison that import path so any attempt to proceed
    # past the auth gate fails loudly instead of silently passing.
    import tui_gateway.ws as ws_mod

    def _forbidden(*_a, **_k):
        raise AssertionError("handle_ws reached despite failed auth")

    monkeypatch.setattr(ws_mod, "handle_ws", _forbidden)

    ws = FakeWebSocket()
    asyncio.run(web_server.gateway_ws(ws))

    # Closed with the WS auth-failure code, never accepted, dispatcher untouched.
    assert ws.closed_code == 4401
    assert ws.accepted is False


def test_gateway_ws_reaches_handler_only_after_auth_and_boundary_checks(monkeypatch):
    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(web_server, "_ws_auth_ok", lambda ws: True)
    monkeypatch.setattr(web_server, "_ws_request_is_allowed", lambda ws: True)

    reached = {"handle": False}

    async def _fake_handle_ws(ws):
        reached["handle"] = True

    import tui_gateway.ws as ws_mod

    monkeypatch.setattr(ws_mod, "handle_ws", _fake_handle_ws)

    ws = FakeWebSocket()
    asyncio.run(web_server.gateway_ws(ws))

    # Auth + boundary checks passed → the JSON-RPC handler runs (and only then).
    assert reached["handle"] is True
    assert ws.closed_code is None
