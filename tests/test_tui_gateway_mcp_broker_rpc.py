import json

from tui_gateway import server


def test_mcp_catalog_rpc_requires_same_profile(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")

    resp = server._methods["mcp.catalog"]("r1", {"profile": "default"})

    assert resp["error"]["code"] == 4031
    assert "profile" in resp["error"]["message"]


def test_mcp_catalog_rpc_returns_helper_catalog_for_same_profile(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")
    monkeypatch.setattr(
        "tools.mcp_tool.export_mcp_broker_catalog",
        lambda origin_profile=None: {"profile": origin_profile, "tools": [{"name": "mcp_demo_ping"}]},
    )

    resp = server._methods["mcp.catalog"]("r2", {"profile": "claudetriad"})

    assert resp == {
        "jsonrpc": "2.0",
        "id": "r2",
        "result": {"profile": "claudetriad", "tools": [{"name": "mcp_demo_ping"}]},
    }


def test_mcp_status_rpc_returns_helper_status_for_same_profile(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "default")
    monkeypatch.setattr(
        "tools.mcp_tool.export_mcp_broker_status",
        lambda origin_profile=None: {"profile": origin_profile, "servers": []},
    )

    resp = server._methods["mcp.status"]("r3", {"profile": "default"})

    assert resp["result"] == {"profile": "default", "servers": []}


def test_mcp_call_rpc_forwards_call_metadata(monkeypatch):
    captured = {}
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")

    def fake_call(tool_name, arguments, **kwargs):
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return {"call_id": kwargs.get("call_id"), "result": "ok", "queue_wait_ms": 0}

    monkeypatch.setattr("tools.mcp_tool.call_mcp_broker_tool", fake_call)

    resp = server._methods["mcp.call"](
        "r4",
        {
            "profile": "claudetriad",
            "session_id": "sid-1",
            "call_id": "call-1",
            "tool": "mcp_magi_archive_search_cards",
            "arguments": {"query": "butterfly"},
            "timeout": 12,
        },
    )

    assert resp["result"] == {"call_id": "call-1", "result": "ok", "queue_wait_ms": 0}
    assert captured == {
        "tool_name": "mcp_magi_archive_search_cards",
        "arguments": {"query": "butterfly"},
        "kwargs": {
            "origin_profile": "claudetriad",
            "origin_session_id": "sid-1",
            "call_id": "call-1",
            "timeout": 12.0,
        },
    }


def test_mcp_call_rpc_rejects_invalid_tool_name(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "default")

    resp = server._methods["mcp.call"]("r5", {"profile": "default", "tool": "terminal"})

    assert resp["error"]["code"] == -32602
    assert "MCP tool" in resp["error"]["message"]


def test_mcp_status_and_call_reject_cross_profile(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")

    status = server._methods["mcp.status"]("s1", {"profile": "wikiadmin"})
    call = server._methods["mcp.call"](
        "s2", {"profile": "wikiadmin", "tool": "mcp_magi_archive_get_card", "arguments": {}}
    )

    assert status["error"]["code"] == 4031
    assert call["error"]["code"] == 4031


def test_broker_token_required_when_configured(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")
    monkeypatch.setenv("HERMES_MCP_BROKER_TOKEN", "s3cr3t-broker-token")
    called = {"n": 0}
    monkeypatch.setattr(
        "tools.mcp_tool.call_mcp_broker_tool",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"result": "ok", "queue_wait_ms": 0},
    )

    base = {"profile": "claudetriad", "tool": "mcp_magi_archive_get_card", "arguments": {}}

    missing = server._methods["mcp.call"]("t1", dict(base))
    wrong = server._methods["mcp.call"]("t2", {**base, "broker_token": "nope"})

    assert missing["error"]["code"] == 4033
    assert wrong["error"]["code"] == 4033
    # Rejected BEFORE the tool ever executes.
    assert called["n"] == 0
    # The expected token is never echoed back in the rejection.
    assert "s3cr3t-broker-token" not in json.dumps(missing)
    assert "s3cr3t-broker-token" not in json.dumps(wrong)


def test_broker_token_permits_matching_token_and_is_not_echoed(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")
    monkeypatch.setenv("HERMES_MCP_BROKER_TOKEN", "s3cr3t-broker-token")
    monkeypatch.setattr(
        "tools.mcp_tool.call_mcp_broker_tool",
        lambda tool_name, arguments, **kwargs: {"result": "ok", "queue_wait_ms": 0},
    )

    resp = server._methods["mcp.call"](
        "t3",
        {
            "profile": "claudetriad",
            "broker_token": "s3cr3t-broker-token",
            "tool": "mcp_magi_archive_get_card",
            "arguments": {"id": "1"},
        },
    )

    assert resp["result"] == {"result": "ok", "queue_wait_ms": 0}
    # The token must never leak into the response body.
    assert "s3cr3t-broker-token" not in json.dumps(resp)


def test_broker_token_ignored_when_not_configured(monkeypatch):
    monkeypatch.setenv("HERMES_DESKTOP_BACKEND_PROFILE", "claudetriad")
    monkeypatch.delenv("HERMES_MCP_BROKER_TOKEN", raising=False)
    monkeypatch.setattr(
        "tools.mcp_tool.export_mcp_broker_status",
        lambda origin_profile=None: {"profile": origin_profile, "servers": []},
    )

    # No token configured → same-profile call succeeds without any broker_token.
    resp = server._methods["mcp.status"]("t4", {"profile": "claudetriad"})

    assert resp["result"] == {"profile": "claudetriad", "servers": []}
