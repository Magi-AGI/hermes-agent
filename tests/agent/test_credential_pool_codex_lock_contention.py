"""A LOCAL lock-acquisition timeout is a retryable busy, never a verdict on the credential.

A Codex refresh that could not acquire its store lock (another process held it past the budget)
fell into the pool's generic failure path and marked the row exhausted, benching a healthy login
for the cooldown. Lock acquisition now raises a distinct ``LockAcquisitionTimeout`` at the lock
boundary; the refresh transaction reports it as retryable busy BEFORE anything was sent, and the
row's status and cooldown are untouched. A timeout AFTER the refresh request was sent is not a lock
timeout: it keeps the indeterminate-outcome protection (fence). Fake credentials only.
"""

import threading

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import codex_home, pool_row, read_json, registry, write_store  # noqa: F401


def test_store_lock_contention_leaves_the_credential_untouched_and_a_later_attempt_succeeds(codex_home, monkeypatch):
    from agent.credential_pool import load_pool
    from hermes_cli import auth_codex_refresh_txn as txn
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    write_store(root, pool=[pool_row("acct01", endpoint.issue(), source="manual:device_code")])
    assert authority_dispatch() == "bootstrapped"
    monkeypatch.setattr(txn, "AUTH_LOCK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setenv("HERMES_CODEX_REFRESH_TIMEOUT_SECONDS", "0")  # lock budget: 0 + 5 s
    pool = load_pool("openai-codex")
    held, release = threading.Event(), threading.Event()

    def other_process_holds_the_store():
        with auth._auth_store_lock(target_path=root / "auth.json"):
            held.set()
            release.wait(30)

    holder = threading.Thread(target=other_process_holds_the_store)
    holder.start()
    try:
        assert held.wait(10)
        entry = pool.try_refresh_matching(credential_id="acct01")
    finally:
        release.set()
        holder.join(10)
    assert endpoint.calls == [], "nothing may be sent without the lock"
    assert entry is not None and entry.last_status in (None, "ok"), f"marked {getattr(entry, 'last_status', None)}"
    row = read_json(root / "auth.json")["credential_pool"]["openai-codex"][0]
    assert row.get("last_status") in (None, "ok") and not row.get("last_error_reset_at"), row
    assert pool.try_refresh_matching(credential_id="acct01") is not None
    assert len(endpoint.calls) == 1, "the retry after the lock was released refreshes"


def test_a_timeout_after_the_request_was_sent_still_fences(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    real = endpoint.__call__

    def consumed_then_timed_out(access, refresh, **kw):
        real(access, refresh, **kw)  # the server consumed the refresh token
        raise TimeoutError("read timed out")  # ... and the response never arrived

    auth.refresh_codex_oauth_pure = consumed_then_timed_out
    try:
        with pytest.raises(AuthError) as err:
            auth.resolve_codex_runtime_credentials(force_refresh=True)
    finally:
        auth.refresh_codex_oauth_pure = endpoint
    assert err.value.code == "codex_refresh_indeterminate"
    (chain,) = registry(root)["chains"].values()
    assert chain["state"] == "fenced" and chain["fence"]["reason"] == "indeterminate"
