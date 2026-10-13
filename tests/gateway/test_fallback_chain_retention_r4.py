"""R4-2: ONE per-home retention for the fallback chain, shared by both readers.

Two caches held the same fact and never cross-updated:

- ``GatewayRunner._load_fallback_model`` (a ``@staticmethod``, the chain source for every API agent
  created without a confirmed runtime lock) wrote the module global ``_LAST_CLEAN_FALLBACK_CHAIN``
  and read it back on a failed read;
- ``GatewayRunner._refresh_fallback_model`` (an instance method, the per-turn refresh) wrote
  ``self._fallback_model_by_home``.

So this ordering served a REVOKED policy:

1. startup seeds an allowed chain F (``gateway/run.py``'s ``__init__`` calls ``_load_fallback_model``);
2. a legitimate managed edit suppresses F with ``fallback_providers: []``;
3. a messaging turn refreshes successfully — INSTANCE cache only; the static slot still holds F;
4. the managed file becomes unreadable;
5. a new API agent takes the retained-cache return and gets F;
6. the ungated activation seam (``agent/chat_completion_helpers.try_activate_fallback``) installs
   that revoked entry.

Precondition, stated so the test's shape is legible: there must be no API-agent construction between
the suppression and the failure — any such construction would itself have refreshed the static slot
to empty. That is what makes step 3 the only observer of the suppression.

The fix is one retention per home written by BOTH readers, including successful EMPTY observations,
so a clean suppression is retained AS a suppression.

The retained arm is also made DISCRIMINATING here (R4-8): the R3 test's managed layer was
``telemetry: false``, which leaves the clean chain EQUAL to the user chain, so the unfixed base
passed it for the wrong reason. These managed layers narrow the chain instead, so the retained
answer differs from the user's and the arm fails without a working retention path.

Temp HERMES_HOME + HERMES_MANAGED_DIR, two homes for the A→B→A rule, no network, no live gateway.
"""

from __future__ import annotations

import pytest

_USER_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
    "  - provider: deepinfra\n    model: other/model\n"
)
# The managed layer NARROWS the user chain: the clean answer differs from the user's own chain, so
# an implementation that simply serves the user chain cannot pass the retained arm (R4-8).
_MANAGED_NARROWS = (
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
)
_NARROWED = [{"provider": "openrouter", "model": "some/model"}]
_USER_FULL = [{"provider": "openrouter", "model": "some/model"},
              {"provider": "deepinfra", "model": "other/model"}]
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
    """``_clear_config_caches`` plus every retention slot (a fresh process's state).

    Tolerant of the slots being absent so a red-first run on the unfixed tree fails on the BEHAVIOUR
    under test rather than on a missing symbol.
    """
    from gateway import run_config_loaders
    _clear_config_caches()
    for attr in ("_LAST_CLEAN_FALLBACK_CHAIN", "_LAST_CLEAN_FALLBACK_CHAIN_AT"):
        slot = getattr(run_config_loaders, attr, None)
        if slot is not None:
            slot.clear()


class _Runner:
    """Just enough of GatewayRunner to exercise the refresh path (the mixin is bound via the MRO)."""

    _fallback_model = None

    def __init__(self):
        from gateway.run_config_loaders import GatewayConfigLoadersMixin
        self._refresh = GatewayConfigLoadersMixin._refresh_fallback_model.__get__(self, _Runner)

    def refresh(self):
        return self._refresh()


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


def test_a_suppression_observed_through_the_refresh_is_not_resurrected_by_the_constructor(homes):
    """The named ordering, end to end: the revoked chain must not reach a new API agent.

    Steps 1-5 of the module docstring, then the ungated activation seam is asked to install whatever
    the constructor handed over.
    """
    home, managed = homes
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    _clear_caches()

    from gateway.run import GatewayRunner

    # 1. startup seeds the allowed chain F.
    assert GatewayRunner._load_fallback_model() == _NARROWED, "the managed narrowing did not apply"

    # 2. a legitimate managed edit suppresses F.
    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    _clear_config_caches()

    # 3. a messaging turn refreshes successfully -- and nothing else constructs an agent (the stated
    #    precondition), so this refresh is the ONLY observer of the suppression.
    runner = _Runner()
    assert runner.refresh() is None, "the refresh did not observe the suppression"

    # 4. the managed file becomes unreadable.
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches()

    # 5. a new API agent is constructed.
    chain = GatewayRunner._load_fallback_model()
    assert chain is None, (
        f"a suppression seen only by the refresh was resurrected from the static cache: {chain!r}")

    # 6. the ungated activation seam gets nothing to install.
    from agent.agent_init import _fallback_entries
    from agent.chat_completion_helpers import try_activate_fallback

    entries = _fallback_entries(chain)
    assert entries == [], f"the new agent was handed the revoked chain: {entries!r}"

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
    assert agent.provider == "anthropic", "a revoked fallback provider was installed"


def test_a_clean_chain_observed_only_by_the_refresh_is_retained_by_the_constructor(homes):
    """The mirror direction: the retention must be shared, not merely emptied.

    A chain the refresh observed cleanly is the admin layer's current answer, so a later managed
    failure serves THAT, not nothing. This is the arm that proves the two caches were unified rather
    than the static one simply being cleared on every refresh.
    """
    home, managed = homes
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    # The constructor never runs cleanly in this test: the managed file is broken from the start, so
    # the ONLY clean observation of the narrowed chain comes from the refresh below.
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()

    from gateway.run import GatewayRunner

    assert GatewayRunner._load_fallback_model() is None, (
        "a cold process with a broken managed file must serve no chain (fail closed)")

    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    _clear_config_caches()

    runner = _Runner()
    assert runner.refresh() == _NARROWED, "the refresh did not observe the narrowed chain"

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches()

    chain = GatewayRunner._load_fallback_model()
    assert chain == _NARROWED, (
        f"the chain the refresh observed cleanly was not retained for the constructor: {chain!r}")
    assert chain != _USER_FULL, "the user's unconstrained chain was served"


def test_the_retention_is_per_home_across_an_a_b_a_sequence(tmp_path, monkeypatch):
    """Two homes, A -> B -> A: neither home's retention may answer for the other.

    The repo's rule for anything keyed by ``hermes_home_key`` (root ``AGENTS.md``). The R3 test had
    no cross-home arm despite per-home keying being the stated reason for the dict. Home A's managed
    layer narrows to one entry; home B's suppresses entirely, so a leak in either direction is
    visible as the OTHER home's answer rather than as a shared empty.
    """
    import gateway.run as gateway_run

    home_a = tmp_path / "a"
    home_a.mkdir()
    home_b = tmp_path / "b"
    home_b.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    (home_a / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    # B's user file carries a DIFFERENT chain, so a leak is identifiable by content.
    (home_b / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
        "fallback_providers:\n  - provider: groq\n    model: b/model\n", encoding="utf-8")

    def _use(home):
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(gateway_run, "_hermes_home", home)
        _clear_config_caches()

    _clear_caches()
    try:
        from gateway.run import GatewayRunner

        # A: clean, narrowed to one entry.
        _use(home_a)
        assert GatewayRunner._load_fallback_model() == _NARROWED

        # B: the managed layer suppresses; B's own clean answer is "no chain".
        _use(home_b)
        (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
        _clear_config_caches()
        assert GatewayRunner._load_fallback_model() is None, "B did not observe the suppression"

        # Back to A with the managed file broken: A must serve A's retained answer, not B's.
        _use(home_a)
        (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
        _clear_config_caches()
        chain_a = GatewayRunner._load_fallback_model()
        assert chain_a == _NARROWED, (
            f"home A's retained chain was lost or taken from home B: {chain_a!r}")

        # And B must still serve B's suppression, not A's chain.
        _use(home_b)
        chain_b = GatewayRunner._load_fallback_model()
        assert chain_b is None, f"home B was handed home A's retained chain: {chain_b!r}"
    finally:
        _clear_caches()


def test_the_retention_does_not_outlive_its_grace(homes, monkeypatch):
    """R4-6: a permanently broken managed file must not serve the stale chain forever.

    Bounded like the analogue in ``hermes_cli/managed_scope.py``. Past the bound the answer is no
    chain — the same direction a cold process fails in.

    The clock is INJECTED (``monkeypatch.setattr(mod.time, "monotonic", ...)``, the shape
    ``test_codex_closure_r2.py`` uses for ``VANISHED_GRACE_SECONDS``) rather than the module's whole
    ``time`` object being replaced: this module reads other ``time`` attributes elsewhere, and a
    one-method stub would make an unrelated path raise instead of exercising the bound.

    The advance is a day rather than "the configured grace + 1" so the arm is about the BEHAVIOUR
    (retention expires) and not about a constant existing: an unbounded implementation keeps serving
    the chain a day later and fails HERE, rather than erroring on a missing symbol.
    """
    from gateway import run_config_loaders

    home, managed = homes
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    _clear_caches()

    clock = [1000.0]
    monkeypatch.setattr(run_config_loaders.time, "monotonic", lambda: clock[0])

    from gateway.run import GatewayRunner

    assert GatewayRunner._load_fallback_model() == _NARROWED

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches()

    # Within the grace the retained answer still stands — that is the R4-2/BLOCK-2 contract.
    assert GatewayRunner._load_fallback_model() == _NARROWED

    # A day later it must not.
    clock[0] += 86400.0
    _clear_config_caches()

    assert GatewayRunner._load_fallback_model() is None, (
        "the retained chain was still served a day after the config stopped reading, so a "
        "permanently broken managed file keeps the stale chain alive indefinitely")
