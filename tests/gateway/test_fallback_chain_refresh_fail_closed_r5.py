"""R5-1: the per-turn fallback refresh must be home-bound and fail closed.

``GatewayRunner._refresh_fallback_model`` (``gateway/run_config_loaders.py``) reads the chain for the
ACTIVE gateway home on every turn and hands the result straight to agent construction
(``gateway/run_turn_runner.py``: ``fallback_model=self._runner._refresh_fallback_model()``). On a
failed read it consults the shared per-home retention — but when nothing is retained for that home
(``_NO_RETENTION``) it used to fall through and return ``self._fallback_model`` **unchanged**. That
field is a single instance slot, not keyed by ``hermes_home_key``, so the fall-through has two
consequences, both of which these tests pin:

1. **Expiry restores a known-revoked chain.** The refresh observes chain ``F``; the static
   constructor then observes a clean managed suppression for the SAME home; the managed file breaks;
   past ``FALLBACK_CHAIN_RETENTION_GRACE_SECONDS`` the retention reports ``_NO_RETENTION`` and the
   refresh serves ``F`` again. So the R4-6 grace failed closed on the static reader
   (``_load_fallback_model`` → ``None``) and fails OPEN on the refresh reader — the opposite of the
   invariant R4-2/R4-6 claim.
2. **Cross-home leak on a failed FIRST refresh.** Home A refreshes to ``F``; routing moves the same
   runner to home B, whose config does not read cleanly and for which nothing is retained; the
   refresh hands home B home A's chain. The module's own comment (``:42-57``) says the retention dict
   is keyed per home precisely to prevent this.

The existing A→B→A arm in ``test_fallback_chain_retention_r4.py`` only exercises the STATIC
constructor; this file is the refresh reader's coverage.

Clock injection follows the shape already used by ``test_fallback_chain_retention_r4.py``
(``monkeypatch.setattr(mod.time, "monotonic", ...)``) rather than replacing the module's ``time``
object, because this module reads other ``time`` attributes elsewhere.

Temp HERMES_HOME + HERMES_MANAGED_DIR, two homes for the A→B rule, no network, no live gateway.
"""

from __future__ import annotations

import pytest

_USER_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
    "  - provider: deepinfra\n    model: other/model\n"
)
# The managed layer NARROWS the user chain, so a retained/stale answer is distinguishable from the
# user's own unconstrained chain by content (the R4-8 discrimination standard).
_MANAGED_NARROWS = "fallback_providers:\n  - provider: openrouter\n    model: some/model\n"
_NARROWED = [{"provider": "openrouter", "model": "some/model"}]
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

    Tolerant of the slots being absent so a red-first run fails on the BEHAVIOUR under test rather
    than on a missing symbol.
    """
    from gateway import run_config_loaders
    _clear_config_caches()
    for attr in ("_LAST_CLEAN_FALLBACK_CHAIN", "_LAST_CLEAN_FALLBACK_CHAIN_AT"):
        slot = getattr(run_config_loaders, attr, None)
        if slot is not None:
            slot.clear()


class _Runner:
    """Just enough of GatewayRunner to exercise the refresh path (the mixin is bound via the MRO).

    One instance across both homes on purpose: under multiplexing ONE runner serves many profiles,
    which is exactly why a non-home-bound instance slot leaks.
    """

    _fallback_model = None

    def __init__(self):
        from gateway.run_config_loaders import GatewayConfigLoadersMixin
        self._refresh = GatewayConfigLoadersMixin._refresh_fallback_model.__get__(self, _Runner)

    def refresh(self):
        return self._refresh()


@pytest.fixture
def one_home(tmp_path, monkeypatch):
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


def test_expired_retention_after_a_suppression_makes_the_refresh_serve_no_chain(one_home, monkeypatch):
    """R5-1(A): past the grace the refresh must fail closed, not resurrect its own stale answer.

    The static reader already fails closed here (``_load_fallback_model`` returns ``None`` once the
    retention expires — ``test_fallback_chain_retention_r4`` pins that). The refresh reader must fail
    closed in the SAME direction: a chain the admin layer revoked, whose retained suppression has
    since aged out, is not a chain anyone may serve.

    The advance is a day rather than "the grace + 1" so the arm is about the BEHAVIOUR (the refresh
    stops serving a stale answer) and not about a constant existing.
    """
    from gateway import run_config_loaders

    home, managed = one_home
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    _clear_caches()

    clock = [1000.0]
    monkeypatch.setattr(run_config_loaders.time, "monotonic", lambda: clock[0])

    from gateway.run import GatewayRunner

    # 1. a turn refreshes cleanly: the refresh observes the narrowed chain F and holds it.
    runner = _Runner()
    assert runner.refresh() == _NARROWED, "the refresh did not observe the narrowed chain"

    # 2. the admin layer revokes F; the STATIC constructor is the observer of that suppression.
    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    _clear_config_caches()
    assert GatewayRunner._load_fallback_model() is None, "the constructor did not observe the suppression"

    # 3. the managed file stops reading, and the suppression ages past the retention grace.
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    clock[0] += 86400.0
    _clear_config_caches()

    # The static reader fails closed — the direction the refresh must match.
    assert GatewayRunner._load_fallback_model() is None

    chain = runner.refresh()
    assert chain is None, (
        "past the retention grace the per-turn refresh served its own stale instance chain "
        f"({chain!r}) for a chain the admin layer had already revoked, so the R4-6 grace fails "
        "closed on the constructor and OPEN on the refresh")


def test_a_failed_first_refresh_for_a_second_home_serves_no_chain_not_the_first_homes(tmp_path, monkeypatch):
    """R5-1(B): home B's failed FIRST refresh must never be answered with home A's chain.

    One runner, two homes, A → B. B has never had a clean read, so nothing is retained for it; the
    only honest answer is "no chain". Home B's user file carries a DIFFERENT chain from A's so a leak
    is identifiable by content rather than showing up as a shared empty.
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
    (home_b / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
        "fallback_providers:\n  - provider: groq\n    model: b/model\n", encoding="utf-8")

    def _use(home):
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(gateway_run, "_hermes_home", home)
        _clear_config_caches()

    _clear_caches()
    try:
        runner = _Runner()

        # A refreshes cleanly and the runner holds A's narrowed chain.
        _use(home_a)
        assert runner.refresh() == _NARROWED, "home A's refresh did not observe the narrowed chain"

        # Routing moves to B; B's config does not read cleanly (broken managed layer) and B has
        # never been observed cleanly, so nothing is retained for it.
        _use(home_b)
        (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
        _clear_config_caches()

        chain_b = runner.refresh()
        assert chain_b != _NARROWED, (
            "home B's failed first refresh was answered with home A's retained chain "
            f"({chain_b!r}) — the per-home retention was bypassed by the instance slot")
        assert chain_b is None, (
            f"home B's failed first refresh must serve no chain, got {chain_b!r}")
    finally:
        _clear_caches()
