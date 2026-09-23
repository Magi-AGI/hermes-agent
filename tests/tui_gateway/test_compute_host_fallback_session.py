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
