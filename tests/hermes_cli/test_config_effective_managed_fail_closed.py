"""CA-P2/P3-6, round 2: ``load_user_config_effective(fail_closed=True)`` must fail closed on a
MANAGED file failure, not only a user-file failure.

``config_effective.py`` raised only for ``snap.user_error``. ``managed_error`` — produced by the
same observation (``config._load_config_cache_sig``) and honoured by ``load_config`` — had no
reference in that module, so a managed file that exists but does not read was applied as an EMPTY
overlay and the user-only config was returned as if the admin layer said nothing.

Why that is a privilege change and not cosmetics: a managed ``fallback_providers: []`` SUPPRESSES
a user chain. Once the managed file is malformed, every fail-closed resync caller
(``cli_chat_turn_mixin._sync_fallback_chain_with_config``,
``gateway/run_config_loaders`` fallback refresh) reads the suppressed user chain as live policy and
installs it. Fail-closed callers hold their own last-known-good state, so raising is exactly the
contract they asked for.

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network.
"""

from __future__ import annotations

import pytest


def _clear_caches():
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


_USER_WITH_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
)


def test_a_malformed_managed_file_makes_the_fail_closed_effective_loader_raise(homes):
    """A managed file that does not parse is a failed read, so ``fail_closed=True`` must raise."""
    home, managed = homes
    (home / "config.yaml").write_text(_USER_WITH_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text("fallback_providers: []\n", encoding="utf-8")
    _clear_caches()

    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.fallback_config import get_fallback_chain

    # Baseline: the admin layer suppresses the user chain, and the loader reports it.
    assert get_fallback_chain(load_user_config_effective(fail_closed=True)) == []

    (managed / "config.yaml").write_text("fallback_providers: [unclosed\n", encoding="utf-8")
    _clear_caches()

    with pytest.raises(Exception):
        load_user_config_effective(fail_closed=True)


def test_a_fail_closed_resync_cannot_install_a_chain_the_admin_layer_suppressed(homes):
    """The concrete consequence: the CLI's per-turn resync must keep the suppressed chain.

    ``_sync_fallback_chain_with_config`` calls the loader with ``fail_closed=True`` and keeps its
    current chain when it raises. While the managed failure was invisible, the loader handed back
    the user's chain and the resync installed a provider the admin layer had removed.
    """
    home, managed = homes
    (home / "config.yaml").write_text(_USER_WITH_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text("fallback_providers: []\n", encoding="utf-8")
    _clear_caches()

    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.fallback_config import get_fallback_chain

    assert get_fallback_chain(load_user_config_effective(fail_closed=True)) == []

    class _Surface(CLIChatTurnMixin):
        def __init__(self):
            self._fallback_model = []

    class _Agent:
        _fallback_chain: list = []
        _fallback_model = None
        _fallback_activated = False
        _rate_limited_until = 0

    surface, agent = _Surface(), _Agent()
    (managed / "config.yaml").write_text("fallback_providers: [unclosed\n", encoding="utf-8")
    _clear_caches()

    surface._sync_fallback_chain_with_config(agent)

    assert surface._fallback_model == [], (
        "a malformed managed file let the resync install the chain the admin layer suppressed")
    assert agent._fallback_chain == [], (
        "the agent was given a fallback provider the managed layer had removed")
