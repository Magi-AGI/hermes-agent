"""Native server-side compaction obeys the subscription-only compaction route policy.

Review (Codex, sync A3 finding 5): with ``compression.codex_responses_native`` on, a gpt-5.6 request
to api.openai.com carried ``context_management`` asking the provider to compact the conversation
server-side. That path never entered the auxiliary route guard, so compaction ran on a metered
route. Native compaction is now requested only on the ChatGPT Codex OAuth backend, and only while
that route is allowed; otherwise nothing is sent and the local compressor compacts through the
guarded path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import native_compaction as nc

CODEX_URL = "https://chatgpt.com/backend-api/codex"


def _agent(base_url, *, provider="openai-codex", capabilities=None):
    return SimpleNamespace(
        model="gpt-5.6", provider=provider, base_url=base_url, runtime_capabilities=None,
        codex_responses_native_compaction=True, compression_enabled=True,
        compression_checkpoint_required=False, capabilities=capabilities or {},
        context_compressor=SimpleNamespace(threshold_tokens=300_000),
        codex_responses_compact_threshold=None,
    )


@pytest.fixture(autouse=True)
def _default_allowed_routes(monkeypatch):
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "_allowed_compression_routes",
                        lambda: frozenset(ac.DEFAULT_ALLOWED_COMPRESSION_ROUTES))


def test_codex_oauth_backend_keeps_native_compaction():
    payload = nc.native_compaction_context_management(_agent(CODEX_URL), is_codex_backend=True)
    assert payload and payload[0]["type"] == "compaction"


@pytest.mark.parametrize("agent_kwargs", [
    {"base_url": "https://api.openai.com/v1", "provider": "openai"},                  # metered API key
    {"base_url": "https://proxy.example/v1", "provider": "custom",
     "capabilities": {"openai_native_compaction": True}},                             # trusted proxy
])
def test_metered_or_unprovable_routes_get_no_native_compaction(agent_kwargs):
    agent = _agent(**agent_kwargs)
    assert nc.native_compaction_context_management(agent, is_codex_backend=False) is None


def test_codex_route_narrowed_out_of_allowed_routes_is_refused(monkeypatch):
    import agent.auxiliary_client as ac
    monkeypatch.setattr(ac, "_allowed_compression_routes",
                        lambda: frozenset({("anthropic", ac.AUTH_MODE_OAUTH_SUBSCRIPTION)}))
    assert nc.native_compaction_context_management(_agent(CODEX_URL), is_codex_backend=True) is None


def test_a_broken_allowed_routes_config_fails_closed(monkeypatch):
    import agent.auxiliary_client as ac

    def _broken():
        raise ac.CompressionRoutingRejected("malformed allowed_routes")

    monkeypatch.setattr(ac, "_allowed_compression_routes", _broken)
    assert nc.native_compaction_context_management(_agent(CODEX_URL), is_codex_backend=True) is None
