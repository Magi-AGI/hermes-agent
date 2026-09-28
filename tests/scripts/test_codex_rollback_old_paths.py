"""F8 review (Codex, 1ba190d25b): the older build's own paths on states the preparation admits.

Real processes: this checkout prepares, a checkout of the deployed build (``HERMES_TEST_ROLLBACK_OLD_TREE``)
then runs its real resolver and pool against a local single-use token endpoint whose ledger records
every presented token. Copied from the review's reproductions: a registered home hidden by a damaged
registry; one Codex CLI pair importable into several stores (empty and token-less profiles); a
singleton with manual and normal aliases cycled pool-first and singleton-first; and the multi-profile
workload (borrowers, a same-account profile login, a copy, another account) cycled repeatedly.
Fake credentials, loopback only.
"""

import json
import os
from pathlib import Path

import pytest

from scripts.bench_codex_authority import make_profile, write_store
from scripts.codex_rollback_scenarios import Sandbox
from tests.hermes_cli.codex_authority_env import pool_row


@pytest.fixture
def sb():
    old = os.environ.get("HERMES_TEST_ROLLBACK_OLD_TREE")
    if not old:
        pytest.skip("HERMES_TEST_ROLLBACK_OLD_TREE (a checkout of the deployed build) not set")
    sandbox = Sandbox("review-old-paths", Path(old), False)
    yield sandbox
    sandbox.close()


def _detail(sb):
    return json.dumps({"ledger": sb.ledger_lines(), "log": sb.log[-12:]}, indent=1, default=str)[-6000:]


def _cli_pair(sb):
    access, refresh = sb.ledger.issue("cli")
    cli = sb.dir / "no-codex"
    cli.mkdir(exist_ok=True)
    (cli / "auth.json").write_text(json.dumps({"tokens": {"access_token": access, "refresh_token": refresh}}),
                                   encoding="utf-8")
    return cli / "auth.json"


def test_unreadable_registry_cannot_hide_a_nonstandard_store(sb):
    # work is beneath the native root but outside profiles/: only the registry knows it.
    sb.root = sb.dir / "userhome" / ".hermes"
    sb.root.mkdir(parents=True)
    sb.home = lambda profile="": sb.root / profile if profile else sb.root
    write_store(sb.home("work"), singleton=sb.ledger.issue("work"))
    assert sb.run("new", "new-refresh", profile="work")["outcome"] == "ok"
    assert sb.run("new", "new-kill", "after-response", profile="work")["exit"] == 137
    assert sb.run("new", "new-refresh", profile="work")["outcome"] == "codex_refresh_indeterminate"
    (sb.root / "auth.chains.json").write_text("{damaged", encoding="utf-8")
    prepared = sb.run("new", "prepare")
    if prepared.get("outcome") == "safe":
        sb.run("old", "old-refresh", profile="work")
    assert prepared.get("outcome") != "safe", "a damaged registry certified an inventory it cannot prove"
    assert sb.replays() == 0, _detail(sb)


@pytest.mark.parametrize("tokenless", [False, True], ids=["empty-profiles", "tokenless-profiles"])
def test_one_cli_pair_cannot_be_imported_into_several_stores(sb, tokenless):
    for name in ("work", "notes"):
        home = make_profile(sb.root, name)
        if tokenless:
            (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {
                "openai-codex": {"auth_mode": "chatgpt", "tokens": {}}}}), encoding="utf-8")
    cli = _cli_pair(sb)
    prepared = sb.run("new", "prepare")
    assert prepared.get("outcome") != "safe", f"an importable Codex CLI pair was certified: {prepared}"
    cli.unlink()  # the runbook's remedy: the Codex CLI login is moved aside for the rollback
    prepared = sb.run("new", "prepare")
    assert prepared.get("outcome") == "safe", prepared
    for _round in range(2):
        for name in ("work", "notes", ""):
            sb.run("old", "old-refresh", profile=name)
            sb.run("old", "old-pool", profile=name)
    assert sb.replays() == 0 and not sb.ledger.presented, _detail(sb)


def test_aliases_the_old_pool_cannot_sync_are_not_kept(sb):
    pair = sb.ledger.issue("alias")
    write_store(sb.root, singleton=pair, pool=[pool_row("manual", pair, source="manual:device_code")])
    assert sb.run("new", "new-refresh")["outcome"] == "ok"
    prepared = sb.run("new", "prepare")
    assert prepared.get("outcome") == "safe", prepared
    for op in ("old-pool", "old-refresh", "old-pool", "old-pool", "old-refresh", "old-refresh", "old-pool"):
        sb.run("old", op)
    ok = [p for p in sb.ledger.presented if p[3] == "ok"]
    assert sb.replays() == 0, _detail(sb)
    assert len(ok) >= 6, "the kept login must keep refreshing under the old build"


def test_ungoverned_default_home_cannot_be_certified_with_replaying_aliases(sb):
    """F8 round 2: NO registry and no trace of the new build, the default home holds a singleton
    and a ``manual:device_code`` alias of the same pair. Neither ``--check`` nor the preparation may
    certify that as it is; after the preparation, Codex used from the default home only (the
    runbook's most restrictive mode) never replays."""
    pair = sb.ledger.issue("A")
    write_store(sb.root, singleton=pair, pool=[pool_row("manual", pair, source="manual:device_code")])
    assert not (sb.root / "auth.chains.json").exists()
    assert sb.run("new", "check").get("outcome") != "safe", "--check certified unregistered duplicate rows"
    prepared = sb.run("new", "prepare")
    assert prepared.get("outcome") == "safe", prepared
    for op in ("old-pool", "old-refresh", "old-pool", "old-pool", "old-refresh"):
        sb.run("old", op)
    assert sb.replays() == 0, _detail(sb)


def test_ungoverned_copies_across_homes_are_one_login(sb):
    """No registry: the same pair in the default home and in a profile is ONE login (identified by
    its token material), kept once."""
    pair = sb.ledger.issue("A")
    write_store(sb.root, singleton=pair)
    write_store(make_profile(sb.root, "work"), singleton=pair)
    prepared = sb.run("new", "prepare")
    assert prepared.get("outcome") == "safe", prepared
    for name, op in (("work", "refresh"), ("", "refresh"), ("work", "pool"), ("", "pool"), ("", "refresh")):
        sb.run("old", f"old-{op}", profile=name)
    assert sb.replays() == 0, _detail(sb)
    assert any(p[3] == "ok" for p in sb.ledger.presented), "the default home's login keeps working"


def test_multi_profile_workload_never_replays(sb):
    """root's login; work signed in with the SAME account (the old build merges it into root, then
    borrows); side holds a copy of root's login; notes never had one; other signed in with ANOTHER
    account. Every store is cycled repeatedly, both paths, with refreshes from different stores
    back to back (a pool pass in the same store in between runs the old build's fork heal, which
    would hide a fork)."""
    a0 = sb.ledger.issue("A")
    write_store(sb.root, singleton=a0)
    for name in ("work", "side", "notes", "other"):
        make_profile(sb.root, name)
    write_store(sb.home("side"), singleton=a0)
    sb.run("new", "new-dispatch")
    sb.run("new", "new-refresh")
    sb.run("new", "new-sign-in", sb.ledger.issue("A"), profile="work")
    sb.run("new", "new-sign-in", sb.ledger.issue("B"), profile="other")
    prepared = sb.run("new", "prepare")
    assert prepared.get("outcome") == "safe", prepared
    steps = [("notes", "refresh"), ("", "refresh"), ("work", "refresh"), ("side", "refresh"), ("", "pool"),
             ("other", "refresh"), ("notes", "pool"), ("", "refresh"), ("other", "pool"), ("work", "pool"),
             ("side", "pool"), ("", "refresh"), ("notes", "refresh"), ("other", "refresh"), ("", "pool"),
             ("work", "refresh"), ("", "refresh"), ("other", "pool")]
    for name, op in steps:
        sb.run("old", f"old-{op}", profile=name)
    assert sb.replays() == 0, _detail(sb)
    chains = {p[2] for p in sb.ledger.presented if p[3] == "ok"}
    assert {"A", "B"} <= chains, "root's and the other account's logins must keep working"
