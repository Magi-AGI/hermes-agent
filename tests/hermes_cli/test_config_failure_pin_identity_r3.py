"""CA-P2-2, round 3: identity recovery must REFUSE on an unmatched or ambiguous read.

Round 2 recovered the identity on two rungs (endpoint lookup, then a one-entry widening). Two
reachable paths survived, both in ``auth._provider_matches_pin``'s bare-``custom`` branch:

(a) an endpoint that maps to NO configured entry returned ``None`` from the lookup and then fell
    through to ``return not others`` -- so with a single configured entry, a bare ``custom`` route at
    an unrelated URL satisfied that entry's pin. A failed read is the wrong moment to treat "I could
    not identify this route" as "it must be the pinned one".
(b) two entries sharing one ``base_url`` with different credentials: the lookup matched on the
    normalised URL only and returned the FIRST entry, so beta's runtime was identified as alpha and
    satisfied alpha's pin.

Both are closed by carrying the RESOLVED entry's canonical identity with the runtime and by treating
unmatched and ambiguous the same way as each other: refuse.

The last probe pins the R3-1 behaviour drift in the opposite direction: a registry provider
legitimately configured under ``providers:`` (``providers: {anthropic: ...}``, written by the model
picker and read by doctor) is not a custom endpoint and must not make the documented single-entry
widening ambiguous.

Real loader + real resolution ladder against a temp HERMES_HOME. Fake credentials, loopback URLs,
no network.
"""

from __future__ import annotations

import pytest

_BROKEN = "model: [save in progress\n"

_ONE_ENTRY = (
    "providers:\n"
    "  alpha:\n    base_url: http://127.0.0.1:18081/v1\n    api_key: fake-alpha-no-network\n"
)
# Two DIFFERENT entries, same endpoint, different credentials: the URL alone cannot tell them apart.
_SHARED_ENDPOINT = (
    "providers:\n"
    "  alpha:\n    base_url: http://127.0.0.1:18081/v1\n    api_key: fake-alpha-no-network\n"
    "  beta:\n    base_url: http://127.0.0.1:18081/v1\n    api_key: fake-beta-no-network\n"
)
_UNMAPPED_ENDPOINT = "http://127.0.0.1:18099/v1"


def _clear_caches():
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def _pinned_home(tmp_path, monkeypatch, providers_yaml: str, pin: str = "alpha"):
    """Seed a home whose config reads CLEANLY with ``model.provider: <pin>``, then return its path."""
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(f"model:\n  provider: {pin}\n  default: {pin}-model\n" + providers_yaml,
                    encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    _clear_caches()
    from hermes_cli import auth, config
    loaded, failure = config.load_config_with_status()
    assert failure is None, "the seeded config did not read cleanly"
    assert auth.config_pinned_provider(loaded) == pin
    return path


@pytest.fixture
def one_entry(tmp_path, monkeypatch):
    path = _pinned_home(tmp_path, monkeypatch, _ONE_ENTRY)
    try:
        yield path
    finally:
        _clear_caches()


@pytest.fixture
def shared_endpoint(tmp_path, monkeypatch):
    path = _pinned_home(tmp_path, monkeypatch, _SHARED_ENDPOINT)
    try:
        yield path
    finally:
        _clear_caches()


def test_a_bare_custom_route_at_an_unmapped_endpoint_does_not_satisfy_a_named_pin(one_entry):
    """An endpoint that maps to no configured entry identifies nothing, so it cannot be the pin.

    The single-entry widening answers "the nameless form can only mean alpha" -- which is true only
    while nothing else identifies the route. Once a base_url IS supplied and it belongs to no
    configured entry, the route is positively NOT alpha, and ``return not others`` admitted it.
    """
    from hermes_cli import auth, config

    one_entry.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure(), "the config did not fail to read"

    assert auth.provider_blocked_by_config_failure("custom", None, base_url=_UNMAPPED_ENDPOINT), (
        "a bare custom route at an endpoint belonging to no configured entry rode alpha's pin")
    # The documented widening itself is untouched: with no endpoint to judge, one entry still wins.
    assert auth.provider_blocked_by_config_failure("custom", None) is None
    # And the pinned entry's own endpoint still resolves exactly.
    assert auth.provider_blocked_by_config_failure(
        "custom", None, base_url="http://127.0.0.1:18081/v1") is None


def test_the_saved_fallback_route_at_an_unmapped_endpoint_is_refused(one_entry):
    """The production path for (a): a SAVED fallback entry carrying its own base_url.

    ``request_origin="fallback"`` wins over the explicit-URL branch, so publication stays implicit
    and the A44 gate applies -- the gate just said "allowed" because the URL identified nothing.
    """
    from hermes_cli import auth, config, runtime_provider

    one_entry.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure()

    with pytest.raises(auth.AuthError) as excinfo:
        runtime_provider.resolve_runtime_provider(
            requested="custom", request_origin="fallback",
            explicit_base_url=_UNMAPPED_ENDPOINT, explicit_api_key="fake-no-network")
    assert excinfo.value.code == "corrupt_config", (
        "a saved fallback entry at an unconfigured endpoint published itself on the named pin")


def test_two_entries_sharing_one_endpoint_are_not_silently_matched_to_the_first(shared_endpoint):
    """(b): the URL is ambiguous, so it must not be read as "the first match".

    ``find_custom_provider_identity`` matches on the normalised ``base_url`` only and returns the
    first hit with no credential tie-break, so beta's runtime was identified as alpha.
    """
    from hermes_cli import auth, config

    shared_endpoint.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure()

    assert auth.provider_blocked_by_config_failure(
        "custom", None, base_url="http://127.0.0.1:18081/v1"), (
        "an endpoint shared by alpha and beta was resolved to alpha and rode its pin")
    # Two entries also means the nameless form with no endpoint is ambiguous (round-2 contract).
    assert auth.provider_blocked_by_config_failure("custom", None)


def test_the_resolved_entrys_identity_decides_when_the_endpoint_cannot(shared_endpoint):
    """The resolver knows WHICH entry it picked; that identity must travel with the runtime.

    Same shared endpoint, so the URL cannot discriminate. Resolving ``beta`` must still be refused
    against alpha's pin, and resolving ``alpha`` must still be allowed -- the identity, not the URL,
    is what the comparison uses.
    """
    from hermes_cli import auth, config, runtime_provider

    shared_endpoint.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure()

    with pytest.raises(auth.AuthError) as excinfo:
        runtime_provider.resolve_runtime_provider(requested="beta", request_origin="fallback")
    assert excinfo.value.code == "corrupt_config", (
        "beta's runtime published itself on alpha's pin because both entries share one endpoint")

    runtime = runtime_provider.resolve_runtime_provider(requested="alpha", request_origin="fallback")
    assert runtime["provider"] == "custom"
    assert runtime.get("custom_identity") == "custom:alpha", (
        "the resolver did not carry the entry identity it picked onto the runtime")


def test_a_registry_provider_under_providers_is_not_a_second_custom_endpoint(tmp_path, monkeypatch):
    """R3-1: ``providers:`` holds registry providers too; counting them narrows the widening.

    The model picker writes registry credentials under ``providers:`` (``model_switch_providers``)
    and doctor reads them there, so ``providers: {alpha: <endpoint>, anthropic: {api_key: ...}}`` is
    an ordinary single-custom-endpoint install. Counting ``anthropic`` as a custom entry made the
    nameless form "ambiguous" and refused the exact route the widening exists to allow.
    """
    path = _pinned_home(
        tmp_path, monkeypatch,
        "providers:\n"
        "  alpha:\n    base_url: http://127.0.0.1:18081/v1\n    api_key: fake-alpha-no-network\n"
        "  anthropic:\n    api_key: fake-anthropic-no-network\n")
    from hermes_cli import auth, config
    try:
        path.write_text(_BROKEN, encoding="utf-8")
        assert config.current_config_read_failure()
        assert auth.provider_blocked_by_config_failure("custom", None) is None, (
            "a registry provider configured under providers: was counted as a rival custom endpoint")
    finally:
        _clear_caches()
