"""A saturated asyncio default executor never holds WebSocket RPC ingress.

Every WS frame used to reach ``server.dispatch`` through ``asyncio.to_thread``: the loop's DEFAULT
executor, shared with dashboard SQLite reads, skin resolution and disconnect teardown. Once that
pool was busy, requests from every window waited before they could reach a free lane, including
the interrupts and approval answers a user sends to unstick a turn. Drives the real ``/api/ws`` route
(its pre-``handle_ws`` prerequisites included) with only the auth gate stubbed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading

from tui_gateway import server
from tui_gateway import ws as ws_mod


def test_rpc_and_approval_answers_are_served_while_the_default_executor_is_saturated(monkeypatch):
    from hermes_cli import mcp_startup
    from hermes_cli.web_routers import chat_ws

    async def _allowed(_ws):
        return True

    monkeypatch.setattr(chat_ws, "_close_unless_sidecar_allowed", _allowed)
    monkeypatch.setattr(mcp_startup, "start_deferred_mcp_discovery_now", lambda: None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    monkeypatch.setattr(server, "resolve_skin", lambda: {})
    monkeypatch.setitem(server._methods, "probe.inline", lambda rid, params: {"jsonrpc": "2.0", "id": rid, "result": {"ok": True}})
    answered = threading.Event()
    monkeypatch.setattr(server, "_relay_compute_host_response", lambda req: answered.set() or True)

    release = threading.Event()
    sent: list[dict] = []
    inbound = [
        {"jsonrpc": "2.0", "id": "inline-1", "method": "probe.inline", "params": {}},
        # The renderer answering an approval the agent asked for (a server->client request response).
        {"jsonrpc": "2.0", "id": "srq-unknown", "result": {"choice": "once"}},
        {"jsonrpc": "2.0", "id": "inline-2", "method": "probe.inline", "params": {}},
    ]

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            if inbound:
                return json.dumps(inbound.pop(0))
            await hang_up.wait()
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    served_while_saturated: dict = {}

    async def scenario():
        loop = asyncio.get_running_loop()
        saturated = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        loop.set_default_executor(saturated)
        blockers = [loop.run_in_executor(None, release.wait, 30) for _ in range(4)]
        # The real /api/ws route: its connection prerequisites run before handle_ws.
        session = asyncio.create_task(chat_ws.gateway_ws(FakeWS()))
        deadline = loop.time() + 5
        while loop.time() < deadline and not ({"inline-1", "inline-2"} <= _replies() and answered.is_set()):
            await asyncio.sleep(0.05)
        served_while_saturated.update(replies=_replies(), answered=answered.is_set(),
                                      ready=any(f.get("params", {}).get("type") == "gateway.ready" for f in sent))
        # Disconnect teardown deliberately stays on the default executor; free it and hang up.
        release.set()
        hang_up.set()
        await asyncio.gather(*blockers)
        await asyncio.wait_for(session, timeout=10)

    def _replies() -> set:
        return {frame.get("id") for frame in sent if "result" in frame}

    hang_up = None

    async def main():
        nonlocal hang_up
        hang_up = asyncio.Event()
        await scenario()

    asyncio.run(main())
    replies = served_while_saturated["replies"]

    assert served_while_saturated["ready"], "handshake held by the default executor"
    assert {"inline-1", "inline-2"} <= replies, "RPC ingress waited behind a saturated default executor"
    assert served_while_saturated["answered"], "the approval answer never reached dispatch"
