"""F8 review (Codex, 1ba190d25b): states the rollback preparation must not certify, in-process.

Copied from the review's reproductions (review-artifacts/f8-1ba190d25b): an incomplete store
inventory, an unreadable registry that may hide a registered home, removed material that must stay
recognised on roll-forward, aliases the older build's pool cannot keep in step, profiles the older
build would let borrow (or merge into) the default profile's login, a Codex CLI login the older build
would import into several stores, and a read-only ``--check`` that wrote a ``.corrupt`` copy.
Fake credentials only.
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_codex_registry import CodexAuthorityRefused
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, fake_jwt, make_profile, pool_row, read_json, registry, write_store)


def _prepare(**kw):
    from hermes_cli.auth_codex_rollback import prepare_rollback
    return prepare_rollback(**kw)


def _codex_rows(path):
    store = read_json(path)
    rows = []
    tokens = ((store.get("providers") or {}).get("openai-codex") or {}).get("tokens") or {}
    if tokens.get("access_token") or tokens.get("refresh_token"):
        rows.append(("singleton", tokens.get("refresh_token")))
    for entry in (store.get("credential_pool") or {}).get("openai-codex") or ():
        if entry.get("access_token") or entry.get("refresh_token"):
            rows.append((entry.get("source"), entry.get("refresh_token")))
    return rows


def test_readonly_check_never_creates_a_corrupt_store_copy(codex_home):
    from hermes_cli.auth_codex_rollback import check_rollback
    root, _endpoint = codex_home
    (root / "auth.json").write_text("{bad", encoding="utf-8")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = check_rollback()
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before, "--check wrote files while inspecting a corrupt store"
    assert not result.safe, "an unparseable store is not evidence of 'no Codex login'"


def test_failed_inventory_cannot_issue_a_safe_receipt(codex_home, monkeypatch):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    endpoint.mode = "lost"
    with pytest.raises(auth.AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    consumed = auth._load_auth_store(root / "auth.json")["providers"]["openai-codex"]["tokens"]
    work = make_profile(root, "unregistered")
    write_store(work, singleton=(consumed["access_token"], consumed["refresh_token"]))
    from hermes_cli import profiles

    def denied(*_a, **_k):
        raise PermissionError("review: refused profile inventory")
    monkeypatch.setattr(profiles, "_iter_named_profile_dirs", denied)
    try:
        result, _report = _prepare()
    except (OSError, auth.AuthError):
        assert not (root / "auth.rollback-receipt.json").exists()
        return  # failure to prove completeness correctly refuses
    assert not result.safe, "receipt certified safety while an unseen profile retained a consumed token"


def test_unreadable_registry_refuses(codex_home):
    """A damaged registry cannot prove it lists every home that held a Codex login (a registered
    home outside profiles/ is found only through it)."""
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    (root / "auth.chains.json").write_text("{ damaged", encoding="utf-8")
    with pytest.raises(CodexAuthorityRefused) as refused:
        _prepare()
    assert "--rebuild" in str(refused.value)
    assert not (root / "auth.rollback-receipt.json").exists()


def test_a_rebuilt_registry_needs_the_other_homes_confirmation(codex_home):
    from hermes_cli.auth_codex_authority import migrate
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    (root / "auth.chains.json").write_text("{ damaged", encoding="utf-8")
    migrate(rebuild=True)
    with pytest.raises(CodexAuthorityRefused):
        _prepare()
    result, report = _prepare(other_homes_checked=True)
    assert result.safe and report["removed"], "every chain of a rebuilt registry is paused: removed"
    assert not _codex_rows(root / "auth.json")


def test_removed_unregistered_material_stays_non_importable(codex_home):
    from hermes_cli.auth_codex_authority import import_pair
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue("owner"))
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    used = endpoint.issue("unregistered")
    endpoint(*used)  # consumed elsewhere; a stale copy of it turns up below
    work = make_profile(root, "work")
    write_store(work, singleton=used)
    result, report = _prepare()
    assert result.safe and any(v["reason"] == "unregistered" for v in report["removed"])
    assert import_pair(*used, reason="review-roll-forward") is None, \
        "rollback removed this pair without retaining evidence; roll-forward imported it"
    assert endpoint.calls.count(used[1]) == 1


def test_only_one_row_per_login_and_no_manual_alias_beside_a_singleton(codex_home):
    """The older build's pool writes a refreshed ``device_code`` row back to the singleton but
    never a ``manual:device_code`` one, and syncs that row FROM the singleton first: a kept manual
    alias is refreshed and then replayed through the singleton (review blocker 2)."""
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair, pool=[pool_row("manual", pair, source="manual:device_code")])
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    result, report = _prepare()
    assert result.safe
    sources = [src for src, _rt in _codex_rows(root / "auth.json")]
    assert sources.count("singleton") == 1 and sources.count("device_code") <= 1
    assert "manual:device_code" not in sources
    assert [v for v in report["removed"] if v["row"] == "pool:manual"]


def test_every_profile_ends_with_its_own_login_or_codex_off(codex_home, monkeypatch):
    """Under the older build a profile without its own Codex state BORROWS the default profile's,
    and that borrow path forks the login. Every profile must end with its own login, or with Codex
    switched off for the older build: an EMPTY provider section (read as "no login", no Codex CLI
    import, left alone by the older build's fork heal) plus an inert pool row (no root fallback)."""
    from hermes_cli.auth_codex_authority import sign_in
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    work = make_profile(root, "work")
    notes = make_profile(root, "notes")  # never had a Codex login
    tokenless = make_profile(root, "tokenless")
    (tokenless / "auth.json").write_text(json.dumps({"version": 1, "providers": {
        "openai-codex": {"auth_mode": "chatgpt", "tokens": {}}}}), encoding="utf-8")
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    monkeypatch.setenv("HERMES_HOME", str(work))
    sign_in(*endpoint.issue())  # the SAME account as root: the older build merges it into root
    monkeypatch.setenv("HERMES_HOME", str(root))

    result, report = _prepare()

    assert result.safe
    for home in (work, notes, tokenless):
        store = read_json(home / "auth.json")
        assert store["providers"]["openai-codex"] == {}, home.name
        pool = store["credential_pool"]["openai-codex"]
        assert pool and not _codex_rows(home / "auth.json"), home.name
    from pathlib import Path
    assert sorted(Path(k).parent.name for k in result.codex_off) == ["notes", "tokenless", "work"]
    assert [v for v in report["removed"] if "merge" in v["reason"]], "the same-account profile login goes"


def test_a_profile_login_of_another_account_is_kept(codex_home, monkeypatch):
    from hermes_cli.auth_codex_authority import sign_in
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue("acct-root"))
    work = make_profile(root, "work")
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    other = (fake_jwt("acct-other", sub="user-other"), "rt-other-account")
    endpoint.valid[other[1]] = True
    monkeypatch.setenv("HERMES_HOME", str(work))
    sign_in(*other)
    monkeypatch.setenv("HERMES_HOME", str(root))
    result, _report = _prepare()
    assert result.safe
    assert ("singleton", other[1]) in _codex_rows(work / "auth.json")


def test_an_importable_codex_cli_login_blocks_the_receipt(codex_home, tmp_path):
    """The older build imports the Codex CLI file into ANY store whose login is missing,
    token-less or rejected; several stores importing one pair replay it (review blocker 3)."""
    root, endpoint = codex_home
    make_profile(root, "work")
    cli = tmp_path / "no-codex-cli"
    cli.mkdir()
    pair = endpoint.issue()
    (cli / "auth.json").write_text(json.dumps({"tokens": {"access_token": pair[0], "refresh_token": pair[1]}}),
                                   encoding="utf-8")
    write_store(root, singleton=endpoint.issue())
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    result, _report = _prepare()
    assert not result.safe and result.codex_cli["status"] == "importable"
    assert not (root / "auth.rollback-receipt.json").exists()
    (cli / "auth.json").unlink()
    result, _report = _prepare()
    assert result.safe
