"""R5-3: a retention publication must not overwrite a NEWER observation.

``hermes_cli/config_effective.py`` observes the config under ``_config._CONFIG_LOCK``, but the
fallback-chain retention is published **after** that lock is released
(``gateway/run_config_loaders.py``: ``_record_clean_fallback_chain`` on the success paths of
``_load_fallback_model`` and ``_refresh_fallback_model``), and the write unconditionally overwrote
BOTH the value and the timestamp. So this interleaving loses a revocation:

1. reader A's config read completes and observes chain ``F``; A is descheduled before publishing;
2. reader B's config read completes, observes the managed suppression, and publishes it;
3. A resumes and publishes ``F`` with a **fresh** timestamp.

The newer suppression is gone, so the next managed failure serves the revoked ``F`` to a newly
constructed agent. Gateway turns run in a thread pool, so concurrent readers are the normal case.

**Red-first shape.** The assertion is purely behavioural — "after a managed failure the reader serves
the suppression, not the revoked chain" — and uses no symbol that does not already exist at the base
tip, so an unfixed tree fails on the behaviour rather than on an import or attribute error.

**Determinism.** The interleaving is driven by ``threading.Event``, never ``sleep``. The seam is the
module-level ``get_fallback_chain``, which production calls *after* the config read completes and
*before* the retention is published — so blocking there puts a reader exactly between observation and
publication without touching the read itself (whose epoch is the thing the fix must carry). The only
wall-clock bound is a generous 30s join timeout, per the repo's timing rules.

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no live gateway.
"""

from __future__ import annotations

import threading

import pytest

_USER_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
    "  - provider: deepinfra\n    model: other/model\n"
)
# The managed layer NARROWS the user chain, so a resurrected answer is distinguishable from the
# user's own unconstrained chain by content (the R4-8 discrimination standard).
_MANAGED_NARROWS = "fallback_providers:\n  - provider: openrouter\n    model: some/model\n"
_NARROWED = [{"provider": "openrouter", "model": "some/model"}]
_USER_FULL = [{"provider": "openrouter", "model": "some/model"},
              {"provider": "deepinfra", "model": "other/model"}]
_MANAGED_SUPPRESSES = "fallback_providers: []\n"
_MANAGED_BROKEN = "fallback_providers: [unclosed\n"

_JOIN_TIMEOUT_S = 30.0  # generous: a loaded box must not turn this into a flake
_A_THREAD = "r5-3-reader-a"


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


def test_a_stale_observation_does_not_overwrite_a_newer_suppression(homes, monkeypatch):
    """The R5-3 interleaving, deterministically: A observes first, B publishes first, A publishes last.

    A's publication is the LAST write in wall-clock order but carries the OLDER observation, so it
    must not displace B's newer suppression.
    """
    from gateway import run_config_loaders

    home, managed = homes
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    _clear_caches()

    a_observed = threading.Event()
    b_published = threading.Event()
    real_get_chain = run_config_loaders.get_fallback_chain

    def gated_get_fallback_chain(cfg):
        """Production's post-read, pre-publication seam; A parks here, B passes straight through."""
        chain = real_get_chain(cfg)
        if threading.current_thread().name == _A_THREAD:
            a_observed.set()
            assert b_published.wait(_JOIN_TIMEOUT_S), "reader B never published"
        return chain

    monkeypatch.setattr(run_config_loaders, "get_fallback_chain", gated_get_fallback_chain)

    from gateway.run import GatewayRunner

    a_result: list = []
    a_error: list[BaseException] = []

    def reader_a():
        try:
            a_result.append(GatewayRunner._load_fallback_model())
        except BaseException as exc:  # surfaced in the main thread
            a_error.append(exc)
            a_observed.set()

    thread_a = threading.Thread(target=reader_a, name=_A_THREAD)
    thread_a.start()

    # A's config read has completed (it observed the narrowed chain) and A is now parked between
    # observation and publication.
    assert a_observed.wait(_JOIN_TIMEOUT_S), "reader A never reached the publication seam"
    if a_error:
        raise a_error[0]

    # B observes the revocation AFTER A's read and publishes it BEFORE A does.
    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    _clear_config_caches()
    assert GatewayRunner._load_fallback_model() is None, "reader B did not observe the suppression"
    b_published.set()

    thread_a.join(_JOIN_TIMEOUT_S)
    assert not thread_a.is_alive(), f"reader A did not finish within {_JOIN_TIMEOUT_S}s"
    if a_error:
        raise a_error[0]
    assert a_result == [_NARROWED], f"reader A did not observe the narrowed chain: {a_result!r}"

    # Behavioural check, no new symbols: the managed file stops reading, so the reader serves the
    # retained observation. That must be B's suppression, not A's stale chain.
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches()  # only the config caches: the retention IS the state under test

    served = GatewayRunner._load_fallback_model()
    assert served != _NARROWED, (
        "reader A's stale observation overwrote the newer suppression, so a managed failure served "
        f"the revoked chain {served!r} to a newly constructed agent")
    assert served != _USER_FULL, f"the user's unconstrained chain was served: {served!r}"
    assert served is None, f"expected the retained suppression (no chain), got {served!r}"


def test_a_newer_observation_still_replaces_an_older_record(homes):
    """The mirror direction: ordering must not freeze the retention.

    Without this arm an implementation that refuses every overwrite would pass the test above while
    making the retention permanently stale. Sequential, single-threaded, fully behavioural.
    """
    home, managed = homes
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    _clear_caches()

    from gateway.run import GatewayRunner

    assert GatewayRunner._load_fallback_model() is None, "the suppression did not apply"

    # A legitimate later edit re-permits a narrowed chain; that observation is newer and must win.
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    _clear_config_caches()
    assert GatewayRunner._load_fallback_model() == _NARROWED

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches()

    served = GatewayRunner._load_fallback_model()
    assert served == _NARROWED, (
        f"a genuinely newer observation was refused, so the retention is frozen at the older "
        f"suppression: {served!r}")
