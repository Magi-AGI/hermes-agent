"""R4-1: identity CARDINALITY must not be read off a display-deduplicated list view.

``auth._custom_endpoint_entries`` enumerated through
``config_providers.get_compatible_custom_providers``, whose dedupe key is ``(name, base_url, model)``
and which therefore drops a later entry matching that triple REGARDLESS of its provider key or its
credentials. Two separately keyed ``providers:`` entries that happen to share a display ``name:`` and
an endpoint collapsed into one row, and the pin check — which asks "how many distinct custom
identities are enabled?" — read that one row as "exactly one":

- rung 2 (``auth.py``, the endpoint lookup) saw ``len(matched) == 1``, so the SHARED endpoint read as
  uniquely the first entry and the second entry's runtime satisfied the first entry's pin;
- rung 3 (the documented one-entry widening) saw a single name, so the NAMELESS ``custom`` route was
  admitted on the pinned entry's name while two identities were enabled.

The shipped R3 ambiguity probes miss this because their fixture omits ``name:``
(``test_config_failure_pin_identity_r3.py``): with no display name the dedupe key is never hit, so
both entries survive the view and the ambiguity is seen. These probes add the display name back.

The fix is a dedicated enumeration (``config_providers.custom_endpoint_identities``) deduplicated by
canonical identity and nothing else. It must NOT regress R3-1, which was closed by routing the
enumeration through a filter that requires a usable ``base_url`` so registry-provider credentials
stored under ``providers:`` (``providers: {anthropic: {api_key: ...}}``) are not counted as rival
custom endpoints — the last probe here pins both halves at once.

Real loader + real resolution ladder against a temp HERMES_HOME. Fake credentials, loopback URLs,
no network.
"""

from __future__ import annotations

import pytest

_BROKEN = "model: [save in progress\n"

# Two DIFFERENT keyed entries with the SAME display name, endpoint and (absent) model, and DIFFERENT
# credentials. The display-deduplicated view keeps only `alpha`.
_SHARED_DISPLAY_NAME = (
    "providers:\n"
    "  alpha:\n    name: shared\n    base_url: http://127.0.0.1:18081/v1\n"
    "    api_key: fake-alpha-no-network\n"
    "  beta:\n    name: shared\n    base_url: http://127.0.0.1:18081/v1\n"
    "    api_key: fake-beta-no-network\n"
)
_SHARED_ENDPOINT = "http://127.0.0.1:18081/v1"


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
def shared_display_name(tmp_path, monkeypatch):
    path = _pinned_home(tmp_path, monkeypatch, _SHARED_DISPLAY_NAME)
    try:
        yield path
    finally:
        _clear_caches()


def test_two_entries_sharing_a_display_name_are_two_identities(shared_display_name):
    """The enumeration the pin check uses must see BOTH keyed entries.

    This is the mechanism under the two behavioural probes below: with the display-deduplicated view
    as the primitive, ``beta`` is simply absent, and every downstream cardinality question is then
    answered about a config that does not exist.
    """
    from hermes_cli import auth, config

    loaded, failure = config.load_config_with_status()
    assert failure is None

    names = auth._enabled_custom_entry_names(loaded)
    assert names is not None, "the entry list did not read"
    assert sorted(names) == ["alpha", "beta"], (
        f"a shared display name erased the second custom identity: {names!r}")


def test_a_shared_endpoint_is_ambiguous_even_when_the_entries_share_a_display_name(
        shared_display_name):
    """Rung 2: the endpoint maps to TWO entries, so it identifies neither — refuse.

    The credentials differ, so the URL alone cannot say which runtime this is. With ``beta`` deduped
    out of the enumeration the lookup returned exactly one match and beta's runtime rode alpha's pin.
    """
    from hermes_cli import auth, config

    shared_display_name.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure(), "the config did not fail to read"

    assert auth.provider_blocked_by_config_failure("custom", None, base_url=_SHARED_ENDPOINT), (
        "an endpoint shared by two entries with one display name resolved to the first and rode "
        "its pin")


def test_the_nameless_route_is_refused_when_two_entries_share_a_display_name(shared_display_name):
    """Rung 3: two enabled identities means the nameless form is genuinely ambiguous — refuse.

    The one-entry widening is documented for the case where nothing ELSE could be meant. With the
    second identity deduped away the widening fired on a two-identity install and admitted a bare
    ``custom`` route on alpha's pin.
    """
    from hermes_cli import auth, config

    shared_display_name.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure()

    assert auth.provider_blocked_by_config_failure("custom", None), (
        "the one-entry widening admitted the nameless route while two identities were enabled")


def test_the_reachable_fallback_route_on_the_shared_display_name_is_refused(shared_display_name):
    """The production path: a saved fallback entry carrying beta's own endpoint and credentials.

    ``requested="custom"`` + ``request_origin="fallback"`` + an explicit base_url takes the
    bare-custom rung in ``runtime_provider_custom``, which never calls
    ``_apply_custom_provider_extras``, so NO ``custom_identity`` is stamped and the comparison must
    answer from the endpoint alone. Fallback provenance keeps publication implicit, so the A44 gate
    applies — and it said "allowed" because the enumeration had lost beta.
    """
    from hermes_cli import auth, config, runtime_provider

    shared_display_name.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure()

    with pytest.raises(auth.AuthError) as excinfo:
        runtime_provider.resolve_runtime_provider(
            requested="custom", request_origin="fallback",
            explicit_base_url=_SHARED_ENDPOINT, explicit_api_key="fake-beta-no-network")
    assert excinfo.value.code == "corrupt_config", (
        "beta's runtime published itself on alpha's pin because the two entries shared a display "
        "name")


def test_a_registry_provider_is_still_not_a_rival_entry_under_the_new_enumeration(
        tmp_path, monkeypatch):
    """R3-1 must not regress: the discriminator is a usable base_url, not display identity.

    R3-1 was closed by enumerating through a filter that requires a ``base_url``, which is why
    ``providers: {anthropic: {api_key: ...}}`` stopped counting as a second custom endpoint. The
    R4-1 fix replaces the *dedupe*, not that filter, so one change has to satisfy both: count every
    distinct keyed endpoint entry, exclude keyless-URL registry entries.
    """
    path = _pinned_home(
        tmp_path, monkeypatch,
        "providers:\n"
        "  alpha:\n    name: shared\n    base_url: http://127.0.0.1:18081/v1\n"
        "    api_key: fake-alpha-no-network\n"
        "  anthropic:\n    api_key: fake-anthropic-no-network\n")
    from hermes_cli import auth, config
    try:
        loaded, failure = config.load_config_with_status()
        assert failure is None
        assert auth._enabled_custom_entry_names(loaded) == ["alpha"], (
            "a registry provider under providers: was counted as a custom endpoint")

        path.write_text(_BROKEN, encoding="utf-8")
        assert config.current_config_read_failure()
        assert auth.provider_blocked_by_config_failure("custom", None) is None, (
            "the documented single-endpoint widening was refused on a one-endpoint install")
    finally:
        _clear_caches()
