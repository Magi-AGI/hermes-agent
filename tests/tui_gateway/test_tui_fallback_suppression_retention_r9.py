"""R9/R5-2 residual: a TUI per-turn suppression must reach the SHARED retention.

The R8 review's static counterexample, executed. ``_sync_agent_fallback_with_config``
(``tui_gateway/agent_callbacks.py``) is on the NORMAL turn admission path
(``tui_gateway/prompt_turn.py:587``) and already reads the config fail-closed — but it read it
through ``get_fallback_chain(load_user_config_effective(...))`` **directly**, bypassing
``gateway/run_config_loaders.load_fail_closed_fallback_chain``. So a suppression it observed was
applied to THAT agent only and never published to the per-home retention the constructors read:

1. a TUI constructor reads the allowed chain ``F`` and records it in the shared retention;
2. the managed config changes to ``fallback_providers: []``; an existing session's next turn
   observes the suppression and clears that agent's chain — and publishes nothing;
3. the managed file then stops reading; within ``FALLBACK_CHAIN_RETENTION_GRACE_SECONDS`` the next
   TUI constructor retrieves the unchanged ``F`` from the retention and hands it to a new agent
   (``tui_gateway/server.py:2524``), where ``try_activate_fallback`` installs it with no gate.

No race and no CLI source is needed. The existing R5 TUI coverage goes clean -> broken DIRECTLY
(``test_tui_fallback_chain_fail_closed_r5.py``), which is why it misses the intervening suppression.

The two contracts that must SURVIVE the fix are asserted here too, because the obvious
over-correction (publish unconditionally) breaks them:
- a FAILED read must still keep the live agent's current chain (#95066, the reason this sync exists);
- a clean NON-empty observation must publish as well, so the publication is not empty-only.

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
_MANAGED_NARROWS_FURTHER = "fallback_providers:\n  - provider: deepinfra\n    model: other/model\n"
_MANAGED_SUPPRESSES = "fallback_providers: []\n"
_MANAGED_BROKEN = "fallback_providers: [unclosed\n"

_NARROWED = [{"provider": "openrouter", "model": "some/model"}]
_NARROWED_FURTHER = [{"provider": "deepinfra", "model": "other/model"}]
_USER_FULL = [{"provider": "openrouter", "model": "some/model"},
              {"provider": "deepinfra", "model": "other/model"}]


def _clear_config_caches():
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def _clear_caches():
    """``_clear_config_caches`` plus every retention slot (a fresh process's state)."""
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
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


def _new_agent_chain():
    """The chain a NEW desktop/TUI agent would be constructed with (``server.py:2524``)."""
    _clear_config_caches()
    return server._load_fallback_model()


class _Agent:
    """The fields ``_apply_fallback_chain_to_agent`` reads and writes."""

    def __init__(self, chain):
        self._fallback_chain = list(chain or [])
        self._fallback_model = self._fallback_chain[0] if self._fallback_chain else None
        self._fallback_activated = False
        self._fallback_index = 0
        self._rate_limited_until = 0


def _turn_refresh(home, agent):
    """One NORMAL turn's fallback sync (``prompt_turn.py:587`` calls exactly this)."""
    _clear_config_caches()
    session = {"agent": agent, "session_key": "r9"}
    server._sync_agent_fallback_with_config("sid", session)


def test_a_suppression_seen_by_a_normal_turn_is_not_served_to_the_next_new_agent(tui_home):
    """The R8 counterexample, end to end: F -> turn sees suppression -> managed breaks -> no chain.

    The assertion that matters is the LAST one: a brand-new agent must not be handed ``F``, the
    chain the admin layer revoked while this process was watching.
    """
    home, managed = tui_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")

    # 1. a constructor records the allowed chain F in the shared retention.
    assert _new_agent_chain() == _NARROWED, "the positive arm broke before the counterexample ran"

    # 2. the admin layer revokes it, and an existing session's next turn observes that.
    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    agent = _Agent(_NARROWED)
    _turn_refresh(home, agent)
    assert agent._fallback_chain == [], (
        "the per-turn sync did not even apply the suppression to the live agent; the fail-closed "
        "read it already had is broken, which is a different defect")

    # 3. the managed file stops reading, well within the retention grace.
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    chain = _new_agent_chain()
    assert chain != _NARROWED, (
        f"a new TUI agent was constructed with {chain!r} — the chain the admin layer revoked while "
        "this process was running. The turn refresh observed the suppression but never published it "
        "to the shared per-home retention, so the constructor served the pre-revocation record")
    assert chain != _USER_FULL, "the user's unconstrained chain was served"
    assert not chain, f"a revoked chain must leave no chain behind, got {chain!r}"


def test_the_suppression_the_turn_observed_is_what_the_activation_seam_cannot_install(tui_home):
    """The consequence the P2 severity rests on: nothing reaches the ungated activation seam.

    ``agent/chat_completion_helpers.try_activate_fallback`` walks ``agent._fallback_chain`` only, so
    an empty chain hits the exhaustion guard. That is only true if the constructor above yielded
    nothing.
    """
    home, managed = tui_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    assert _new_agent_chain() == _NARROWED

    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    _turn_refresh(home, _Agent(_NARROWED))
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    from agent.agent_init import _fallback_entries
    from agent.chat_completion_helpers import try_activate_fallback

    entries = _fallback_entries(_new_agent_chain())
    assert entries == [], f"the new TUI agent was handed a revoked chain: {entries!r}"

    class _Candidate:
        provider = "anthropic"
        model = "claude-sonnet-4"
        base_url = "https://api.anthropic.com"
        _fallback_chain = entries
        _fallback_index = 0
        _fallback_activated = False
        _rate_limited_until = 0
        _primary_runtime: dict = {}

    candidate = _Candidate()
    assert try_activate_fallback(candidate) is False
    assert candidate.provider == "anthropic", "a revoked fallback provider was installed in the TUI"


def test_a_clean_non_empty_turn_observation_also_reaches_the_shared_retention(tui_home):
    """Publication must not be empty-only: a narrowed chain seen per-turn is the record too.

    Otherwise the retention still answers with a pre-edit chain for every narrowing that is not a
    full suppression — the same bug class, one step weaker.
    """
    home, managed = tui_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    assert _new_agent_chain() == _NARROWED

    # The admin layer swaps WHICH single provider is allowed; a turn observes it.
    (managed / "config.yaml").write_text(_MANAGED_NARROWS_FURTHER, encoding="utf-8")
    agent = _Agent(_NARROWED)
    _turn_refresh(home, agent)
    assert agent._fallback_chain == _NARROWED_FURTHER

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    chain = _new_agent_chain()
    assert chain == _NARROWED_FURTHER, (
        f"the retention served {chain!r}; the turn refresh's clean non-empty observation was not "
        "published, so the constructor answered with the superseded chain")


def test_the_turn_refresh_still_keeps_the_live_chain_when_the_read_fails(tui_home):
    """#95066's contract, preserved: a failed read is not "chain removed".

    This is the arm an over-correction breaks. The sync must publish only SUCCESSFUL observations;
    on a failed read it keeps the agent's current chain and leaves the retention alone.
    """
    home, managed = tui_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    assert _new_agent_chain() == _NARROWED

    from gateway import run_config_loaders
    from hermes_constants import hermes_home_key

    home_key = hermes_home_key(home)
    before = run_config_loaders._LAST_CLEAN_FALLBACK_CHAIN.get(home_key)
    epoch_before = run_config_loaders._LAST_CLEAN_FALLBACK_CHAIN_EPOCH.get(home_key)

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    agent = _Agent(_NARROWED)
    _turn_refresh(home, agent)

    assert agent._fallback_chain == _NARROWED, (
        f"a failed config read wiped the live agent's chain ({agent._fallback_chain!r}); the "
        "fail-closed sync must keep the last known-good chain, not read a failure as a removal")
    assert run_config_loaders._LAST_CLEAN_FALLBACK_CHAIN.get(home_key) == before, (
        "a FAILED read published to the shared retention; only successful observations may")
    assert run_config_loaders._LAST_CLEAN_FALLBACK_CHAIN_EPOCH.get(home_key) == epoch_before, (
        "a FAILED read advanced the retention's observation epoch")


def test_the_turn_refresh_publication_carries_an_observation_epoch(tui_home):
    """R5-3's ordering must cover this writer too, or it can lose to a descheduled reader.

    A publication with no epoch cannot be refused by ``_record_clean_fallback_chain``, so the turn
    refresh would be able to overwrite a strictly newer observation — exactly the race R5-3 closed
    for the other two writers.
    """
    home, managed = tui_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    assert _new_agent_chain() == _NARROWED

    from gateway import run_config_loaders
    from hermes_constants import hermes_home_key

    home_key = hermes_home_key(home)
    epoch_before = run_config_loaders._LAST_CLEAN_FALLBACK_CHAIN_EPOCH.get(home_key)
    assert epoch_before is not None, "the constructor itself recorded no epoch (R5-3 regression)"

    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    _turn_refresh(home, _Agent(_NARROWED))

    epoch_after = run_config_loaders._LAST_CLEAN_FALLBACK_CHAIN_EPOCH.get(home_key)
    assert epoch_after is not None and epoch_after > epoch_before, (
        f"the turn refresh published without advancing the observation epoch "
        f"({epoch_before!r} -> {epoch_after!r}); an unordered publication can overwrite a newer one")
