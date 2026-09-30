"""Codex closure review of magi/config-auth at 1615aeeb1b (review-artifacts/config-auth/codex-closure/).

A43 probes are adopted verbatim where their seam still exists; two inject the managed read failure at
the new single-read seam (``managed_scope._read_managed_bytes``), since the old two-read seams were the
defect. A44 probes asserted the unsafe outcome Codex reproduced; here they assert the ruling: every
provider selection fails closed after a failed authoritative (user or managed) config read.
All credentials are fake; nothing is sent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from hermes_cli import auth, config, managed_scope, runtime_provider
from utils import file_signature


def rewrite_preserving_mtime(path: Path, data: bytes) -> None:
    previous = path.stat()
    assert len(data) == previous.st_size
    with path.open("r+b") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert path.read_bytes() == data
    assert file_signature(path.stat()) == file_signature(previous)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    for pconfig in auth.PROVIDER_REGISTRY.values():
        for var in pconfig.api_key_env_vars or ():
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("agent.bedrock_adapter.has_aws_credentials", lambda: False)
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE, config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    managed_scope.invalidate_managed_cache()
    yield home
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE, config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    managed_scope.invalidate_managed_cache()


def _fail_managed_reads(monkeypatch, managed_path: Path, times: int):
    real = managed_scope._read_managed_bytes
    failed = []

    def flaky(path):
        if Path(path) == managed_path and len(failed) < times:
            failed.append(True)
            raise PermissionError(13, "simulated managed read failure", str(path))
        return real(path)

    monkeypatch.setattr(managed_scope, "_read_managed_bytes", flaky)
    return failed


# ── A43 ──────────────────────────────────────────────────────────────────────────────────────────

def test_tui_raw_writeback_preserves_external_same_metadata_edit(tmp_path, monkeypatch):
    import tui_gateway.server as server

    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    before = b"model:\n  context_length: 32768\n"
    after = b"model:\n  context_length: 65536\n"
    config_path.write_bytes(before)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    assert server._load_cfg_raw()["model"]["context_length"] == 32768

    rewrite_preserving_mtime(config_path, after)
    server._write_config_key("display.skin", "midnight")
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["context_length"] == 65536


def test_direct_alias_cache_sees_external_same_metadata_edit(tmp_path, monkeypatch):
    import hermes_cli.model_switch as switch

    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    before = b"model_aliases:\n  quick:\n    model: model-a\n"
    after = b"model_aliases:\n  quick:\n    model: model-b\n"
    config_path.write_bytes(before)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(switch, "_DIRECT_ALIAS_IDENTITY", None)
    monkeypatch.setattr(switch, "_DIRECT_ALIAS_LOADED", None)
    monkeypatch.setattr(switch, "DIRECT_ALIASES", {})
    assert switch.resolve_startup_model_route("quick").model == "model-a"

    rewrite_preserving_mtime(config_path, after)
    assert switch.resolve_startup_model_route("quick").model == "model-b"


def test_managed_read_failure_reports_config_failure(isolated_home, tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    (isolated_home / "config.yaml").write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
    managed_path = managed / "config.yaml"
    managed_path.write_text("model:\n  provider: anthropic\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _fail_managed_reads(monkeypatch, managed_path, times=10**6)

    value, failure = config.load_config_with_status()
    assert failure is not None, (value["model"], failure)
    assert str(managed_path) in failure


def test_managed_cache_parses_bytes_it_signed(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    config_path = managed / "config.yaml"
    first = b"approvals:\n  deny: []  \n"
    second = b"approvals:\n  deny: [rm]\n"
    config_path.write_bytes(first)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    real = managed_scope._read_managed_bytes
    switched = []

    def change_after_read(path):
        result = real(path)
        if Path(path) == config_path and not switched:
            switched.append(True)
            rewrite_preserving_mtime(config_path, second)  # lands after the one read
        return result

    monkeypatch.setattr(managed_scope, "_read_managed_bytes", change_after_read)
    assert managed_scope.load_managed_config()["approvals"]["deny"] == []  # the bytes it read
    assert managed_scope.load_managed_config()["approvals"]["deny"] == ["rm"]
    rewrite_preserving_mtime(config_path, first)
    assert managed_scope.load_managed_config()["approvals"]["deny"] == []


def test_model_options_inflight_build_is_not_joined_after_env_rotation(tmp_path, monkeypatch):
    from tui_gateway.rpc_coalesce import model_options_key
    from tui_gateway.rpc_lanes import SingleFlight

    @dataclass
    class PickerContext:
        provider: str = ""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    env_path = home / ".env"
    env_path.write_bytes(b"OPENROUTER_API_KEY=sk-or-FAKE-1111\n")
    ctx = PickerContext()
    flight = SingleFlight()
    first_key = model_options_key(home, ctx, {})
    _first_future, first_is_leader = flight.claim(first_key)
    assert first_is_leader

    rewrite_preserving_mtime(env_path, b"OPENROUTER_API_KEY=sk-or-FAKE-2222\n")
    second_key = model_options_key(home, ctx, {})
    _second_future, second_is_leader = flight.claim(second_key)
    assert second_is_leader, "a picker requested after a key rotation must build from the new key"


def test_tui_config_poll_detects_same_mtime_cosmetic_edit(tmp_path, monkeypatch):
    import tui_gateway.server as server

    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_bytes(b"display:\n  skin: light\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    first = server._methods["config.get"]("rid", {"key": "mtime"})["result"]
    rewrite_preserving_mtime(cfg, b"display:\n  skin: night\n")
    second = server._methods["config.get"]("rid", {"key": "mtime"})["result"]
    assert first != second, "the TUI poller needs a changed revision to rehydrate cosmetic config"
    assert first["mtime"] == second["mtime"] and first["rev"] != second["rev"]


def test_picker_build_reuses_its_config_snapshot_for_credential_pools(isolated_home, monkeypatch):
    """Perf guard for the 2026-09-13 model.options convoy: pool loads inside one build share the build's
    snapshot instead of re-validating config.yaml once per provider row."""
    from agent import credential_pool
    from agent.models_dev import config_snapshot

    (isolated_home / "config.yaml").write_text("model:\n  provider: anthropic\n", encoding="utf-8")
    loads = []
    real = config._load_config_impl
    monkeypatch.setattr(config, "_load_config_impl", lambda **kw: loads.append(1) or real(**kw))
    with config_snapshot():
        before = len(loads)
        for _ in range(50):
            assert credential_pool._load_config_safe()["model"]["provider"] == "anthropic"
        assert len(loads) == before


# ── A44: fail closed after a failed authoritative read ─────────────────────────────────────────

def test_transient_managed_read_failure_refuses_then_recovers(isolated_home, tmp_path, monkeypatch):
    (isolated_home / "config.yaml").write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    managed_path = managed_dir / "config.yaml"
    managed_path.write_text("model:\n  provider: gemini\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-000000000000")
    failed = _fail_managed_reads(monkeypatch, managed_path, times=1)

    with pytest.raises(auth.AuthError) as excinfo:
        auth.resolve_provider("auto")
    assert failed and excinfo.value.code == "corrupt_config"
    # Not cached: the next observation reads the managed pin again, no cache clearing needed.
    assert auth.resolve_provider("auto") == "gemini"


def test_inference_provider_env_refused_after_failed_config_read(isolated_home, monkeypatch):
    (isolated_home / "config.yaml").write_text("model: [broken\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-000000000")
    assert config.load_config_with_status()[1]
    with pytest.raises(auth.AuthError) as excinfo:
        runtime_provider.resolve_requested_provider()
    assert excinfo.value.code == "corrupt_config"
    with pytest.raises(auth.AuthError):
        runtime_provider.resolve_runtime_provider()


def test_inference_provider_env_still_works_on_a_clean_read(isolated_home, monkeypatch):
    (isolated_home / "config.yaml").write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "gemini")
    assert runtime_provider.resolve_requested_provider() == "gemini"
    assert runtime_provider.resolve_requested_provider("anthropic") == "anthropic"  # explicit flag


def test_terminal_openrouter_fallback_refused_when_config_breaks_mid_resolution(isolated_home, monkeypatch):
    cfg = isolated_home / "config.yaml"
    cfg.write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
    monkeypatch.setattr(auth, "_logged_in_oauth_active_provider", lambda **_kw: "openai-codex")
    monkeypatch.setattr(auth, "_openrouter_auto_detected", lambda _reader: False)
    monkeypatch.setattr(auth, "_env_key_auto_detected", lambda _reader, _oauth: None)

    def fail_oauth(*_args, **_kwargs):
        cfg.write_text("model: [broken\n", encoding="utf-8")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-late-fake-000000000")
        raise auth.AuthError("simulated expired OAuth", code="expired_token")

    monkeypatch.setattr(runtime_provider, "_resolve_oauth_runtime", fail_oauth)
    with pytest.raises(auth.AuthError) as excinfo:
        runtime_provider.resolve_runtime_provider()
    assert excinfo.value.code == "corrupt_config"


@pytest.mark.parametrize("rung", ["oauth-login", "free-tier"])
def test_saved_login_and_free_tier_refused_after_failed_read(isolated_home, monkeypatch, rung):
    (isolated_home / "config.yaml").write_text("model: [broken\n", encoding="utf-8")
    if rung == "oauth-login":
        monkeypatch.setattr(auth, "_logged_in_oauth_active_provider", lambda **_kw: "openai-codex")
    else:
        monkeypatch.setattr(auth, "_logged_in_oauth_active_provider", lambda **_kw: None)
        monkeypatch.setattr("hermes_cli.anon_auth.guest_enabled", lambda: True)
        monkeypatch.setattr("hermes_cli.anon_auth.has_guest", lambda: True)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.resolve_provider("auto")
    assert excinfo.value.code == "corrupt_config"


def test_oneshot_model_detection_refused_after_failed_read_but_explicit_provider_kept(isolated_home, monkeypatch):
    from hermes_cli.oneshot import _resolve_model_and_provider

    (isolated_home / "config.yaml").write_text("model: [broken\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-000000000000")
    cfg = config.load_config()
    with pytest.raises(auth.AuthError) as excinfo:
        _resolve_model_and_provider(cfg, "some-vendor/some-model", None)
    assert excinfo.value.code == "corrupt_config"
    choice = _resolve_model_and_provider(cfg, "some-model", "anthropic")  # --provider typed this run
    assert choice.provider == "anthropic"


def test_fallback_chain_is_not_walked_on_a_failed_read(isolated_home, monkeypatch):
    (isolated_home / "config.yaml").write_text("model: [broken\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-000000000000")
    chain_cfg = {"fallback_providers": [{"provider": "openrouter", "model": "a/b"}]}
    with pytest.raises(auth.AuthError) as excinfo:
        runtime_provider.resolve_runtime_with_fallback(chain_cfg)
    assert excinfo.value.code == "corrupt_config"


# ── Refusal wording and its window ─────────────────────────────────────────────────────────────

def test_refusal_says_retry_for_a_read_error_and_fix_for_complete_broken_bytes(isolated_home, monkeypatch):
    cfg = isolated_home / "config.yaml"
    cfg.write_text("model:\n  provider: anthropic\n", encoding="utf-8")
    real = config._read_config_bytes

    def denied(path):
        if Path(path) == cfg:
            raise PermissionError(13, "sharing violation", str(path))
        return real(path)

    monkeypatch.setattr(config, "_read_config_bytes", denied)
    with pytest.raises(auth.AuthError) as read_err:
        auth.resolve_provider("auto")
    text = str(read_err.value)
    assert "could not be read just now" in text and "try again" in text
    assert "formatting error" not in text and "corrupt" not in text.lower()

    monkeypatch.setattr(config, "_read_config_bytes", real)
    cfg.write_text("model: [broken\n", encoding="utf-8")
    with pytest.raises(auth.AuthError) as parse_err:
        auth.resolve_provider("auto")
    assert "formatting error" in str(parse_err.value) and "hermes config edit" in str(parse_err.value)


def test_refusal_window_is_bounded_by_the_save(isolated_home, monkeypatch):
    """A fresh process (no last-known-good yet) whose first read catches an in-place save half-written
    refuses as a retryable read, not a formatting error, and the first read after the save completes
    selects the configured provider. (With a last-known-good config in hand the same read serves it
    and keeps the configured provider, so no refusal is seen at all.)"""
    cfg = isolated_home / "config.yaml"
    final = b"model:\n  provider: anthropic\n"
    cfg.write_bytes(final)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-000000000000")

    real = config._read_config_bytes
    saving = {"on": True}

    def mid_save(path):
        if Path(path) == cfg and saving["on"]:
            saving["on"] = False           # the save finishes right after this read
            return b"model:\n  provider: [anthr"  # the half-written bytes this read caught
        return real(path)

    monkeypatch.setattr(config, "_read_config_bytes", mid_save)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.resolve_provider("auto")
    assert "could not be read just now" in str(excinfo.value)  # bytes changed underneath: a save
    assert auth.resolve_provider("auto") == "anthropic"      # save done: the next read is clean
