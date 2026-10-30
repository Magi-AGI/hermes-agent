"""Codex closure r2 probes on magi/config-auth (review-artifacts/config-auth/codex-closure-r2/).

Verbatim where the probe's seam still exists. Adapted (and marked) where the defect was the seam:
- the requested-provider race now injects the break at the ONE observation (load_config_with_status);
- the TUI read-then-save race injects the edit right after the transaction's strict read, since
  _write_config_key no longer saves a mapping read earlier outside the transaction.
The last-known-good probe is pinned to Lake's 2026-09-30 ruling (keep the LKG provider; refuse every
implicit fallback) and to the opposite behaviour behind the one switch. Fake credentials only.
"""

from __future__ import annotations

import pytest
import yaml

def test_full_runtime_must_not_use_env_after_final_config_read_failure(tmp_path, monkeypatch):
    from hermes_cli import auth, config, runtime_provider

    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-000000000")
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()

    original = config.load_config_readonly
    switched = []

    def break_once_before_final_read():
        if not switched:
            switched.append(True)
            cfg.write_text("model: [incomplete\n", encoding="utf-8")
        return original()

    monkeypatch.setattr(config, "load_config_readonly", break_once_before_final_read)
    try:
        try:
            runtime = runtime_provider.resolve_runtime_provider()
        except auth.AuthError as error:
            assert error.code == "corrupt_config"
        else:
            assert config.current_config_read_failure(), "the final read must have failed"
            pytest.fail(f"constructed {runtime['provider']!r} runtime after the failed final read")
    finally:
        monkeypatch.setattr(config, "load_config_readonly", original)
        for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                      config._LAST_EXPANDED_CONFIG_BY_PATH):
            cache.clear()
    assert switched, "the final config read was not reached"


def test_loader_exception_gets_retry_wording_not_parse_wording(monkeypatch):
    from hermes_cli import auth, config

    def unreadable():
        raise PermissionError(13, "simulated settings read denial")

    monkeypatch.setattr(config, "load_config_with_status", unreadable)
    with pytest.raises(auth.AuthError) as error:
        auth.resolve_provider("auto")
    assert error.value.code == "corrupt_config"
    assert "could not be read just now" in str(error.value)
    assert "try again" in str(error.value)


def test_explicit_primary_failure_must_not_walk_stale_fallback_chain(tmp_path, monkeypatch):
    from hermes_cli import auth, config, runtime_provider

    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text("fallback_providers:\n  - provider: gemini\n    model: model-x\n",
                   encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-000000000")
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()

    previously_saved = config.load_config()
    cfg.write_text("model: [broken\n", encoding="utf-8")
    # The explicitly requested primary fails, so walking a remembered chain
    # would select an implicit provider during a failed authoritative read.
    def primary_missing(*_args, **_kwargs):
        raise auth.AuthError("primary credentials unavailable", code="missing_api_key")

    monkeypatch.setattr(runtime_provider, "_anthropic_token_or_raise", primary_missing)
    try:
        try:
            runtime, entry = runtime_provider.resolve_runtime_with_fallback(
                previously_saved, requested="anthropic")
        except auth.AuthError:
            pass
        else:
            assert config.current_config_read_failure(), "the current config did not fail"
            pytest.fail(f"walked stale fallback to {runtime['provider']!r} / {entry!r}")
    finally:
        for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                      config._LAST_EXPANDED_CONFIG_BY_PATH):
            cache.clear()

def test_managed_scalar_root_is_reported_as_failed_read(tmp_path, monkeypatch):
    from hermes_cli import config, managed_scope

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    (managed / "config.yaml").write_text("not-a-mapping\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    try:
        observation = managed_scope.read_managed_config()
        assert observation.error, observation
        _value, failure = config.load_config_with_status()
        assert failure, "a non-mapping admin config was treated as cleanly absent"
    finally:
        managed_scope.invalidate_managed_cache()
        config._LOAD_CONFIG_CACHE.clear()
        config._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def test_tui_read_failure_cannot_turn_a_partial_write_into_full_replacement(tmp_path, monkeypatch):
    import builtins
    import tui_gateway.server as server

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  context_length: 32768\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    real_open = builtins.open

    def fail_raw_read(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and mode == "rb":
            raise PermissionError("transient raw config read refusal")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(server, "open", fail_raw_read, raising=False)
    try:
        server._write_config_key("display.skin", "midnight")
    except PermissionError:
        pass  # a failed write is safe
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["model"]["context_length"] == 32768, saved


def test_managed_disappearing_at_open_is_not_clean_absence(tmp_path, monkeypatch):
    from hermes_cli import config, managed_scope

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    path = managed / "config.yaml"
    path.write_text("model:\n  provider: gemini\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    try:
        assert config.load_config_with_status()[0]["model"]["provider"] == "gemini"
        real_read = managed_scope._read_managed_bytes
        faults = []

        def vanish_during_both_load_attempts(p):
            if p == path and len(faults) < 2:
                faults.append(True)
                raise FileNotFoundError(2, "file vanished between observations", str(path))
            return real_read(p)

        monkeypatch.setattr(managed_scope, "_read_managed_bytes", vanish_during_both_load_attempts)
        value, failure = config.load_config_with_status()
        assert len(faults) == 2, faults
        assert failure, (value["model"], failure)
    finally:
        managed_scope.invalidate_managed_cache()
        config._LOAD_CONFIG_CACHE.clear()
        config._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def test_stable_managed_yaml_error_has_parse_advice(tmp_path, monkeypatch):
    from hermes_cli import config, managed_scope

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    (managed / "config.yaml").write_text("model: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    config._LOAD_CONFIG_CACHE.clear()
    try:
        _value, failure = config.load_config_with_status()
        assert failure is not None
        assert failure.kind == "parse", (failure.kind, str(failure))
    finally:
        managed_scope.invalidate_managed_cache()
        config._LOAD_CONFIG_CACHE.clear()


def _clear(config):
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE, config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()


def test_inference_env_must_not_win_when_its_one_observation_fails(tmp_path, monkeypatch):
    """Adapted probe 1: the env-vs-config decision is one observation; break the file right before it."""
    from hermes_cli import auth, config, runtime_provider

    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "gemini")
    _clear(config)
    original = config.load_config_with_status
    observed = []

    def break_before_the_observation():
        cfg.write_text("model: [incomplete\n", encoding="utf-8")
        observed.append(True)
        return original()

    monkeypatch.setattr(config, "load_config_with_status", break_before_the_observation)
    try:
        with pytest.raises(auth.AuthError) as error:
            runtime_provider.resolve_requested_provider()
        assert error.value.code == "corrupt_config" and observed
    finally:
        _clear(config)


def test_tui_write_does_not_erase_an_edit_between_its_read_and_save(tmp_path, monkeypatch):
    """Adapted settings probe 2: the external edit lands right after the write transaction's read."""
    import tui_gateway.server as server

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  context_length: 32768\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    real_read = server._read_cfg_for_update
    edits = []

    def read_then_external_edit(p):
        result = real_read(p)
        if not edits:
            edits.append(True)
            path.write_text("model:\n  context_length: 65536\n", encoding="utf-8")
        return result

    monkeypatch.setattr(server, "_read_cfg_for_update", read_then_external_edit)
    server._write_config_key("display.skin", "midnight")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert edits and saved["model"]["context_length"] == 65536, saved
    assert saved["display"]["skin"] == "midnight", saved


def test_tui_write_refuses_a_file_that_keeps_changing(tmp_path, monkeypatch):
    import tui_gateway.server as server
    from utils import ConfigChangedDuringWrite

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  context_length: 1\n", encoding="utf-8")
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_read = server._read_cfg_for_update
    n = []

    def always_edited_after_read(p):
        result = real_read(p)
        n.append(1)
        path.write_text(f"model:\n  context_length: {100 + len(n)}\n", encoding="utf-8")
        return result

    monkeypatch.setattr(server, "_read_cfg_for_update", always_edited_after_read)
    with pytest.raises(ConfigChangedDuringWrite):
        server._write_config_key("display.skin", "midnight")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "display" not in saved and saved["model"]["context_length"] == 100 + len(n)


@pytest.mark.parametrize("refuse_lkg", [False, True], ids=["lake-ruling-keep", "switch-refuse"])
def test_last_known_good_provider_policy(tmp_path, monkeypatch, refuse_lkg):
    """Probe 2 (policy). Lake 2026-09-30: KEEP the last-known-good pin during a failed read (sessions keep
    their configured provider through an editor save and cannot fall onto metered billing, because every
    implicit fallback still refuses). Codex recommended refusing: an accepted divergence, one switch."""
    from hermes_cli import auth, config

    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text("model:\n  provider: anthropic\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-000000000000")
    monkeypatch.setattr(auth, "REFUSE_LAST_KNOWN_GOOD_PROVIDER", refuse_lkg)
    _clear(config)
    try:
        assert auth.resolve_provider("auto") == "anthropic"
        cfg.write_text("model:\n  provider: gemini\n  broken: [unclosed\n", encoding="utf-8")
        assert config.current_config_read_failure()
        if refuse_lkg:
            with pytest.raises(auth.AuthError) as error:
                auth.resolve_provider("auto")
            assert error.value.code == "corrupt_config"
        else:
            assert auth.resolve_provider("auto") == "anthropic"  # the LKG pin, never OpenRouter
    finally:
        _clear(config)


def test_no_last_known_good_refuses_even_under_the_keep_ruling(tmp_path, monkeypatch):
    from hermes_cli import auth, config

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: gemini\n  broken: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-000000000000")
    _clear(config)
    try:
        with pytest.raises(auth.AuthError) as error:
            auth.resolve_provider("auto")
        assert error.value.code == "corrupt_config"
    finally:
        _clear(config)


def test_managed_file_removal_counts_after_the_grace_window(tmp_path, monkeypatch):
    from hermes_cli import config, managed_scope

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    path = managed / "config.yaml"
    path.write_text("model:\n  provider: gemini\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    _clear(config)
    clock = [1000.0]
    monkeypatch.setattr(managed_scope.time, "monotonic", lambda: clock[0])
    try:
        assert managed_scope.read_managed_config().value["model"]["provider"] == "gemini"
        path.unlink()
        assert managed_scope.read_managed_config().error_kind == "read"  # inside the window
        clock[0] += managed_scope.VANISHED_GRACE_SECONDS + 1
        observation = managed_scope.read_managed_config()
        assert observation.error is None and observation.value == {}  # a real removal
    finally:
        managed_scope.invalidate_managed_cache()
        _clear(config)


# ── Paths found by the round-3 sweep (every implicit provider pick, not just Gemini) ─────────────

@pytest.fixture
def broken_home(tmp_path, monkeypatch):
    from hermes_cli import config

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("model: [broken\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-000000000000")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-000000000")
    _clear(config)
    yield home
    _clear(config)


def test_cli_startup_fallback_walk_refused_after_failed_read(broken_home, monkeypatch):
    import cli as cli_mod
    from hermes_cli import auth, runtime_provider

    obj = object.__new__(cli_mod.HermesCLI)
    obj._fallback_model = [{"provider": "gemini", "model": "model-x"}]
    calls = []
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **kw: calls.append(kw) or {})
    primary = auth.AuthError("no key", code="missing_api_key")
    assert obj._resolve_fallback_runtime(primary) is None
    assert calls == []


def test_init_time_fallback_walk_refused_after_failed_read(broken_home, monkeypatch):
    import types

    from agent import agent_init
    from hermes_cli import auth

    monkeypatch.setattr("agent.auxiliary_client.resolve_provider_client", lambda *a, **k: (None, None))
    agent = types.SimpleNamespace(provider="", model="m")
    with pytest.raises(auth.AuthError) as error:
        agent_init._routed_client_kwargs(agent, [{"provider": "gemini", "model": "model-x"}], None)
    assert error.value.code == "corrupt_config"


def test_vision_and_custom_scans_do_not_pick_env_providers_after_failed_read(broken_home, monkeypatch):
    from agent import auxiliary_client as aux

    picked = []
    monkeypatch.setattr(aux, "_resolve_strict_vision_backend", lambda c: picked.append(c) or (object(), "m"))
    monkeypatch.setattr(aux, "_resolve_api_key_provider", lambda *a, **k: picked.append("api-key") or (object(), "m"))
    monkeypatch.setattr(aux, "_try_custom_endpoint", lambda *a, **k: (None, None))
    assert aux._vision_auto_route({"provider": ""}, None, None, False) == (None, None, None)
    assert aux._resolve_auto_route({"provider": ""}, "compression") == (None, None, "")
    assert picked == []


def test_model_switch_detection_refused_after_failed_read(broken_home):
    from hermes_cli.model_switch import switch_model

    result = switch_model("some-model-x", current_provider="", current_model="old-model")
    assert not result.success
    assert "Cannot pick a provider for some-model-x automatically" in result.error_message
    assert "Your settings could not be loaded" in result.error_message
