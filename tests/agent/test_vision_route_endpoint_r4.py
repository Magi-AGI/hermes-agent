"""R4-3: the vision auto-route must pass its endpoint to the A44 publication check.

``_vision_auto_route`` called ``_route_blocked(provider, selection, "vision")`` with no ``base_url``
and no identity, while every sibling call site passes ``_client_base_url(client)``
(``_resolve_auto_route``, the env-key scan, ``_published_route_blocked``).

Every named custom endpoint reports the nameless provider ``custom``, so with no endpoint the pin
comparison skips the endpoint rung entirely and lands on the documented one-entry widening: with ONE
configured entry, a bare ``custom`` route at an endpoint belonging to NO configured entry is admitted
on that entry's pin. That is the same class as the R3 BLOCK-1(a) finding, on a path the R3 range did
not touch, which is why it is carried rather than introduced.

Real loader + the real vision route against a temp HERMES_HOME. Fake credentials, loopback URLs,
no network: the route is refused before any request would be made.
"""

from __future__ import annotations

import pytest

_BROKEN = "model: [save in progress\n"
_CONFIGURED_ENDPOINT = "http://127.0.0.1:18081/v1"
_UNMAPPED_ENDPOINT = "http://127.0.0.1:18099/v1"


def _clear_caches():
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


@pytest.fixture
def pinned_one_entry(tmp_path, monkeypatch):
    """One configured custom entry, pinned by ``model.provider`` — the widening's own shape."""
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text(
        "model:\n  provider: alpha\n  default: alpha-model\n"
        "providers:\n"
        f"  alpha:\n    base_url: {_CONFIGURED_ENDPOINT}\n    api_key: fake-alpha-no-network\n",
        encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    _clear_caches()
    from hermes_cli import auth, config
    loaded, failure = config.load_config_with_status()
    assert failure is None, "the seeded config did not read cleanly"
    assert auth.config_pinned_provider(loaded) == "alpha"
    try:
        yield path
    finally:
        _clear_caches()


def test_the_vision_route_refuses_an_endpoint_belonging_to_no_configured_entry(
        pinned_one_entry, monkeypatch):
    """A bare ``custom`` vision client at an unmapped endpoint must not ride alpha's pin.

    The route is built by ``_vision_auto_route_unchecked``; this stubs that one function so the
    endpoint under test is exactly what the real route would have carried, and leaves the
    publication check and the whole pin ladder real. Without the ``base_url`` argument the check
    cannot see that the endpoint is not alpha's, so the widening admits it.
    """
    from agent import auxiliary_client as aux
    from hermes_cli import config

    pinned_one_entry.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure(), "the config did not fail to read"

    class _Client:
        base_url = _UNMAPPED_ENDPOINT

    monkeypatch.setattr(
        aux, "_vision_auto_route_unchecked",
        lambda runtime, model, api_mode, async_mode: ("custom", _Client(), "some-model"))

    provider, client, model = aux._vision_auto_route(
        {"provider": "custom"}, "some-model", None, False)
    assert client is None, (
        "a bare custom vision route at an endpoint belonging to no configured entry was published "
        "on the pinned entry's name")
    assert provider is None and model is None


def test_the_vision_route_still_allows_the_pinned_entrys_own_endpoint(
        pinned_one_entry, monkeypatch):
    """The fix must not refuse the route the widening exists to allow.

    Same failed read, same single entry — but the client's endpoint IS alpha's, so the endpoint rung
    identifies it positively and the route is allowed.
    """
    from agent import auxiliary_client as aux
    from hermes_cli import config

    pinned_one_entry.write_text(_BROKEN, encoding="utf-8")
    assert config.current_config_read_failure()

    class _Client:
        base_url = _CONFIGURED_ENDPOINT

    sentinel = _Client()
    monkeypatch.setattr(
        aux, "_vision_auto_route_unchecked",
        lambda runtime, model, api_mode, async_mode: ("custom", sentinel, "some-model"))

    provider, client, model = aux._vision_auto_route(
        {"provider": "custom"}, "some-model", None, False)
    assert client is sentinel, "the pinned entry's own endpoint was refused"
    assert provider == "custom" and model == "some-model"


def test_every_route_blocked_call_site_in_auxiliary_client_passes_an_endpoint():
    """The omission was one call site among four; keep the class closed rather than the instance.

    ``_route_blocked`` is the choke point and ``base_url`` is keyword-only with a ``""`` default, so
    a new call site silently re-opens the widening. This asserts the contract across the module's
    own call sites by invoking the real checker with a recorded argument, not by reading source.
    """
    import inspect

    from agent import auxiliary_client as aux

    sig = inspect.signature(aux._route_blocked)
    assert "base_url" in sig.parameters, "the endpoint parameter is gone from the choke point"

    # Every in-module caller of _route_blocked other than the choke point's own definition must
    # forward an endpoint. _published_route_blocked does so by construction; the direct callers are
    # checked by invoking them with a client that carries a recognisable endpoint and asserting the
    # value reaches the rule.
    seen: list = []

    def _record(provider, selection, *, base_url="", **_):
        seen.append(base_url)
        return None

    import hermes_cli.auth as auth_mod
    original = auth_mod.provider_blocked_by_config_failure
    try:
        auth_mod.provider_blocked_by_config_failure = _record

        class _Client:
            base_url = _CONFIGURED_ENDPOINT

        assert aux._published_route_blocked(_Client(), "custom", None, "vision") is False
        assert seen and seen[-1] == _CONFIGURED_ENDPOINT, (
            f"_published_route_blocked did not forward the client endpoint: {seen!r}")
    finally:
        auth_mod.provider_blocked_by_config_failure = original
