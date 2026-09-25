"""``--retry-refresh`` can select every chain its fence message advertises it for (RULE-O1).

The override looked only at the ACTIVE store's singleton, so the logins that most often end up
paused after an unconfirmed refresh — a pool-only account (``hermes auth add``) and a profile
borrowing root's login — could never use it. It now resolves the paused chain the active store
owns or borrows (under the ownership locks), and ``--chain <id>`` (named in the fence message and
``hermes auth recovery list``) selects one explicitly. Fake credentials only.
"""

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from hermes_cli.auth_codex_registry import CodexAuthorityRefused
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, pool_row, registry, write_store)


def _pause_pool_row(root, endpoint, row_id):
    from agent.credential_pool import load_pool
    endpoint.mode = "lost"
    assert load_pool("openai-codex").try_refresh_matching(credential_id=row_id) is None
    endpoint.mode = "ok"


@pytest.mark.parametrize("login", ["pool-only", "borrowing-profile"])
def test_the_override_reaches_the_paused_login_of_this_store(codex_home, monkeypatch, login):
    from hermes_cli.auth_codex_authority import authority_dispatch, grant_retry_override

    root, endpoint = codex_home
    if login == "pool-only":
        write_store(root, pool=[pool_row("acct01", endpoint.issue(), source="manual:device_code")])
        assert authority_dispatch() == "bootstrapped"
        _pause_pool_row(root, endpoint, "acct01")
    else:
        write_store(root, singleton=endpoint.issue())
        work = make_profile(root, "work")
        write_store(work)
        assert authority_dispatch() == "bootstrapped"
        monkeypatch.setenv("HERMES_HOME", str(work))
        endpoint.mode = "lost"
        with pytest.raises(AuthError):
            auth.resolve_codex_runtime_credentials(force_refresh=True)
        endpoint.mode = "ok"
    (cid,) = [c for c, e in registry(root)["chains"].items() if e["state"] == "fenced"]
    grant_retry_override()
    assert registry(root)["chains"][cid]["state"] == "active", "the paused login was not reachable"


def test_chain_selection_is_explicit_when_several_logins_are_paused(codex_home):
    from hermes_cli.auth_codex_authority import authority_dispatch, grant_retry_override

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue(),
                pool=[pool_row("acct02", endpoint.issue(), source="manual:device_code", priority=1)])
    assert authority_dispatch() == "bootstrapped"
    endpoint.mode = "lost"
    with pytest.raises(AuthError) as err:
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    _pause_pool_row(root, endpoint, "acct02")
    paused = [c for c, e in registry(root)["chains"].items() if e["state"] == "fenced"]
    assert len(paused) == 2
    singleton_chain = next(c for c in paused
                           if any(r["row"] == "singleton" for r in registry(root)["chains"][c]["rows"]))
    assert f"--chain {singleton_chain[:12]}" in str(err.value), "the fence message names the selector"
    with pytest.raises(CodexAuthorityRefused, match="--chain"):
        grant_retry_override()
    grant_retry_override(chain=singleton_chain[:12])
    assert {c: registry(root)["chains"][c]["state"] for c in paused} == {
        c: ("active" if c == singleton_chain else "fenced") for c in paused}
