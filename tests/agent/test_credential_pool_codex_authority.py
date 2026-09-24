"""Codex rows in the credential pool under the chain authority (credential recovery protocol P3, §9).

Invariants:
- metadata-only saves: a live pool holding a STALE in-memory pair persists its status without
  rolling the tokens on disk back (RULE-T4) — the replay that revoked a whole Codex family;
- the pool refresh runs outside the pool mutex (RULE-L1/L3): another thread can take the pool
  lock while the POST is in flight, and a verdict only marks the row DEAD (metadata);
- runtime pool maintenance never removes a Codex row (the 24 h DEAD prune skips them, RULE-K1);
  ``hermes auth remove`` / ``logout`` do, as ownership ops that retire the chain;
- a fence requested while a caller up the stack holds an auth-store lock cannot take the migration
  lock: it is deferred, the chain record keeps the verdict, and the next transaction records it
  (no refresh in between).
"""

import threading
import time

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, pool_row, read_json, registry, write_store)


def _bootstrap():
    from hermes_cli.auth_codex_authority import authority_dispatch
    assert authority_dispatch() == "bootstrapped"


def test_stale_pool_status_save_never_rolls_tokens_back(codex_home):
    from agent.credential_pool import load_pool

    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, pool=[pool_row("acct01", pair, source="manual:device_code")])
    _bootstrap()
    stale_pool = load_pool("openai-codex")          # a live process loaded the old pair
    fresh_pool = load_pool("openai-codex")
    fresh_pool.try_refresh_matching(credential_id="acct01")  # another process rotates it
    rotated = read_json(root / "auth.json")["credential_pool"]["openai-codex"][0]["refresh_token"]
    assert rotated != pair[1]
    stale_pool.mark_exhausted_and_rotate(status_code=429, credential_id="acct01",
                                         error_context={"reason": "usage_limit_reached"})
    row = read_json(root / "auth.json")["credential_pool"]["openai-codex"][0]
    assert row["refresh_token"] == rotated, "a stale snapshot rolled the refresh token back"


def test_pool_refresh_runs_outside_the_pool_mutex(codex_home, monkeypatch):
    from agent.credential_pool import load_pool

    root, endpoint = codex_home
    write_store(root, pool=[pool_row("acct01", endpoint.issue(), source="manual:device_code")])
    _bootstrap()
    pool = load_pool("openai-codex")
    other_thread_got_lock = []
    real_endpoint = endpoint.__call__

    def probe_lock():
        got = pool._lock.acquire(timeout=2)
        other_thread_got_lock.append(got)
        if got:
            pool._lock.release()

    def slow_endpoint(*a, **k):
        t = threading.Thread(target=probe_lock)
        t.start()
        t.join()
        return real_endpoint(*a, **k)

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", slow_endpoint)
    assert pool.try_refresh_matching(credential_id="acct01") is not None
    assert other_thread_got_lock == [True], "the pool mutex was held across the token POST"


def test_verdict_marks_the_row_dead_and_keeps_its_tokens(codex_home):
    from agent.credential_pool import load_pool

    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, pool=[pool_row("acct01", pair, source="manual:device_code")])
    _bootstrap()
    endpoint.mode = "lost"
    assert load_pool("openai-codex").try_refresh_matching(credential_id="acct01") is None
    row = read_json(root / "auth.json")["credential_pool"]["openai-codex"][0]
    assert row["last_status"] == "dead"
    assert (row["access_token"], row["refresh_token"]) == pair, "tokens stay for the owner's record"
    (chain,) = registry(root)["chains"].values()
    assert chain["fence"]["reason"] == "indeterminate"


def test_dead_codex_rows_are_never_pruned_at_runtime(codex_home):
    from agent.credential_pool import load_pool

    root, endpoint = codex_home
    write_store(root, pool=[
        pool_row("dead01", endpoint.issue(), source="manual:device_code", last_status="dead",
                 last_status_at=time.time() - 30 * 3600),
        pool_row("ok0001", endpoint.issue(), source="manual:device_code", priority=1)])
    _bootstrap()
    assert load_pool("openai-codex").select().id == "ok0001"
    ids = [r["id"] for r in read_json(root / "auth.json")["credential_pool"]["openai-codex"]]
    assert ids == ["dead01", "ok0001"]


def test_remove_and_logout_are_ownership_ops(codex_home):
    from types import SimpleNamespace

    from agent.credential_pool import load_pool
    from hermes_cli import auth_commands

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue(),
                pool=[pool_row("indep1", endpoint.issue(), source="manual:device_code", priority=5)])
    _bootstrap()
    auth_commands.auth_remove_command(SimpleNamespace(provider="openai-codex", target="indep1"))
    store = read_json(root / "auth.json")
    assert [r["id"] for r in store.get("credential_pool", {}).get("openai-codex", [])] != ["indep1"]
    assert all(r.id != "indep1" for r in load_pool("openai-codex").entries()), "not re-created at runtime"
    retired = [c for c in registry(root)["chains"].values() if c["state"] == "retired"]
    assert retired, "the removed login's chain is retired"
    auth.clear_provider_auth("openai-codex")
    store = read_json(root / "auth.json")
    assert "openai-codex" not in store.get("providers", {})
    assert not store.get("credential_pool", {}).get("openai-codex")
    assert all(c["state"] == "retired" for c in registry(root)["chains"].values())
    assert not registry(root)["ops"]


def test_fence_inside_a_callers_store_lock_is_deferred_not_lost(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    _bootstrap()
    endpoint.mode = "lost"
    with auth._auth_store_lock():  # a caller up the stack holds the store lock
        with pytest.raises(AuthError) as err:
            auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert err.value.code == "codex_refresh_indeterminate"
    posts = len(endpoint.calls)
    with pytest.raises(AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert len(endpoint.calls) == posts, "the verdict held although the fence was deferred"
    (chain,) = registry(root)["chains"].values()
    assert chain["state"] == "fenced", "the next transaction recorded the deferred fence"
