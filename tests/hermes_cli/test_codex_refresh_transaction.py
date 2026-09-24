"""Codex refresh transaction invariants (credential recovery protocol stage 1, design rev 9 §5).

Driven through the real entry point ``resolve_codex_runtime_credentials`` against a temp Hermes
root; the token endpoint is the single-use fake in ``codex_authority_env`` (no network).

Invariants:
- no automatic replay: an outcome that may have consumed the refresh token (lost response, any HTTP
  response after sending incl. 429, a 200 without a refresh token) fences the chain; later calls make
  NO network request;
- a pre-send failure (connect error) is not a verdict: the chain stays usable;
- refresh-after-refresh on one chain never enters repair (r5-1);
- a peer's rotation is adopted by comparing the WHOLE pair, even when the provider returns the same
  refresh token (r7-4): two forced refreshes from one snapshot make exactly one network call;
- a profile borrowing the root grant refreshes the ROOT chain and never forks it into the profile.
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, pool_row, read_json, registry, write_store)


def _resolve(**kw):
    return auth.resolve_codex_runtime_credentials(**kw)


def _chain(root):
    chains = registry(root)["chains"]
    assert len(chains) == 1
    return next(iter(chains.values()))


def test_refresh_after_refresh_never_enters_repair(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    first = _resolve(force_refresh=True)["api_key"]
    second = _resolve(force_refresh=True)["api_key"]
    assert first != second
    assert len(endpoint.calls) == 2
    chain = _chain(root)
    assert chain["state"] == "active"
    store = read_json(root / "auth.json")
    assert store["providers"]["openai-codex"]["tokens"]["access_token"] == second
    alias = [r for r in store["credential_pool"]["openai-codex"] if r["source"] == "device_code"]
    assert alias and alias[0]["access_token"] == second, "every row of the chain moves together"


@pytest.mark.parametrize("mode", ["lost", "status429"])
def test_unknown_outcome_fences_and_never_replays(codex_home, mode):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    endpoint.mode = mode
    with pytest.raises(AuthError) as err:
        _resolve(force_refresh=True)
    assert err.value.code == "codex_refresh_indeterminate"
    endpoint.mode = "ok"
    calls = len(endpoint.calls)
    for _ in range(3):
        with pytest.raises(AuthError) as again:
            _resolve(force_refresh=True)
        assert again.value.code == "codex_refresh_indeterminate"
    assert len(endpoint.calls) == calls, "a fenced chain must never reach the token endpoint"
    assert _chain(root)["state"] == "fenced"


def test_200_without_refresh_token_is_indeterminate(codex_home, monkeypatch):
    """RULE-V1 through the REAL refresh_codex_oauth_pure (mocked transport, no network)."""
    import httpx
    from hermes_cli import auth_codex
    from tests.hermes_cli.codex_authority_env import REAL_REFRESH_CODEX_OAUTH_PURE, fake_jwt

    root, endpoint = codex_home
    write_store(root, singleton=(fake_jwt(), "rt-original"))
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", REAL_REFRESH_CODEX_OAUTH_PURE)
    posted = []

    def handler(request):
        posted.append(request)
        return httpx.Response(200, json={"access_token": fake_jwt()})  # no refresh_token

    monkeypatch.setattr(auth_codex, "_codex_http_client",
                        lambda **kw: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthError) as err:
        _resolve(force_refresh=True)
    assert err.value.code == "codex_refresh_indeterminate"
    assert len(posted) == 1
    assert _chain(root)["fence"]["reason"] == "indeterminate"
    with pytest.raises(AuthError):
        _resolve(force_refresh=True)
    assert len(posted) == 1, "the predecessor refresh token must never be replayed"


def test_pre_send_failure_is_not_a_verdict(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    endpoint.mode = "connect"
    with pytest.raises(Exception):
        _resolve(force_refresh=True)
    assert _chain(root)["state"] == "active"
    endpoint.mode = "ok"
    assert _resolve(force_refresh=True)["api_key"]


def test_rejected_refresh_token_fences_without_cli_import(codex_home, monkeypatch):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    endpoint.mode = "reject"
    imported = []
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", lambda: imported.append(1) or None)
    with pytest.raises(AuthError) as err:
        _resolve(force_refresh=True)
    assert err.value.relogin_required
    assert _chain(root)["fence"]["reason"] == "rejected"
    assert not imported, "a paused login is never repaired by a Codex CLI import (decision 5)"


def test_same_refresh_token_peer_rotation_is_adopted_by_pair(codex_home):
    """r7-4: two forced refreshes started from (A0, R0); the endpoint keeps R0. Exactly one POST."""
    from hermes_cli.auth_codex import _refresh_codex_auth_tokens

    root, endpoint = codex_home
    a0, r0 = endpoint.issue()
    write_store(root, singleton=(a0, r0))
    endpoint.mode = "same_refresh"
    started = {"access_token": a0, "refresh_token": r0}
    first = _refresh_codex_auth_tokens(dict(started), 20)
    second = _refresh_codex_auth_tokens(dict(started), 20)  # a stale caller, same snapshot
    assert len(endpoint.calls) == 1
    assert first["refresh_token"] == r0 and second == first


def test_borrowing_profile_refreshes_the_root_chain(codex_home, monkeypatch):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    profile = make_profile(root, "work")
    write_store(profile)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    new_key = _resolve(force_refresh=True)["api_key"]
    assert read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"]["access_token"] == new_key
    assert "openai-codex" not in read_json(profile / "auth.json").get("providers", {}), \
        "the root grant must not be forked into the borrowing profile"
