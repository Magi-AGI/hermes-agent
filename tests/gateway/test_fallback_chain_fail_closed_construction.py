"""CA-P2/P3-6, round 3: the gateway's fallback-chain CONSTRUCTOR must read fail-closed.

Round 2 armed the raise inside ``config_effective.load_user_config_effective(fail_closed=True)``.
Only ``GatewayRunner._refresh_fallback_model`` passes that flag. The other chain source --
``GatewayRunner._load_fallback_model`` -> ``gateway.run._load_gateway_config`` (self-labelled
"fail-open") -> ``load_user_config_effective`` with no flag -- applied an unreadable MANAGED file as
an EMPTY overlay and returned the user's unconstrained chain as success.

That reader is used precisely where there is no confirmed runtime lock
(``gateway/platforms/api_server.py``: ``"fallback_model": None if confirmed_runtime_lock else
GatewayRunner._load_fallback_model()``), so after a managed ``fallback_providers: []`` became
malformed a newly created API agent received the chain the admin layer had suppressed -- and
``agent.chat_completion_helpers.try_activate_fallback`` installs chain entries with no A44 gate of
its own.

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no live gateway.
"""

from __future__ import annotations

import pytest

_USER_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
)
_MANAGED_SUPPRESSES = "fallback_providers: []\n"
_MANAGED_BROKEN = "fallback_providers: [unclosed\n"


def _clear_config_caches():
    """Drop the config-read caches so the next read sees the file on disk."""
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def _clear_caches():
    """``_clear_config_caches`` plus the retained-clean-chain slot (a fresh process's state).

    Tolerant of the slot being absent so a red-first run on the unfixed tree fails on the
    BEHAVIOUR under test rather than on a missing symbol.
    """
    from gateway import run_config_loaders
    _clear_config_caches()
    retained = getattr(run_config_loaders, "_LAST_CLEAN_FALLBACK_CHAIN", None)
    if retained is not None:
        retained.clear()


@pytest.fixture
def homes(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


def test_a_malformed_managed_file_does_not_hand_the_static_reader_the_user_chain(homes):
    """The suppressed chain must stay suppressed once the managed file stops reading.

    ``_load_fallback_model`` is the chain source for every agent created without a confirmed
    runtime lock. While the managed failure was invisible it returned the user's own
    ``fallback_providers`` -- the entries the admin layer had removed.
    """
    home, managed = homes
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    _clear_caches()

    from gateway.run import GatewayRunner

    assert GatewayRunner._load_fallback_model() is None, "the managed suppression did not apply"

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()

    chain = GatewayRunner._load_fallback_model()
    assert chain is None, (
        f"a malformed managed file handed the constructor the user's unconstrained chain: {chain!r}")


def test_a_previously_clean_chain_is_retained_across_a_later_managed_failure(homes):
    """Fail-closed must not mean "destroy the chain": the last clean answer is kept.

    A managed file that stops reading says nothing about the chain the admin layer last allowed, so
    the reader keeps serving that one rather than dropping to an empty chain and silently disabling
    failover for every agent created afterwards.
    """
    home, managed = homes
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    # The managed layer permits the user chain (no fallback key at all).
    (managed / "config.yaml").write_text("telemetry: false\n", encoding="utf-8")
    _clear_caches()

    from gateway.run import GatewayRunner

    clean = GatewayRunner._load_fallback_model()
    assert clean == [{"provider": "openrouter", "model": "some/model"}]

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    # Only the config-read caches: the retained slot IS the state under test here.
    _clear_config_caches()

    assert GatewayRunner._load_fallback_model() == clean, (
        "a later managed failure destroyed the chain the admin layer had already allowed")


def test_a_new_api_agent_does_not_receive_a_chain_the_admin_layer_suppressed(homes):
    """End to end at the named seam: agent construction, then mid-turn activation.

    ``try_activate_fallback`` installs a chain entry with no publication/pin gate of its own, so the
    only thing standing between a malformed managed file and an unpinned route being activated is
    what the constructor handed the agent.
    """
    home, managed = homes
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    _clear_caches()

    from gateway.run import GatewayRunner
    assert GatewayRunner._load_fallback_model() is None

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()

    from agent.agent_init import _fallback_entries
    from agent.chat_completion_helpers import try_activate_fallback

    chain = _fallback_entries(GatewayRunner._load_fallback_model())
    assert chain == [], f"the new agent was handed the suppressed chain: {chain!r}"

    class _Agent:
        provider = "anthropic"
        model = "claude-sonnet-4"
        base_url = "https://api.anthropic.com"
        _fallback_chain = chain
        _fallback_index = 0
        _fallback_activated = False
        _rate_limited_until = 0
        _primary_runtime: dict = {}

    agent = _Agent()
    assert try_activate_fallback(agent) is False
    assert agent.provider == "anthropic", "an unpublished fallback provider was installed"
