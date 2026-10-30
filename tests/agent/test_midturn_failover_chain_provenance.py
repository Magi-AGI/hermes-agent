"""CA-P2/P3-6: mid-turn failover is not re-gated by the A44 config-read rule.

Classification (2026-10-05, with evidence): **P3 deferred known limit**, not a P2 deploy blocker.

``agent/chat_completion_helpers.py::try_activate_fallback`` walks ``agent._fallback_chain``
mid-turn and swaps provider/model/client in place without calling
``runtime_provider.fallback_chain_walk_allowed`` or ``auth.provider_blocked_by_config_failure``,
which DO gate every resolution-time walker (``runtime_provider.py``:1174, ``cron/scheduler.py``:1726,
``hermes_cli/cli_agent_setup_mixin.py``:367).

Why that is not a P2: A44 refuses adopting a route from OUTSIDE the config (env keys, credential
pool, login, free tier, ``HERMES_INFERENCE_PROVIDER``, discovery) while the config does not read
cleanly. A mid-turn chain entry is INSIDE the config and was read CLEANLY -- the chain is a pure
projection of the user's own ``fallback_providers``/``fallback_model`` keys
(``hermes_cli/fallback_config.py::get_fallback_chain``), and every per-turn re-sync that could
replace it reads with ``fail_closed=True`` and keeps the last known-good chain rather than letting
a torn file change it (``hermes_cli/cli_chat_turn_mixin.py::_sync_fallback_chain_with_config``,
``gateway/run_config_loaders.py::_apply_fallback_chain_to_agent``). So a failed read can neither
insert an entry nor silently empty the chain, and mid-turn failover can only reach a provider the
user wrote down and Hermes read cleanly.

These tests pin the two properties the deferral RESTS ON, so the justification cannot rot
silently: if either becomes false, this is a P2 again.
"""

from __future__ import annotations


def test_the_fallback_chain_is_only_a_projection_of_the_users_own_config_keys():
    """No entry can enter the chain from the environment or from discovery."""
    from hermes_cli.fallback_config import get_fallback_chain

    assert get_fallback_chain({}) == []
    assert get_fallback_chain(None) == []
    chain = get_fallback_chain({
        "fallback_providers": [
            {"provider": "gemini", "model": "gemini-2.5-flash"},
            {"provider": "openrouter", "model": "openai/gpt-4o-mini"},
        ]
    })
    assert [e["provider"] for e in chain] == ["gemini", "openrouter"], (
        "the chain is no longer the config's own ordered entries")


def test_a_torn_config_does_not_change_a_live_agents_fallback_chain(monkeypatch):
    """The per-turn re-sync is fail-closed: a failed read keeps the last known-good chain.

    This is what makes mid-turn failover safe without re-gating: the chain the mid-turn walker
    reads can only have come from a clean read.
    """
    from hermes_cli import config_effective

    known_good = [{"provider": "gemini", "model": "gemini-2.5-flash"}]

    class _Agent:
        _fallback_chain = list(known_good)
        _fallback_model = known_good[0]
        _fallback_activated = False
        _fallback_index = 0
        _rate_limited_until = 0

    agent = _Agent()

    def _torn(*_args, **kwargs):
        # fail_closed=True is the contract the sync relies on: the loader RAISES rather than
        # returning an empty/partial config that would read as "the user removed the chain".
        assert kwargs.get("fail_closed") is True, (
            "the fallback-chain sync stopped reading the config fail-closed")
        raise RuntimeError("config.yaml is mid-save")

    monkeypatch.setattr(config_effective, "load_user_config_effective", _torn)

    # The CLI sync swallows the failure and returns BEFORE touching the agent's chain.
    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin

    holder = CLIChatTurnMixin.__new__(CLIChatTurnMixin)
    CLIChatTurnMixin._sync_fallback_chain_with_config(holder, agent)

    assert agent._fallback_chain == known_good, (
        "a torn config.yaml changed a live agent's fallback chain; mid-turn failover could then "
        "reach an entry that was never read cleanly, which would make CA-P2/P3-6 a P2 again")
