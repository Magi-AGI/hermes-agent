"""CP007K — opt-in local-loopback compaction route exception.

Literal loopback (``localhost`` / ``127.0.0.0/8`` / ``::1``) is not metered
third-party cloud egress: the compaction summary stays on-box. It may therefore
be admitted for compaction, but ONLY behind the explicit, default-off opt-in
``auxiliary.compression.allow_local_loopback_route``.

These tests lock the default-closed posture and every rejection edge (public /
cloud / private-LAN / wildcard / spoofed hosts, provider-name aliasing, and
proxy egress), and prove the startup validator agrees with the runtime gate.
"""

from unittest.mock import MagicMock

import pytest

from agent import auxiliary_client as ac
from agent.auxiliary_client import (
    CodexAuxiliaryClient,
    compression_route_guard,
)

OAUTH_TOKEN = "sk-ant-oat01-real-claude-max-token"

_PROXY_ENVS = (
    "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
)


@pytest.fixture(autouse=True)
def clear_proxy_env(monkeypatch):
    """Start every test from a no-proxy baseline (loopback is proxy-safe)."""
    for var in (*_PROXY_ENVS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


def _client(base_url):
    c = MagicMock()
    c.base_url = base_url
    return c


def _fake_anthropic_client(base_url="https://api.anthropic.com", is_oauth=True, token=OAUTH_TOKEN):
    client = MagicMock(spec=ac.AnthropicAuxiliaryClient)
    client.base_url = base_url
    client.is_oauth = is_oauth
    client.api_key = token
    return client


class TestIsLoopbackHost:
    @pytest.mark.parametrize("host", [
        "127.0.0.1", "127.5.5.5", "127.255.255.254", "localhost",
        "::1", "ip6-localhost", "ip6-loopback",
    ])
    def test_loopback_true(self, host):
        assert ac._is_loopback_host(host) is True

    @pytest.mark.parametrize("host", [
        "", "0.0.0.0", "::", "10.0.0.5", "172.16.0.9", "192.168.1.2",
        "169.254.1.1", "api.openai.com", "chatgpt.com", "127.0.0.1.evil.com",
        "localhost.evil.com", "notlocalhost", "8.8.8.8",
    ])
    def test_non_loopback_false(self, host):
        assert ac._is_loopback_host(host) is False


class TestLoopbackProxySafety:
    def test_no_proxy_env_is_safe(self):
        assert ac._loopback_egress_is_proxy_safe("127.0.0.1") is True

    def test_proxy_without_no_proxy_is_unsafe(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
        assert ac._loopback_egress_is_proxy_safe("127.0.0.1") is False

    def test_proxy_with_loopback_no_proxy_is_safe(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
        assert ac._loopback_egress_is_proxy_safe("127.0.0.1") is True

    def test_proxy_with_wildcard_no_proxy_is_safe(self, monkeypatch):
        monkeypatch.setenv("ALL_PROXY", "socks5://corp-proxy:1080")
        monkeypatch.setenv("NO_PROXY", "*")
        assert ac._loopback_egress_is_proxy_safe("127.0.0.1") is True


class TestRuntimeGateDefaultClosed:
    @pytest.mark.parametrize("base_url", [
        "http://127.0.0.1:5000/v1", "http://localhost:5000/v1", "http://[::1]:5000/v1",
    ])
    def test_loopback_rejected_without_opt_in(self, base_url, monkeypatch):
        monkeypatch.setattr(ac, "_local_loopback_opt_in", lambda: False)
        with compression_route_guard():
            assert ac._compression_client_allowed("mock", _client(base_url)) is False


class TestRuntimeGateOptIn:
    @pytest.fixture(autouse=True)
    def _opt_in(self, monkeypatch):
        monkeypatch.setattr(ac, "_local_loopback_opt_in", lambda: True)

    @pytest.mark.parametrize("base_url", [
        "http://127.0.0.1:5000/v1", "http://localhost:5000/v1",
        "http://[::1]:5000/v1", "http://127.9.9.9:11434/v1",
    ])
    def test_loopback_admitted(self, base_url):
        with compression_route_guard():
            assert ac._compression_client_allowed("mock", _client(base_url)) is True

    @pytest.mark.parametrize("base_url", [
        "https://api.openai.com/v1",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://openrouter.ai/api/v1",
        "http://10.0.0.5:5000/v1",
        "http://172.16.4.4:5000/v1",
        "http://192.168.1.7:5000/v1",
        "http://0.0.0.0:5000/v1",
        "http://127.0.0.1.evil.com/v1",
        "http://localhost.evil.com/v1",
    ])
    def test_non_loopback_rejected(self, base_url):
        with compression_route_guard():
            assert ac._compression_client_allowed("mock", _client(base_url)) is False

    def test_proxied_loopback_rejected(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
        with compression_route_guard():
            assert ac._compression_client_allowed("mock", _client("http://127.0.0.1:5000/v1")) is False

    def test_anthropic_alias_to_foreign_host_still_rejected(self):
        aliased = _fake_anthropic_client(base_url="https://openrouter.ai/api/v1")
        with compression_route_guard():
            assert ac._compression_client_allowed("anthropic", aliased) is False

    def test_codex_alias_to_azure_still_rejected(self):
        real = MagicMock()
        real.api_key = "azure-key"
        real.base_url = "https://my-proj.openai.azure.com"
        codex = CodexAuxiliaryClient(real, "gpt-5.6")
        with compression_route_guard():
            assert ac._compression_client_allowed("openai-codex", codex) is False

    def test_real_subscription_route_still_admitted(self):
        with compression_route_guard():
            assert ac._compression_client_allowed("anthropic", _fake_anthropic_client()) is True


class TestOptInConfigRead:
    @pytest.mark.parametrize("value,expected", [
        (True, True), ("true", True), ("1", True), ("yes", True), ("on", True),
        (False, False), ("false", False), ("", False), (None, False), (0, False),
    ])
    def test_flag_coercion(self, value, expected, monkeypatch):
        cfg = {"auxiliary": {"compression": {"allow_local_loopback_route": value}}}
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
        assert ac._local_loopback_opt_in() is expected

    def test_missing_key_defaults_off(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"auxiliary": {"compression": {}}})
        assert ac._local_loopback_opt_in() is False

    def test_unreadable_config_fails_closed(self, monkeypatch):
        def boom():
            raise RuntimeError("config unreadable")
        monkeypatch.setattr("hermes_cli.config.load_config", boom)
        assert ac._local_loopback_opt_in() is False


class TestStartupValidationAgreesWithGate:
    def _cfg(self, *, opt_in, provider="mock", api="http://127.0.0.1:5000/v1"):
        return {
            "auxiliary": {"compression": {
                "provider": provider,
                "allow_local_loopback_route": opt_in,
            }},
            "providers": {provider: {"api": api}},
        }

    def test_loopback_provider_invalid_without_opt_in(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: self._cfg(opt_in=False))
        problems = ac.validate_configured_compression_routes()
        assert any("mock" in p for p in problems), problems

    def test_loopback_provider_valid_with_opt_in(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: self._cfg(opt_in=True))
        assert ac.validate_configured_compression_routes() == []

    def test_cloud_provider_invalid_even_with_opt_in(self, monkeypatch):
        cfg = self._cfg(opt_in=True, provider="openai", api="https://api.openai.com/v1")
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
        problems = ac.validate_configured_compression_routes()
        assert any("openai" in p for p in problems), problems

    def test_loopback_with_proxy_no_bypass_flagged(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://corp-proxy:8080")
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: self._cfg(opt_in=True))
        problems = ac.validate_configured_compression_routes()
        assert any("proxy" in p.lower() for p in problems), problems
