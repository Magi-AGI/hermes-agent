"""CA-P2-2, round 2: the publication rule must compare the pin against the identity the
PRODUCTION resolution path actually produces.

``runtime_provider_custom._custom_runtime`` builds every named endpoint's runtime as the literal
provider ``"custom"`` (the resolved billing class), so the configured entry name is gone before
``auth._provider_matches_pin`` runs. The bare-``custom`` allowance in that comparison then admits
the nameless form against ANY named custom pin, which re-opens the exact bypass the round-1 fix
claimed to close: with ``providers: {alpha, beta}`` and ``model.provider: alpha``, a ``beta``
fallback resolved while the config does not read cleanly published itself on alpha's pin.

Both probes go through the real loader + the real resolution ladder against a temp HERMES_HOME.
Fake credentials, loopback URLs, no network.
"""

from __future__ import annotations

import pytest

_TWO_NAMED_CUSTOM = (
    "providers:\n"
    "  alpha:\n    base_url: http://127.0.0.1:18081/v1\n    api_key: fake-alpha-no-network\n"
    "  beta:\n    base_url: http://127.0.0.1:18082/v1\n    api_key: fake-beta-no-network\n"
)
_BROKEN = "model: [save in progress\n"


def _clear_caches():
    from hermes_cli import config, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    from hermes_cli import config_effective
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


@pytest.fixture
def pinned_alpha(tmp_path, monkeypatch):
    """A clean read of ``model.provider: alpha`` with alpha+beta configured, then a torn file."""
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  provider: alpha\n  default: alpha-model\n" + _TWO_NAMED_CUSTOM,
                    encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    _clear_caches()
    from hermes_cli import config
    loaded, failure = config.load_config_with_status()
    assert failure is None, "the seeded config did not read cleanly"
    from hermes_cli import auth
    assert auth.config_pinned_provider(loaded) == "alpha"
    try:
        yield path
    finally:
        _clear_caches()


def test_a_named_custom_fallback_cannot_ride_a_different_entrys_pin(pinned_alpha):
    """The production path: a ``beta`` fallback resolved during a failed read must be refused.

    ``request_origin="fallback"`` is a SAVED chain entry, not this run's ``--provider``, so the
    publication check sees ``selection == "fallback"`` and has to compare identities. The runtime
    it judges reports ``provider == "custom"``; its configured identity survives only in
    ``requested_provider`` / the endpoint, and the rule must use that.
    """
    from hermes_cli import auth, config, runtime_provider

    pinned_alpha.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure(), "the config did not fail to read"

    with pytest.raises(auth.AuthError) as excinfo:
        runtime_provider.resolve_runtime_provider(requested="beta", request_origin="fallback")
    assert excinfo.value.code == "corrupt_config", (
        "a custom:beta fallback published itself on the alpha pin during a failed read")

    # The pinned entry itself still resolves: the rule authorises the pin, it does not refuse
    # every custom route.
    runtime = runtime_provider.resolve_runtime_provider(requested="alpha", request_origin="fallback")
    assert runtime["provider"] == "custom"
    assert runtime["base_url"].rstrip("/").endswith(":18081/v1")


def test_a_nameless_custom_route_is_refused_while_two_entries_could_be_meant(pinned_alpha):
    """With several named custom entries, the bare ``custom`` form names none of them.

    The documented widening (a named pin authorises the bare ``custom`` runtime it resolves to)
    only holds while the pin is the ONLY entry the nameless form could denote. With alpha and beta
    both configured it is ambiguous, and an ambiguous identity must not satisfy a named pin during
    a failed read.
    """
    from hermes_cli import auth, config

    pinned_alpha.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure()

    assert auth.provider_blocked_by_config_failure("custom", None), (
        "a nameless custom route satisfied the alpha pin while beta was equally configured")
    # An explicitly identified route is judged on its identity, either way.
    assert auth.provider_blocked_by_config_failure("custom:beta", None)
    assert auth.provider_blocked_by_config_failure("custom:alpha", None) is None
