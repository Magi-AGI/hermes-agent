"""R5-2: cron's chain constructor must fail closed on a failed config read.

``cron/scheduler.py`` builds the chain for every new cron agent from a **fail-open** read:
``_load_cron_job_config`` calls ``load_user_config_effective(Path(_cfg_path))`` with no
``fail_closed`` and swallows every failure to ``{}`` (``logger.warning`` + "using defaults"), and the
resulting ``jc.cfg`` is what ``_resolve_cron_agent_setup`` turns into ``setup.fallback_model``, which
goes straight into ``AIAgent(..., fallback_model=setup.fallback_model)``. From there
``agent/chat_completion_helpers.try_activate_fallback`` installs chain entries with no A44 gate of
its own.

A failed MANAGED read is a failed read (A44): the admin layer can only ever REMOVE or override what
the user file says, so applying an unreadable managed file as an empty overlay hands cron the user's
**unconstrained** chain while reporting success — exactly the chain a managed
``fallback_providers: []`` was suppressing. The gateway's own constructor
(``GatewayRunner._load_fallback_model``) was closed for this in CA-P2/P3-6 round 3; cron was carried
(R4-4) and is closed here.

These tests drive the real ``_load_cron_job_config`` (so the fail-open read at the top of the path is
genuinely exercised) and the real ``_resolve_cron_agent_setup`` (so the chain line that feeds
``AIAgent`` is the one under test). Only the collaborators that would need a live provider, a
credential store or an MCP registry are stubbed; the chain resolution itself is production code.

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no live scheduler, no job ever executed.
"""

from __future__ import annotations

import pytest

_USER_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
    "  - provider: deepinfra\n    model: other/model\n"
)
# The managed layer NARROWS the user chain, so "served the managed answer" is distinguishable from
# "served the user's own chain" by content (the R4-8 discrimination standard).
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
def cron_home(tmp_path, monkeypatch):
    """Temp home + managed dir with cron's heavyweight collaborators stubbed out.

    Everything stubbed here needs a live provider, credential store or MCP registry. The config read
    and the fallback-chain resolution are left as production code — they are the subject.
    """
    from cron import scheduler

    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)

    monkeypatch.setattr(scheduler, "_load_prefill_messages", lambda cfg, job_id: None)
    monkeypatch.setattr(scheduler, "_guard_job_credential_exfil", lambda job: None)
    monkeypatch.setattr(scheduler, "_preflight_or_block", lambda job, job_id, job_name, cfg: None)
    monkeypatch.setattr(scheduler, "_resolve_job_runtime",
                        lambda job, job_id, jc: ({"provider": "anthropic"}, jc.model))
    monkeypatch.setattr(scheduler, "_resolve_job_reasoning_config", lambda job, cfg, model: None)
    monkeypatch.setattr(scheduler, "_load_credential_pool", lambda runtime, job_id: None)
    monkeypatch.setattr(scheduler, "_init_cron_mcp_tools", lambda job_id: None)
    monkeypatch.setattr(scheduler, "_cron_preflight_enabled", lambda cfg: False)

    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


def _cron_chain(home):
    """The chain a NEW cron agent would be constructed with, via the production path."""
    from cron import scheduler

    job = {"id": "job-r5-2", "name": "r5-2", "model": "claude-sonnet-4"}
    _clear_config_caches()
    jc = scheduler._load_cron_job_config(job, job["id"], job["name"])
    setup = scheduler._resolve_cron_agent_setup(job, job["id"], job["name"], jc)
    assert setup.blocked is None, f"the stubbed preflight blocked the run: {setup.blocked!r}"
    return setup.fallback_model


def test_cron_applies_the_managed_narrowing_when_the_config_reads_cleanly(cron_home):
    """The positive arm: cron must honour the admin layer, not serve the user's own chain.

    Without this arm a fix that simply returns ``None`` everywhere would pass the fail-closed arms
    below while silently disabling cron failover.
    """
    home, managed = cron_home
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")

    chain = _cron_chain(home)
    assert chain == _NARROWED, f"cron did not apply the managed narrowing: {chain!r}"
    assert chain != _USER_FULL, "cron served the user's unconstrained chain"


def test_a_cold_cron_agent_gets_no_chain_when_the_managed_config_does_not_read(cron_home):
    """R5-2(cron): a failed managed read must not publish the user's unconstrained chain.

    Nothing is retained for this home (a cold ticker), so the only honest answer is "no chain" —
    the same direction ``GatewayRunner._load_fallback_model`` fails in. The fail-open read instead
    applies the unreadable managed file as an EMPTY overlay and hands the new agent the full user
    chain, which is what the managed layer exists to constrain.
    """
    home, managed = cron_home
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    chain = _cron_chain(home)
    assert chain != _USER_FULL, (
        f"a cron agent was constructed with the user's unconstrained chain ({chain!r}) while the "
        "managed config did not read — the admin layer's suppression was applied as an empty overlay")
    assert chain is None, f"a cold cron agent must get no chain on a failed read, got {chain!r}"

    # The ungated activation seam therefore gets nothing to install.
    from agent.agent_init import _fallback_entries
    from agent.chat_completion_helpers import try_activate_fallback

    entries = _fallback_entries(chain)
    assert entries == [], f"the new cron agent was handed a chain: {entries!r}"

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
    assert agent.provider == "anthropic", "an unconstrained fallback provider was installed in cron"


def test_cron_serves_the_last_clean_managed_answer_across_a_later_managed_failure(cron_home):
    """Fail-closed must not mean "destroy the chain": the retained clean answer is served.

    Same contract (and the SAME per-home retention slots) the gateway constructor already honours —
    ``_record_clean_fallback_chain`` / ``_retained_fallback_chain`` — rather than a third cache.
    """
    home, managed = cron_home
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")

    assert _cron_chain(home) == _NARROWED

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches()  # only the config caches: the retention IS the state under test

    chain = _cron_chain(home)
    assert chain == _NARROWED, (
        f"the clean managed answer was not retained for the next cron agent: {chain!r}")
    assert chain != _USER_FULL, "cron served the user's unconstrained chain instead of the retained one"
