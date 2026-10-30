"""Codex closure r3 probes on magi/config-auth (review-artifacts/config-auth/codex-closure-r3/).

Verbatim unless marked. Adaptations: probe 1's precondition now expects a configured
``model.provider: auto`` to classify as "auto" (it is discovery, not a pin: the fix); the init-time
probe accepts a corrupt_config refusal as "no client"; the two-home characterization is inverted to the
fixed contract; the replace-syscall race is a strict xfail (a proposed accepted limit, see REPORT-ROUND4).
Fake credentials only; temporary homes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml

def test_model_provider_auto_is_not_a_last_known_good_pin_at_final_read(tmp_path, monkeypatch):
    """A configured 'auto' is still discovery, even if it is written in the provider field.

    This targets the source classification used by resolve_runtime_provider's final guard.
    The config breaks while the OpenRouter env-key rung is deciding the provider.
    """
    from hermes_cli import auth, config, runtime_provider

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  provider: auto\n  default: test-model\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fake-no-network")
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    assert runtime_provider._requested_provider_and_source() == ("auto", "auto")  # adapted: auto is not a pin
    observations = []

    def fail_during_env_pick(_read_env):
        observations.append(True)
        path.write_text("model: [save in progress\n", encoding="utf-8")
        return True

    monkeypatch.setattr(auth, "_openrouter_auto_detected", fail_during_env_pick)
    try:
        with pytest.raises(auth.AuthError) as error:
            runtime_provider.resolve_runtime_provider()
        assert error.value.code == "corrupt_config"
        assert observations and config.current_config_read_failure()
    finally:
        for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                      config._LAST_EXPANDED_CONFIG_BY_PATH):
            cache.clear()


@pytest.fixture
def env_derived_runtime(tmp_path, monkeypatch):
    """A real main runtime selected from env before an editor starts saving."""
    from hermes_cli import config, runtime_provider

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  default: gemini-2.5-flash\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-no-network")
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    provider, source = runtime_provider._requested_provider_and_source()
    assert (provider, source) == ("gemini", "env")
    runtime = runtime_provider.resolve_runtime_provider()
    assert runtime["provider"] == "gemini"
    path.write_text("model: [save in progress\n", encoding="utf-8")
    assert config.current_config_read_failure(), "the authoritative config must be unreadable"
    yield runtime
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()


def test_new_auxiliary_client_does_not_reuse_env_derived_main_route_after_failed_read(
        env_derived_runtime, monkeypatch):
    """The main turn exists, but this call constructs a NEW auxiliary client after failure.

    Lake's accepted continuation is a provider pinned by the last-known-good config; this
    clean-built main runtime was chosen from HERMES_INFERENCE_PROVIDER and has no such pin.
    """
    from agent import auxiliary_client as aux

    new_client = SimpleNamespace()
    constructions = []

    def construct(provider, model, **kwargs):
        constructions.append((provider, model))
        return new_client, model

    monkeypatch.setattr(aux, "resolve_provider_client", construct)
    monkeypatch.setattr(aux, "_compression_client_allowed", lambda *_a: True)
    client, _model, route = aux._resolve_auto_route(main_runtime=env_derived_runtime, task="title")
    assert client is None and route == "", (
        "constructed a new auxiliary route from an env-selected main provider after a failed "
        f"authoritative read: {constructions}, {route}"
    )


def test_new_vision_client_does_not_reuse_env_derived_main_route_after_failed_read(
        env_derived_runtime, monkeypatch):
    """The same early-main-route bypass exists in the vision auto router."""
    from agent import auxiliary_client as aux

    new_client = SimpleNamespace()
    constructions = []

    def construct(provider, model, runtime, resolved_model, resolved_api_mode):
        constructions.append((provider, model))
        return new_client, model

    monkeypatch.setattr(aux, "_vision_main_provider_client", construct)
    provider, client, _model = aux._vision_auto_route(env_derived_runtime, None, None, False)
    assert provider is None and client is None, (
        "constructed a new vision route from an env-selected main provider after a failed "
        f"authoritative read: {constructions}, {provider}"
    )


def test_init_time_primary_route_does_not_reuse_env_derived_provider_after_failed_read(
        env_derived_runtime, monkeypatch):
    """The new init-time fallback guard runs AFTER the primary router has made a client."""
    from agent import agent_init, auxiliary_client as aux
    from hermes_cli import auth

    new_client = SimpleNamespace()
    constructions = []

    def construct(provider, model, **kwargs):
        constructions.append((provider, model))
        return new_client, model

    monkeypatch.setattr(aux, "resolve_provider_client", construct)
    monkeypatch.setattr(agent_init, "_client_kwargs_from_routed", lambda *_a: {"selected": "gemini"})
    agent = SimpleNamespace(provider=env_derived_runtime["provider"],
                            model="gemini-2.5-flash", _fallback_activated=False)
    try:
        kwargs = agent_init._routed_client_kwargs(agent, [], None)
    except auth.AuthError as error:  # adapted: a corrupt_config refusal is "no client"
        assert error.code == "corrupt_config"
        kwargs = None
    assert kwargs is None, (
        "built an init-time client from the env-derived primary provider after the "
        f"authoritative config read failed: {constructions}, {kwargs}"
    )


def test_auxiliary_discovery_rechecks_failure_after_client_selection(tmp_path, monkeypatch):
    """A config that breaks DURING the real discovery loop cannot yield its new route."""
    from agent import auxiliary_client as aux
    from hermes_cli import config

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("agent:\n  max_turns: 5\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    new_client = SimpleNamespace()
    constructions = []

    def pick_from_api_key():
        constructions.append(True)
        path.write_text("model: [save in progress\n", encoding="utf-8")
        return new_client, "gemini-2.5-flash"

    # Keep the actual provider-chain loop and its route publication. Force earlier rungs
    # unavailable so the env-key rung is the one that observes the concurrent save.
    monkeypatch.setattr(aux, "_try_openrouter", lambda **_kw: (None, None))
    monkeypatch.setattr(aux, "_try_nous", lambda **_kw: (None, None))
    monkeypatch.setattr(aux, "_try_custom_endpoint", lambda: (None, None))
    monkeypatch.setattr(aux, "_resolve_api_key_provider", pick_from_api_key)
    monkeypatch.setattr(aux, "_is_provider_unhealthy", lambda *_a: False)
    monkeypatch.setattr(aux, "_compression_client_allowed", lambda *_a: True)
    client, _model, route = aux._resolve_auto_route(main_runtime={"provider": ""}, task="title")
    assert config.current_config_read_failure()
    assert client is None and route == "", (
        "returned an implicitly discovered provider after the config became unreadable "
        "during selection"
    )


def test_last_known_good_switch_refuses_implicit_main_runtime_on_failure(
        env_derived_runtime, monkeypatch):
    """The policy switch must also constrain auxiliary reuse of a non-pinned main runtime."""
    from agent import auxiliary_client as aux
    from hermes_cli import auth

    monkeypatch.setattr(auth, "REFUSE_LAST_KNOWN_GOOD_PROVIDER", True)
    selections = []

    def construct(provider, model, **kwargs):
        selections.append(provider)
        return SimpleNamespace(), model

    monkeypatch.setattr(aux, "resolve_provider_client", construct)
    monkeypatch.setattr(aux, "_compression_client_allowed", lambda *_a: True)
    client, _model, route = aux._resolve_auto_route(main_runtime=env_derived_runtime, task="title")
    assert client is None and route == ""

@pytest.mark.parametrize("root", ["[]\n", "false\n", "0\n", "null\n"])
def test_falsy_nonmapping_managed_root_is_failed_read(tmp_path, monkeypatch, root):
    """A present managed config with a nonmapping root cannot be clean absence."""
    from hermes_cli import auth, config, managed_scope

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    (managed / "config.yaml").write_text(root, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    managed_scope.invalidate_managed_cache()
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    try:
        observation = managed_scope.read_managed_config()
        assert observation.error_kind == "parse", observation
        _value, failure = config.load_config_with_status()
        assert failure is not None and failure.kind == "parse", failure
        with pytest.raises(auth.AuthError, match="settings|config|provider"):
            auth.resolve_provider(skip_free_tier=True)
    finally:
        managed_scope.invalidate_managed_cache()
        config._LOAD_CONFIG_CACHE.clear()
        config._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def test_blank_managed_config_is_empty_mapping_control(tmp_path, monkeypatch):
    """A truly empty file can remain the intentional no-policy control."""
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        observed = managed_scope.read_managed_config()
        assert observed.value == {} and observed.error is None, observed
    finally:
        managed_scope.invalidate_managed_cache()


@pytest.mark.xfail(strict=True, reason="Proposed accepted limit (REPORT-ROUND4): an external write landing inside the rename syscall itself cannot be detected without a conditional replace or a lock shared with arbitrary editors; the pre-rename recheck narrows the window to that syscall.")
def test_tui_cas_does_not_overwrite_external_edit_at_atomic_replace(tmp_path, monkeypatch):
    """An editor can write after the CAS check but before the writer replaces the file."""
    import tui_gateway.server as server
    import utils

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  context_length: 32768\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)

    real_replace = utils.atomic_replace
    injected = []

    def edit_at_commit(tmp_name, target):
        if str(target) == str(path) and not injected:
            injected.append(True)
            path.write_text("model:\n  context_length: 65536\n", encoding="utf-8")
        return real_replace(tmp_name, target)

    monkeypatch.setattr(utils, "atomic_replace", edit_at_commit)
    server._write_config_key("display.skin", "midnight")
    assert injected, "the commit seam was not exercised"
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["model"]["context_length"] == 65536, saved
    assert saved["display"]["skin"] == "midnight", saved


def test_managed_directory_replacement_honors_vanished_grace(tmp_path, monkeypatch):
    """Moving the managed directory should not skip the known-file grace entirely."""
    from hermes_cli import config, managed_scope

    home = tmp_path / "home"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    (managed / "config.yaml").write_text("model:\n  provider: gemini\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    try:
        assert config.load_config_with_status()[0]["model"]["provider"] == "gemini"
        managed.rename(tmp_path / "managed-old")
        _value, failure = config.load_config_with_status()
        assert failure is not None and failure.kind == "read", failure
    finally:
        managed_scope.invalidate_managed_cache()
        config._LOAD_CONFIG_CACHE.clear()
        config._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def test_config_mtime_revision_is_declared_in_rpc_contract(tmp_path, monkeypatch):
    """The new A43 revision must survive strict JSON-RPC result validation."""
    from tui_gateway import server
    from tui_gateway.contracts.registry import METHODS

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("display:\n  bell_on_complete: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    result = server._handle_admitted_request(
        {"jsonrpc": "2.0", "id": "mtime-probe", "method": "config.get", "params": {"key": "mtime"}}
    )
    assert result["result"]["rev"]
    METHODS["config.get"].result.model_validate(result["result"])

def _world(tmp_path, monkeypatch):
    import tui_gateway.server as server

    launch = tmp_path / "launch"
    worker = tmp_path / "worker"
    launch.mkdir()
    worker.mkdir()
    (launch / "config.yaml").write_text("display:\n  bell_on_complete: false\n", encoding="utf-8")
    (worker / "config.yaml").write_text("display:\n  bell_on_complete: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(server, "_hermes_home", launch)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_sig", None)
    monkeypatch.setattr(server, "_cfg_path", None)
    monkeypatch.setitem(server._sessions, "worker-session", {
        "agent": None, "profile_home": str(worker), "session_key": "worker-session",
    })
    return server, launch, worker


def _rpc(server, key, *, session_id=None):
    params = {"key": key}
    if session_id is not None:
        params["session_id"] = session_id
    frame = server._handle_admitted_request(
        {"jsonrpc": "2.0", "id": "two-home", "method": "config.get", "params": params}
    )
    return frame["result"]


def test_session_scoped_config_get_reads_the_sessions_own_profile(tmp_path, monkeypatch):
    """Inverted two-home characterization: with the session id the TUI now sends, both ``full`` and
    ``mtime`` answer for the session's profile, not the launch profile."""
    server, _launch, _worker = _world(tmp_path, monkeypatch)
    launch_full, worker_full = _rpc(server, "full"), _rpc(server, "full", session_id="worker-session")
    launch_mtime, worker_mtime = _rpc(server, "mtime"), _rpc(server, "mtime", session_id="worker-session")
    assert launch_full["config"]["display"]["bell_on_complete"] is False
    assert worker_full["config"]["display"]["bell_on_complete"] is True
    assert launch_mtime["rev"] != worker_mtime["rev"]


def test_session_scoped_mtime_rev_changes_when_only_worker_config_changes(tmp_path, monkeypatch):
    """Even adding session_id to the TUI request does not fix the mtime/rev getter."""
    server, _launch, worker = _world(tmp_path, monkeypatch)
    before = _rpc(server, "mtime", session_id="worker-session")
    (worker / "config.yaml").write_text("display:\n  bell_on_complete: false\n", encoding="utf-8")
    after = _rpc(server, "mtime", session_id="worker-session")
    assert before["rev"] != after["rev"], (before, after)


def test_tui_cas_recheck_catches_an_edit_after_the_merge_before_the_rename(tmp_path, monkeypatch):
    """The pre-rename recheck: an edit landing after the compare-and-swap read and the merge but before
    the rename is detected, and the transaction re-reads and re-applies on top of it."""
    import tui_gateway.server as server
    import utils

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  context_length: 32768\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    real_mode = utils._preserve_file_mode
    injected = []

    def edit_after_merge(p):
        if str(p) == str(path) and not injected:
            injected.append(True)
            path.write_text("model:\n  context_length: 65536\n", encoding="utf-8")
        return real_mode(p)

    monkeypatch.setattr(utils, "_preserve_file_mode", edit_after_merge)
    server._write_config_key("display.skin", "midnight")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert injected and saved["model"]["context_length"] == 65536 and saved["display"]["skin"] == "midnight"


@pytest.mark.parametrize("pin, provider, allowed", [
    ("anthropic", "anthropic", True), ("claude", "anthropic", True), ("anthropic", "gemini", False),
    ("", "gemini", False), ("auto", "openrouter", False)])
def test_one_rule_allows_only_the_pin_or_an_explicit_choice(tmp_path, monkeypatch, pin, provider, allowed):
    from hermes_cli import auth, config

    home = tmp_path / "home"
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text(f"model:\n  provider: {pin}\n" if pin else "agent:\n  max_turns: 5\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE, config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    try:
        assert config.load_config_with_status()[1] is None
        assert auth.provider_blocked_by_config_failure(provider) is None  # clean read: anything goes
        cfg.write_text("model: [broken\n", encoding="utf-8")
        assert (auth.provider_blocked_by_config_failure(provider) is None) is allowed
        assert auth.provider_blocked_by_config_failure(provider, "explicit") is None
    finally:
        for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE, config._LAST_EXPANDED_CONFIG_BY_PATH):
            cache.clear()
