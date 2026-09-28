"""``hermes auth rollback-prepare``: what a build WITHOUT the Codex chain authority may find (deploy gate F8).

An older build reads and refreshes every Codex row directly and ignores fences, chain records and the
retired-token index, so a backup or the rows as the new build leaves them can hand it a consumed
pair. The preparation leaves exactly the owner rows of ACTIVE chains holding their recorded current
pair; everything else goes through the removal op (retired, indexed), non-Codex providers untouched.
The Codex CLI rule, the per-home "Codex off" state, alias trimming, strict inventory and the
damaged-registry refusals are in ``test_codex_rollback_review_edges.py``; the old build's own
behaviour on the prepared state is proven with real processes by ``scripts/codex_rollback_scenarios.py``
(``tests/scripts/test_codex_rollback_*.py``). Fake credentials only.
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_codex_registry import CodexAuthorityRefused
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, pool_row, read_json, registry, write_store)

OTHER = {"nous": {"access_token": "nous-at", "refresh_token": "nous-rt", "portal_base_url": "https://p"}}


def _codex_material(path):
    """Every Codex token string an older build could read from *path*."""
    store = read_json(path)
    found = []
    tokens = ((store.get("providers") or {}).get("openai-codex") or {}).get("tokens") or {}
    found += [tokens.get("access_token"), tokens.get("refresh_token")]
    for entry in (store.get("credential_pool") or {}).get("openai-codex") or ():
        found += [entry.get("access_token"), entry.get("refresh_token")]
    return {t for t in found if t}


def _prepare():
    from hermes_cli.auth_codex_rollback import prepare_rollback
    return prepare_rollback()


def test_an_active_rotated_chain_is_kept_and_other_providers_are_untouched(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    store = read_json(root / "auth.json")
    store["providers"].update(OTHER)
    (root / "auth.json").write_text(json.dumps(store), encoding="utf-8")
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # bootstrap + one rotation
    before = read_json(root / "auth.json")
    calls = len(endpoint.calls)

    result, report = _prepare()

    assert result.safe and not report["removed"]
    assert len(endpoint.calls) == calls, "the preparation must never contact the token endpoint"
    after = read_json(root / "auth.json")
    assert after["providers"]["openai-codex"]["tokens"] == before["providers"]["openai-codex"]["tokens"]
    assert after["providers"]["nous"] == OTHER["nous"]
    receipt = read_json(root / "auth.rollback-receipt.json")
    assert {k["row"] for k in receipt["kept"]} >= {"singleton"}


def test_a_chain_fenced_indeterminate_leaves_no_material_for_the_old_build(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    endpoint.mode = "lost"  # the server consumed the refresh token, the response never arrived
    with pytest.raises(auth.AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    tokens = read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"]
    assert tokens["refresh_token"] == endpoint.calls[-1], "precondition: rows keep the consumed predecessor"

    result, report = _prepare()

    assert result.safe and report["removed"]
    assert not _codex_material(root / "auth.json"), "the possibly consumed pair is still readable"
    assert all(c["state"] == "retired" for c in registry(root)["chains"].values())
    from hermes_cli.auth_codex_authority import import_pair
    assert import_pair(tokens["access_token"], tokens["refresh_token"], reason="t") is None, \
        "a rolled-forward build must still recognise the removed pair"


def test_a_copy_outside_the_owner_store_goes_and_the_owner_keeps_refreshing(codex_home, monkeypatch):
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    work = make_profile(root, "work")
    write_store(work, singleton=pair)  # one login in two stores: root owns it
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # root rotates; work's copy is consumed
    assert pair[1] in _codex_material(work / "auth.json")

    result, report = _prepare()

    assert result.safe
    assert not _codex_material(work / "auth.json"), "a consumed copy was left in the profile"
    (chain,) = [c for c in registry(root)["chains"].values() if c["state"] == "active"]
    assert all(r["store"] == chain["owner"] for r in chain["rows"])
    calls = len(endpoint.calls)
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # the new build keeps working
    assert len(endpoint.calls) == calls + 1 and len(set(endpoint.calls)) == len(endpoint.calls)


def test_a_retired_chain_copy_goes(codex_home):
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    work = make_profile(root, "work")
    write_store(work, singleton=pair)
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    new = endpoint.issue()
    auth._save_codex_tokens({"access_token": new[0], "refresh_token": new[1]})  # replacement login
    assert pair[1] in _codex_material(work / "auth.json"), "precondition: the retired chain's copy"

    result, _report = _prepare()

    assert result.safe
    assert not _codex_material(work / "auth.json")
    assert _codex_material(root / "auth.json") == set(new)


def test_a_pending_successor_is_settled_without_the_network(codex_home, monkeypatch):
    from hermes_cli import auth_codex_refresh_txn as txn

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    real = txn._write_attempt

    class Died(BaseException):
        pass

    def die_after_pending(owner, record, attempt, **kw):
        out = real(owner, record, attempt, **kw)
        if attempt and attempt.get("state") == "pending":
            raise Died()
        return out

    with monkeypatch.context() as crash:
        crash.setattr(txn, "_write_attempt", die_after_pending)
        with pytest.raises(Died):
            auth.resolve_codex_runtime_credentials(force_refresh=True)
    calls = len(endpoint.calls)  # the server issued a successor the rows do not hold yet

    result, report = _prepare()

    assert result.safe and not report["removed"]
    assert len(endpoint.calls) == calls
    tokens = read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"]
    assert endpoint.valid.get(tokens["refresh_token"]), "the kept row must hold the UNCONSUMED successor"


def test_an_unfinished_op_refuses_and_changes_nothing(codex_home, monkeypatch):
    from hermes_cli import auth_codex_ops as ops

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    new = endpoint.issue()

    class Killed(SystemExit):
        pass

    with monkeypatch.context() as crash:
        crash.setattr(ops, "apply_payload", lambda body: (_ for _ in ()).throw(Killed("x")))
        with pytest.raises(Killed):
            auth._save_codex_tokens({"access_token": new[0], "refresh_token": new[1]})
    (op_id,) = registry(root)["ops"]
    (root / "auth.ops" / f"{op_id}.json").unlink()  # the decided op can no longer be completed
    before = _codex_material(root / "auth.json")

    with pytest.raises(CodexAuthorityRefused) as refused:
        _prepare()

    assert f"hermes auth recovery resolve {op_id[:8]}" in str(refused.value)
    assert _codex_material(root / "auth.json") == before
    assert not (root / "auth.rollback-receipt.json").exists()


def test_no_registry_a_single_login_is_registered_and_kept_as_it_is(codex_home):
    """Never governed by the new build: the login is registered by its token material first (the
    bootstrap migration), then kept — its pair unchanged, nothing sent."""
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)

    result, report = _prepare()

    assert result.safe and not report["removed"] and not endpoint.calls
    assert read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"]["refresh_token"] == pair[1]
    assert (root / "auth.chains.json").exists()


def test_check_is_read_only_and_tracks_the_preparation(codex_home):
    from hermes_cli.auth_codex_rollback import check_rollback

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    endpoint.mode = "lost"
    with pytest.raises(auth.AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    listing = sorted(str(p) for p in root.rglob("*"))
    snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    assert not check_rollback().safe
    assert sorted(str(p) for p in root.rglob("*")) == listing
    assert all(p.read_bytes() == b for p, b in snapshot.items())

    _prepare()
    assert check_rollback().safe


def test_preparing_twice_changes_nothing_the_second_time(codex_home):
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    work = make_profile(root, "work")
    write_store(work, singleton=pair)
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    _prepare()
    stores = {p: p.read_bytes() for p in (root / "auth.json", work / "auth.json")}
    reg = read_json(root / "auth.chains.json")

    result, report = _prepare()

    assert result.safe and not report["removed"]
    assert {p: p.read_bytes() for p in stores} == stores
    assert read_json(root / "auth.chains.json") == reg
