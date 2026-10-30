"""Codex closure r4 P1 probes on magi/config-auth (review-artifacts/config-auth/codex-closure-r4/).

Final time-boxed round (Lake rule 2026-09-30): only P1s fixed; P2/P3 deferred (REPORT-ROUND5).
Verbatim, except the /theme probe: the fix binds the TUI's active session at its RPC boundary
(useMainApp rpc -> withSessionScope), so the backend now receives the params the TUI actually sends
({key, value, session_id}); the boundary itself is covered by a vitest case. Fake credentials only.
"""

from __future__ import annotations

import pytest
import yaml


def _clear_config_caches(config):
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()


def test_fallback_entry_is_not_an_explicit_per_run_choice_after_config_break(tmp_path, monkeypatch):
    """The saved fallback's `requested=` argument must not turn it into `--provider`."""
    from hermes_cli import auth, config, runtime_provider

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(
        "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
        "fallback_providers:\n  - provider: gemini\n    model: gemini-2.5-flash\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-no-network")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _clear_config_caches(config)
    saved_config = config.load_config()
    assert saved_config["model"]["provider"] == "anthropic"
    assert config.current_config_read_failure() is None

    def primary_unavailable(*_args, **_kwargs):
        raise auth.AuthError("primary unavailable", code="missing_api_key")

    monkeypatch.setattr(runtime_provider, "_anthropic_token_or_raise", primary_unavailable)
    real_resolve = runtime_provider.resolve_runtime_provider
    fallback_calls = []

    def break_on_fallback(**kwargs):
        if kwargs.get("requested") == "gemini":
            fallback_calls.append(dict(kwargs))
            path.write_text("model: [save in progress\n", encoding="utf-8")
        return real_resolve(**kwargs)

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", break_on_fallback)
    try:
        with pytest.raises(auth.AuthError) as error:
            runtime_provider.resolve_runtime_with_fallback(saved_config)
        assert error.value.code == "missing_api_key" or error.value.code == "corrupt_config"
        assert fallback_calls, "the saved fallback resolver was not exercised"
        assert config.current_config_read_failure(), "the config did not fail at fallback publication"
    finally:
        _clear_config_caches(config)


def test_goal_judge_helper_does_not_publish_configured_other_provider_during_failed_read(tmp_path, monkeypatch):
    """A live kanban helper uses get_text_auxiliary_client directly, outside the auto wrapper."""
    from agent import auxiliary_client as aux
    from hermes_cli import config

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(
        "model:\n  provider: anthropic\n"
        "auxiliary:\n  goal_judge:\n    provider: gemini\n    model: gemini-2.5-flash\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-no-network")
    _clear_config_caches(config)
    client = None
    try:
        assert config.load_config_with_status()[1] is None
        path.write_text("model: [save in progress\n", encoding="utf-8")
        assert config.current_config_read_failure()
        # The named task route comes from last-good auxiliary config, but the only allowed
        # implicit exception is last-good model.provider (Anthropic), not Gemini.
        assert aux._resolve_task_provider_model("goal_judge")[0] == "gemini"
        client, _ = aux.get_text_auxiliary_client("goal_judge")
        assert client is None, "configured Gemini client escaped after a failed authoritative read"
    finally:
        if client is not None and hasattr(client, "close"):
            client.close()
        _clear_config_caches(config)


def test_vision_helper_does_not_publish_configured_other_provider_during_failed_read(tmp_path, monkeypatch):
    """The explicit/configured vision branch bypasses _vision_auto_route's publication check."""
    from agent import auxiliary_client as aux
    from hermes_cli import config

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(
        "model:\n  provider: anthropic\n"
        "auxiliary:\n  vision:\n    provider: openrouter\n    model: openai/gpt-4o-mini\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-openrouter-key-no-network")
    _clear_config_caches(config)
    client = None
    try:
        assert config.load_config_with_status()[1] is None
        path.write_text("model: [save in progress\n", encoding="utf-8")
        assert config.current_config_read_failure()
        assert aux._resolve_task_provider_model("vision")[0] == "openrouter"
        provider, client, _ = aux.resolve_vision_provider_client()
        assert client is None or provider != "openrouter", (
            "configured OpenRouter vision client escaped after a failed authoritative read"
        )
    finally:
        if client is not None and hasattr(client, "close"):
            client.close()
        _clear_config_caches(config)


def test_tui_cas_rechecks_after_a_contended_windows_replace_retry(tmp_path, monkeypatch):
    """An editor write during the Windows replace backoff must survive a TUI setting write.

    The new pre-replace check has already completed by the time the first os.replace call
    fails with a sharing violation. The editor changes the target during the bounded retry
    delay. If the retry publishes the old merged temp file without another check, data is lost.
    """
    import agent.retry_utils as retry_utils
    import tui_gateway.server as server
    import utils

    home = tmp_path / "home"
    home.mkdir()
    target = home / "config.yaml"
    target.write_text("model:\n  context_length: 32768\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)

    real_replace = utils.os.replace
    real_classifier = utils._is_contended_windows_replace_error
    attempts = []
    external_edits = []

    def first_replace_is_contended(src, dst):
        if str(dst) == str(target):
            attempts.append(1)
            if len(attempts) == 1:
                raise PermissionError(13, "simulated Windows sharing violation", str(target))
        return real_replace(src, dst)

    def classify_contended(exc):
        return (isinstance(exc, PermissionError) and
                "simulated Windows sharing violation" in str(exc)) or real_classifier(exc)

    def editor_writes_during_backoff(*_args, **_kwargs):
        assert attempts == [1], "edit must occur after the first rename attempt"
        if not external_edits:
            external_edits.append(True)
            target.write_text("model:\n  context_length: 65536\n", encoding="utf-8")
        return 0.0

    monkeypatch.setattr(utils.os, "replace", first_replace_is_contended)
    monkeypatch.setattr(utils, "_is_contended_windows_replace_error", classify_contended)
    monkeypatch.setattr(retry_utils, "jittered_backoff", editor_writes_during_backoff)

    server._write_config_key("display.skin", "midnight")
    assert attempts == [1, 1], attempts
    assert external_edits, "the editor interleaving was not reached"
    saved = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert saved["model"]["context_length"] == 65536, saved
    assert saved["display"]["skin"] == "midnight", saved


def test_secondary_tui_theme_command_writes_its_own_profile(tmp_path, monkeypatch):
    """The TUI /theme command sends {key, value}, without its active session_id.

    Its `ctx.gateway.rpc` passes those params unchanged to the backend. A secondary
    session must not write the launch profile's setting when its user runs /theme.
    """
    import tui_gateway.server as server

    launch = tmp_path / "launch"
    worker = tmp_path / "worker"
    launch.mkdir()
    worker.mkdir()
    launch_cfg = launch / "config.yaml"
    worker_cfg = worker / "config.yaml"
    launch_cfg.write_text("display:\n  tui_theme: light\n", encoding="utf-8")
    worker_cfg.write_text("display:\n  tui_theme: auto\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    monkeypatch.setitem(server._sessions, "r4-worker-session", {
        "agent": None, "profile_home": str(worker), "session_key": "r4-worker-session"
    })

    # Adapted: the /theme params as the TUI now sends them (withSessionScope adds the active session).
    response = server._handle_admitted_request({
        "jsonrpc": "2.0", "id": "r4-theme", "method": "config.set",
        "params": {"key": "theme", "value": "dark", "session_id": "r4-worker-session"},
    })
    assert "error" not in response, response
    launch_saved = yaml.safe_load(launch_cfg.read_text(encoding="utf-8"))
    worker_saved = yaml.safe_load(worker_cfg.read_text(encoding="utf-8"))
    assert launch_saved["display"]["tui_theme"] == "light", launch_saved
    assert worker_saved["display"]["tui_theme"] == "dark", worker_saved
