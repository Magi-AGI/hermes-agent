"""CA-P2-2: the publication rule checks the PIN'S IDENTITY (REPORT-ROUND5, kept in scope by Lake
2026-09-30).

Two holes, both only reachable during a failed authoritative config read:
1. ``selection == "config"`` returned "allowed" BEFORE comparing the provider with the served
   last-known-good pin, so a caller could publish any provider by labelling its pick "config".
2. ``_provider_matches_pin``'s named-custom branch asked "is the PIN a configured providers:
   entry?" and ignored WHICH entry the provider named, so with several named custom providers a
   pin on ``alpha`` authorised ``custom:beta``.

Fake credentials only; no network. Red on 60448cd8e7 + the three MCP P2 commits.
"""

from __future__ import annotations

def _clear_config_caches(config):
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()


def test_selection_config_still_checks_the_pins_identity(tmp_path, monkeypatch):
    """``selection == "config"`` must authorise the PIN, not any provider that claims to be it.

    The rule returns None (allowed) for ``selection == "config"`` before comparing ``provider``
    with the served last-known-good pin, so a caller that labels its pick "config" publishes a
    provider the config never pinned during a failed read.
    """
    from hermes_cli import auth, config

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  provider: anthropic\n  default: claude-sonnet-4\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_config_caches(config)
    try:
        assert config.load_config_with_status()[1] is None
        path.write_text("model: [save in progress\n", encoding="utf-8")
        assert config.current_config_read_failure(), "the config did not fail to read"

        # The pin is anthropic. A DIFFERENT provider carrying the "config" label must be refused.
        assert auth.provider_blocked_by_config_failure("gemini", "config"), (
            "selection='config' published gemini while the last-known-good config pins anthropic: "
            "the rule never compared the pin's identity")
        # The real pin still passes.
        assert auth.provider_blocked_by_config_failure("anthropic", "config") is None
    finally:
        _clear_config_caches(config)


_TWO_NAMED_CUSTOM = (
    "providers:\n"
    "  alpha:\n    base_url: http://127.0.0.1:18081/v1\n    api_key: fake-alpha-no-network\n"
    "  beta:\n    base_url: http://127.0.0.1:18082/v1\n    api_key: fake-beta-no-network\n"
)


def test_named_custom_pin_does_not_authorise_a_different_named_custom_provider(tmp_path, monkeypatch):
    """A bare ``providers:``-key pin ``alpha`` must not authorise ``custom:beta``.

    ``_provider_matches_pin``'s named-custom branch answers "is *pin* a configured ``providers:``
    entry?" and ignores WHICH entry the provider names, so with several named custom providers a
    pin on one authorises every other one during a failed read.
    """
    from hermes_cli import auth, config

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(
        "model:\n  provider: alpha\n  default: alpha-model\n" + _TWO_NAMED_CUSTOM,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_config_caches(config)
    try:
        loaded, failure = config.load_config_with_status()
        assert failure is None
        assert auth.config_pinned_provider(loaded) == "alpha", "the bare providers-key pin changed shape"
        path.write_text("model: [save in progress\n", encoding="utf-8")
        assert config.current_config_read_failure()

        # The pinned entry still passes.
        assert auth.provider_blocked_by_config_failure("custom:alpha", None) is None
        # A DIFFERENT named custom endpoint must not ride the alpha pin.
        assert auth.provider_blocked_by_config_failure("custom:beta", None), (
            "a named-custom pin 'alpha' authorised 'custom:beta' during a failed read")
    finally:
        _clear_config_caches(config)


def test_bare_custom_runtime_still_matches_the_named_custom_pin(tmp_path, monkeypatch):
    """The documented widening stays, but only where the name is RECOVERABLE.

    The runtime reports plain ``custom`` for a ``providers:`` entry, so the pin has to authorise the
    nameless form — otherwise a named-custom install could never publish during a failed read. With
    ONE configured entry the nameless form can only be that entry, so it passes.

    Round 2 (CA-P2-2 follow-up): this test used to seed one entry and assert the allowance was
    unconditional, which also blessed the ``alpha``-pin/``custom:beta`` bypass on the production
    path (every named endpoint resolves to the literal ``custom``). The narrow contract is pinned
    here; the ambiguous and endpoint-identified cases are in
    ``test_config_failure_pin_identity_resolution.py``.
    """
    from hermes_cli import auth, config

    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(
        "model:\n  provider: alpha\n  default: alpha-model\n"
        "providers:\n  alpha:\n    base_url: http://127.0.0.1:18081/v1\n    api_key: fake-alpha\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_config_caches(config)
    try:
        assert config.load_config_with_status()[1] is None
        path.write_text("model: [save in progress\n", encoding="utf-8")
        assert config.current_config_read_failure()
        assert auth.provider_blocked_by_config_failure("custom", None) is None, (
            "the bare custom runtime lost its named-custom pin")
        # And with its endpoint supplied, the identity is exact rather than inferred.
        assert auth.provider_blocked_by_config_failure(
            "custom", None, base_url="http://127.0.0.1:18081/v1") is None
    finally:
        _clear_config_caches(config)
