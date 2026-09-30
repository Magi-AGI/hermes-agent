"""A44/A45: provider selection after a config.yaml read that did not succeed.

A44: ``resolve_provider("auto")`` takes the configured provider AND whether that read was clean from
ONE observation (``load_config_with_status``), and refuses every rung that would adopt a provider
from outside the config (OpenRouter env/pool, provider env keys, AWS/Bedrock) when it was not.
A45: when the active config does not read, "Unknown provider" names the read failure and the file.
All credentials are fake; the read failure is injected at the loader's ``open`` seam.
"""

from __future__ import annotations

import builtins

import pytest

from hermes_cli import auth
from hermes_cli import config as config_mod

_CONFIGURED = "model:\n  provider: anthropic\n"
_ENV_VARS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HERMES_INFERENCE_PROVIDER")


def _env_key_provider():
    """First registry api_key provider the env rung may adopt (not via the OpenRouter rung's keys)."""
    openrouter_vars = {"OPENAI_API_KEY", "OPENROUTER_API_KEY"}
    for pid, pconfig in auth.PROVIDER_REGISTRY.items():
        env_vars = [v for v in pconfig.api_key_env_vars or () if v not in openrouter_vars]
        if pconfig.auth_type == "api_key" and pid not in auth._NO_AUTO_DETECT_PROVIDERS and env_vars:
            return pid, env_vars[0]
    pytest.skip("no auto-detectable api_key provider in the registry")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for pconfig in auth.PROVIDER_REGISTRY.values():
        for var in getattr(pconfig, "api_key_env_vars", ()) or ():
            monkeypatch.delenv(var, raising=False)
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(auth, "_logged_in_oauth_active_provider", lambda **_kw: None)
    monkeypatch.setattr("agent.bedrock_adapter.has_aws_credentials", lambda: False)
    monkeypatch.setattr("hermes_cli.anon_auth.guest_enabled", lambda: False)
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    yield home
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()


def _fail_config_reads(monkeypatch, path, *, until=None):
    """Every loader open of *path* fails (PermissionError) while ``until`` is unset/false."""
    real_open = builtins.open

    def failing_open(file, *args, **kwargs):
        if str(file) == str(path) and not (until and until()):
            raise PermissionError(13, "config read interrupted", str(path))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(config_mod, "open", failing_open, raising=False)


@pytest.mark.parametrize("rung", ["openrouter-env", "provider-env-key", "bedrock"])
def test_failed_config_read_refuses_every_adoption_rung(home, monkeypatch, rung):
    cfg = home / "config.yaml"
    cfg.write_text(_CONFIGURED, encoding="utf-8")
    if rung == "openrouter-env":
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-FAKE-000000000000")
    elif rung == "provider-env-key":
        _pid, var = _env_key_provider()
        monkeypatch.setenv(var, "FAKE-key-0000000000000000")
    else:
        monkeypatch.setattr("agent.bedrock_adapter.has_aws_credentials", lambda: True)
    _fail_config_reads(monkeypatch, cfg)

    with pytest.raises(auth.AuthError) as excinfo:
        auth.resolve_provider("auto")
    assert excinfo.value.code == "corrupt_config"
    assert str(cfg) in str(excinfo.value)


def test_read_that_recovers_before_the_env_rung_still_refuses(home, monkeypatch):
    """The failure is carried from the observation that produced the value: a file that reads
    cleanly again by the time the env rung runs must not turn the failed read into consent."""
    cfg = home / "config.yaml"
    cfg.write_text(_CONFIGURED, encoding="utf-8")
    pid, var = _env_key_provider()
    monkeypatch.setenv(var, "FAKE-key-0000000000000000")
    recovered = []
    _fail_config_reads(monkeypatch, cfg, until=lambda: bool(recovered))
    real_env_rung = auth._env_key_auto_detected

    def env_rung_after_recovery(reader, oauth_active):
        recovered.append(True)
        assert config_mod.load_config_with_status()[1] is None, "the file reads cleanly again"
        return real_env_rung(reader, oauth_active)

    monkeypatch.setattr(auth, "_env_key_auto_detected", env_rung_after_recovery)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.resolve_provider("auto")
    assert recovered and excinfo.value.code == "corrupt_config"


def test_clean_read_keeps_env_adoption_and_config_pin(home, monkeypatch):
    cfg = home / "config.yaml"
    pid, var = _env_key_provider()
    monkeypatch.setenv(var, "FAKE-key-0000000000000000")
    cfg.write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
    assert auth.resolve_provider("auto") == pid
    cfg.write_text(_CONFIGURED, encoding="utf-8")
    assert auth.resolve_provider("auto") == "anthropic"


def test_bootstrap_does_not_claim_inference_while_config_is_unreadable(monkeypatch):
    from hermes_cli import free_tier_bootstrap

    def refuse(*_a, **_kw):
        raise auth.AuthError("config.yaml could not be read", code="corrupt_config")

    monkeypatch.setattr(auth, "resolve_provider", refuse)
    assert free_tier_bootstrap._inventory_other_providers() is True

    def nothing(*_a, **_kw):
        raise auth.AuthError("nothing configured", code="no_provider_configured")

    monkeypatch.setattr(auth, "resolve_provider", nothing)
    assert free_tier_bootstrap._inventory_other_providers() is False


def test_auxiliary_discovery_needs_a_clean_read(home, monkeypatch):
    from agent.auxiliary_client import _discovery_chain_allowed

    cfg = home / "config.yaml"
    cfg.write_text(_CONFIGURED, encoding="utf-8")
    assert _discovery_chain_allowed("", "compression") is True
    _fail_config_reads(monkeypatch, cfg)
    assert _discovery_chain_allowed("", "compression") is False


def test_unknown_provider_names_the_failed_config_read(home, monkeypatch):
    from hermes_cli.model_switch import switch_model

    cfg = home / "config.yaml"
    cfg.write_text("providers:\n  relaybox:\n    base_url: http://127.0.0.1:11434/v1\n", encoding="utf-8")
    kwargs = dict(current_provider="anthropic", current_model="old-model", explicit_provider="relaybox-missing")

    clean = switch_model("local-model", **kwargs, user_providers={})
    assert not clean.success and "Unknown provider 'relaybox-missing'" in clean.error_message
    with pytest.raises(auth.AuthError, match="Unknown provider 'relaybox-missing'"):
        auth.resolve_provider("relaybox-missing")

    _fail_config_reads(monkeypatch, cfg)
    failed = switch_model("local-model", **kwargs, user_providers={})
    assert not failed.success
    for message in (failed.error_message, _auth_error_text("relaybox-missing")):
        assert "Unknown provider" not in message
        assert str(cfg) in message and "could not be read" in message and "hermes config check" in message


def _auth_error_text(name: str) -> str:
    with pytest.raises(auth.AuthError) as excinfo:
        auth.resolve_provider(name)
    return str(excinfo.value)
