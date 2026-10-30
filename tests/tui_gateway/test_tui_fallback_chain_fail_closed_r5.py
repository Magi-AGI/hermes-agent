"""R5-2: the TUI/desktop chain constructor must fail closed on a failed config read.

``tui_gateway/agent_callbacks.py::_load_fallback_model`` builds the chain for every NEW desktop/TUI
agent from ``_load_cfg()``, which is documented fail-open (``contextlib.suppress(Exception)`` →
``{}``). That result is passed into agent construction at ``tui_gateway/server.py:2524``
(``fallback_model=_load_fallback_model()``) and lands at
``agent/chat_completion_helpers.try_activate_fallback`` with no A44 gate.

A failed MANAGED read is a failed read (A44). The admin layer can only ever REMOVE or override what
the user file says, so applying an unreadable managed file as an empty overlay hands a brand-new
desktop agent the user's **unconstrained** chain — precisely the chain a managed
``fallback_providers: []`` was suppressing.

The per-turn sync (``_sync_agent_fallback_with_config``, ``agent_callbacks.py:330``) IS fail-closed,
but it only corrects an **existing** agent: it returns early when ``session["agent"] is None``, so it
can never repair the chain a new agent was born with. That asymmetry is pinned below, because it is
the reason narrowing the docstring does not close this item.

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no live TUI server, no agent ever constructed.
"""

from __future__ import annotations

import pytest

from tui_gateway import server

_USER_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
    "  - provider: deepinfra\n    model: other/model\n"
)
# The managed layer NARROWS the user chain, so the managed answer is distinguishable from the user's
# own chain by content (the R4-8 discrimination standard).
_MANAGED_NARROWS = "fallback_providers:\n  - provider: openrouter\n    model: some/model\n"
_NARROWED = [{"provider": "openrouter", "model": "some/model"}]
_USER_FULL = [{"provider": "openrouter", "model": "some/model"},
              {"provider": "deepinfra", "model": "other/model"}]
_MANAGED_BROKEN = "fallback_providers: [unclosed\n"


def _clear_config_caches():
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def _clear_caches():
    """``_clear_config_caches`` plus every retention slot (a fresh process's state).

    Tolerant of the slots being absent so a red-first run fails on the BEHAVIOUR under test rather
    than on a missing symbol.
    """
    from gateway import run_config_loaders
    _clear_config_caches()
    for attr in ("_LAST_CLEAN_FALLBACK_CHAIN", "_LAST_CLEAN_FALLBACK_CHAIN_AT",
                 "_LAST_CLEAN_FALLBACK_CHAIN_EPOCH"):
        slot = getattr(run_config_loaders, attr, None)
        if slot is not None:
            slot.clear()


@pytest.fixture
def tui_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    # The production seam every TUI config read goes through (same shape
    # tests/tui_gateway/test_fallback_chain_hot_reload.py uses).
    monkeypatch.setattr(server, "_active_config_path", lambda: home / "config.yaml")
    monkeypatch.setattr(server, "_hermes_home", home, raising=False)
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


def _new_agent_chain():
    """The chain a NEW desktop/TUI agent would be constructed with (``server.py:2524``)."""
    _clear_config_caches()
    return server._load_fallback_model()


def test_the_tui_constructor_applies_the_managed_narrowing_when_the_config_reads_cleanly(tui_home):
    """The positive arm: a fix must honour the admin layer, not disable failover wholesale."""
    home, managed = tui_home
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")

    chain = _new_agent_chain()
    assert chain == _NARROWED, f"the TUI constructor did not apply the managed narrowing: {chain!r}"
    assert chain != _USER_FULL, "the TUI constructor served the user's unconstrained chain"


def test_a_cold_tui_agent_gets_no_chain_when_the_managed_config_does_not_read(tui_home):
    """R5-2(tui_gateway): a failed managed read must not publish the user's unconstrained chain.

    Nothing is retained for this home (a cold process), so the only honest answer is "no chain" —
    the same direction ``GatewayRunner._load_fallback_model`` fails in.
    """
    home, managed = tui_home
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    chain = _new_agent_chain()
    assert chain != _USER_FULL, (
        f"a new desktop/TUI agent was constructed with the user's unconstrained chain ({chain!r}) "
        "while the managed config did not read — the suppression was applied as an empty overlay")
    assert not chain, f"a cold TUI agent must get no chain on a failed read, got {chain!r}"

    # The ungated activation seam therefore gets nothing to install.
    from agent.agent_init import _fallback_entries
    from agent.chat_completion_helpers import try_activate_fallback

    entries = _fallback_entries(chain)
    assert entries == [], f"the new TUI agent was handed a chain: {entries!r}"

    class _Agent:
        provider = "anthropic"
        model = "claude-sonnet-4"
        base_url = "https://api.anthropic.com"
        _fallback_chain = entries
        _fallback_index = 0
        _fallback_activated = False
        _rate_limited_until = 0
        _primary_runtime: dict = {}

    agent = _Agent()
    assert try_activate_fallback(agent) is False
    assert agent.provider == "anthropic", "an unconstrained fallback provider was installed in the TUI"


def test_the_per_turn_sync_cannot_repair_a_new_agents_chain(tui_home):
    """Why "the per-turn sync is fail-closed" does not close this item.

    ``_sync_agent_fallback_with_config`` returns immediately when the session has no agent yet, so
    the fail-closed per-turn path never runs for the construction case. The constructor itself has to
    fail closed; this arm pins that reasoning so a later reader does not re-credit the sync.
    """
    home, managed = tui_home
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    session: dict = {"agent": None, "session_key": "r5-2"}
    # No exception, no correction: the sync is a no-op before an agent exists.
    assert server._sync_agent_fallback_with_config("sid", session) is None
    assert session["agent"] is None

    chain = _new_agent_chain()
    assert not chain, (
        f"the construction path served {chain!r}; the per-turn sync cannot correct it because the "
        "agent does not exist yet")


def test_the_tui_constructor_serves_the_last_clean_managed_answer_across_a_later_failure(tui_home):
    """Fail-closed must not mean "destroy the chain": the retained clean answer is served.

    Reuses the SAME per-home retention the gateway constructor already owns
    (``_record_clean_fallback_chain`` / ``_retained_fallback_chain``) rather than a third cache.
    """
    home, managed = tui_home
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")

    assert _new_agent_chain() == _NARROWED

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches()  # only the config caches: the retention IS the state under test

    chain = _new_agent_chain()
    assert chain == _NARROWED, (
        f"the clean managed answer was not retained for the next TUI agent: {chain!r}")
    assert chain != _USER_FULL, "the user's unconstrained chain was served instead of the retained one"
