"""Token material a retired Codex chain held is never a new authorization (RULE-A1, A3).

Retirement archives the chain record (and ``hermes auth recovery clear`` may delete the archive),
so recognition that searched only LIVE records forgot every token a replaced or logged-out login
ever held: importing its already-consumed R0 created a fresh, refreshable chain from it. The
durable retired-token index keeps the recognition, and evidence that is missing or unreadable
never establishes that material is new (fail closed). Fake credentials only.
"""

import hermes_cli.auth as auth
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, read_json, registry, write_store)


def _sign_in(pair):
    auth._save_codex_tokens({"access_token": pair[0], "refresh_token": pair[1]})


def test_consumed_tokens_of_a_retired_chain_are_refused_after_archive_and_clear(codex_home):
    from hermes_cli.auth_codex_authority import import_pair, logout_active_store, migrate
    from hermes_cli.auth_codex_recovery import clear_archives

    root, endpoint = codex_home
    r0 = endpoint.issue()
    write_store(root, singleton=r0)
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # R0 consumed -> R1
    _sign_in(endpoint.issue())  # replaces the chain: the old one is retired, its record archived
    assert import_pair(*r0, reason="test") is None
    clear_archives()
    assert import_pair(*r0, reason="test") is None, "recognition survives `hermes auth recovery clear`"

    logout_active_store()
    write_store(root, singleton=r0)  # the consumed pair turns up again as an unregistered row
    migrate()
    holders = [c for c in registry(root)["chains"].values()
               if any(r["row"] == "singleton" for r in c["rows"]) and c["state"] != "retired"]
    assert holders and all(c["state"] == "fenced" for c in holders), \
        "a consumed predecessor must never become an active chain"


def test_unreadable_evidence_does_not_make_material_new(codex_home):
    from hermes_cli.auth_codex_authority import authority_dispatch, import_pair

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    (cid,) = registry(root)["chains"]
    (root / "auth.recovery" / f"{cid}.json").write_text("{ damaged", encoding="utf-8")
    before = (root / "auth.json").read_bytes()
    assert import_pair(*endpoint.issue(), reason="test") is None
    assert (root / "auth.json").read_bytes() == before
