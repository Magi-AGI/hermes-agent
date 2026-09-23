"""Compaction route guard: the EFFECTIVE authentication must be the subscription token.

Review (Codex, sync A3 finding 4): the guard classified the wrapper by how it was BUILT
(``is_oauth``, host). Configured headers (``custom_providers[].extra_headers``, merged by
``anthropic_adapter._new_sdk_client`` after it suppressed the API-key header) could add a metered
``X-Api-Key`` and blank the OAuth ``Authorization``, so the request carried only the metered key
while the guard admitted it as OAuth. Per-request ``extra_headers`` could do the same after every
screen had passed. Real SDK clients, fake credentials, no network.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent import anthropic_adapter as aa
from agent import auxiliary_client as ac
from agent.auxiliary_client import (
    AnthropicAuxiliaryClient,
    CodexAuxiliaryClient,
    CompressionRoutingRejected,
    compression_route_guard,
)

OAUTH_TOKEN = "sk-ant-oat01-fake-subscription-token"
METERED_KEY = "sk-ant-api03-fake-metered-console-key"
BASE = "https://api.anthropic.com"


def _oauth_client(monkeypatch, configured_headers=None):
    """A wrapper over a REAL anthropic SDK client built by the real builder, with the given
    ``custom_providers[].extra_headers`` configured for the route."""
    monkeypatch.setattr(aa, "_custom_provider_extra_headers", lambda _url: dict(configured_headers or {}))
    real = aa.build_anthropic_client(OAUTH_TOKEN, BASE)
    return AnthropicAuxiliaryClient(real, "claude-haiku-4-5", OAUTH_TOKEN, BASE, is_oauth=True)


def test_control_a_clean_oauth_client_is_admitted(monkeypatch):
    client = _oauth_client(monkeypatch)
    with compression_route_guard():
        assert ac._compression_client_allowed("anthropic", client) is True


@pytest.mark.parametrize("headers", [
    {"X-Api-Key": METERED_KEY, "Authorization": ""},        # replaces the subscription credential
    {"x-api-key": METERED_KEY},                              # supplements it (case-insensitive)
    {"AUTHORIZATION": "Bearer sk-ant-oat01-someone-else"},  # swaps the bearer
    {"Proxy-Authorization": "Basic Zm9vOmJhcg=="},          # any other credential-bearing header
])
def test_configured_auth_headers_refuse_the_route(monkeypatch, headers):
    client = _oauth_client(monkeypatch, headers)
    with compression_route_guard() as state:
        assert ac._compression_client_allowed("anthropic", client) is False
        assert ("anthropic", "auth_override") in state.rejections
        # The pre-egress backstop (also what a CACHED client meets on every reuse) raises.
        with pytest.raises(CompressionRoutingRejected):
            ac._assert_compression_route("anthropic", client)


def test_harmless_configured_headers_stay_admitted(monkeypatch):
    client = _oauth_client(monkeypatch, {"X-Request-Source": "hermes", "anthropic-beta": "x"})
    with compression_route_guard():
        assert ac._compression_client_allowed("anthropic", client) is True


def test_a_client_whose_headers_cannot_be_read_fails_closed():
    client = AnthropicAuxiliaryClient(object(), "claude-haiku-4-5", OAUTH_TOKEN, BASE, is_oauth=True)
    with compression_route_guard() as state:
        assert ac._compression_client_allowed("anthropic", client) is False
        assert ("anthropic", "unverifiable_auth") in state.rejections


def test_per_request_credential_headers_are_refused_before_egress(monkeypatch):
    client = _oauth_client(monkeypatch)
    sent = []
    with patch("agent.anthropic_adapter.create_anthropic_message", side_effect=lambda *_a, **_k: sent.append(1)):
        with compression_route_guard():
            with pytest.raises(CompressionRoutingRejected):
                client.chat.completions.create(
                    model="claude-haiku-4-5", messages=[{"role": "user", "content": "x"}],
                    max_tokens=16, extra_headers={"x-api-key": METERED_KEY})
    assert sent == [], "nothing may be sent"


def test_codex_per_request_authorization_override_is_refused():
    real = MagicMock()
    real.api_key = "codex-oauth-token"
    real.base_url = "https://chatgpt.com/backend-api/codex"
    client = CodexAuxiliaryClient(real, "gpt-5.5")
    with compression_route_guard():
        with pytest.raises(CompressionRoutingRejected):
            client.chat.completions.create(
                model="gpt-5.5", messages=[{"role": "user", "content": "x"}],
                extra_headers={"Authorization": "Bearer metered-key"})
    real.responses.create.assert_not_called()
    real.responses.stream.assert_not_called()


def test_per_request_credential_headers_are_fine_outside_compaction(monkeypatch):
    """Scoped to the compaction guard: other auxiliary tasks keep upstream's behaviour."""
    ac._refuse_compaction_auth_override_headers({"x-api-key": METERED_KEY})  # no guard -> no raise
