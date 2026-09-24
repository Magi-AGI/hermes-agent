"""Codex ownership operations: sign-in, import, removal, the retry override, crash recovery.

Design rev 9 §4 and §8 (stage 1). Real entry points against a temp Hermes root; fake tokens only.

Invariants:
- a fresh sign-in is owned by the store it runs in; a profile never overwrites the root grant it was
  borrowing, whatever the account (r5-5, r6-5);
- re-login replacing an ACTIVE singleton reassigns exactly the rows upstream's reauth kept, retires
  the old chain, keeps each row in one chain (RULE-G2) and resets the replaced credential's failure
  state so the retained pool row is selectable again (r7-3);
- an import never writes material a chain already held: a history token yields the CURRENT pair
  (r6-2), and nothing is imported while a login is paused (decision 5);
- ``--retry-refresh`` allows exactly one retry; an unknown outcome fences again under a new attempt,
  and a delayed repair for the old attempt is dropped (r5-7, r5-8);
- an op that crashed before its decision is abandoned; after its decision it is rolled forward from
  the payload — a sign-in's NEW credentials are recovered (r5-2) — and replaying it keeps metadata
  observed since (r6-4 / RULE-M7);
- a late failure from a replaced login cannot mark the replacement dead (RULE-T5);
- UNREADABLE registry: refresh is fenced with no network call; a sign-in preserves the damaged file
  and rebuilds with every old chain fenced (r5-3);
- profile delete / rename of a store holding a registered login is refused in stage 1 (r5-9).
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, pool_row, read_json, registry, write_store)


def _sign_in(pair, **kw):
    auth._save_codex_tokens({"access_token": pair[0], "refresh_token": pair[1]}, **kw)


def _bootstrap():
    from hermes_cli.auth_codex_authority import authority_dispatch
    assert authority_dispatch() == "bootstrapped"


def _rows_by_chain(root):
    seen = {}
    for cid, chain in registry(root)["chains"].items():
        for r in chain["rows"]:
            key = (r["store"], r["row"])
            assert key not in seen, f"row {key} is in two chains (RULE-G2)"
            seen[key] = cid
    return seen


@pytest.mark.parametrize("same_account", [False, True])
def test_profile_sign_in_is_profile_owned_and_leaves_root_alone(codex_home, monkeypatch, same_account):
    root, endpoint = codex_home
    root_pair = endpoint.issue("acct-root")
    write_store(root, singleton=root_pair)
    profile = make_profile(root, "work")
    write_store(profile)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    _sign_in(endpoint.issue("acct-root" if same_account else "acct-work"))
    root_store = read_json(root / "auth.json")
    assert root_store["providers"]["openai-codex"]["tokens"]["refresh_token"] == root_pair[1]
    owners = {c["owner"] for c in registry(root)["chains"].values() if c["state"] == "active"}
    assert len(owners) == 2, "root's chain and the profile's own chain"
    _rows_by_chain(root)


def test_relogin_reassigns_kept_rows_and_resets_their_failure_state(codex_home):
    from agent.credential_pool import load_pool

    root, endpoint = codex_home
    old = endpoint.issue()
    write_store(root, singleton=old, pool=[pool_row("dev001", old, last_status="dead", last_status_at=1.0,
                                                    last_error_reason="invalid_grant", label="mine", priority=3)])
    _bootstrap()
    (old_cid,) = registry(root)["chains"]
    new = endpoint.issue()
    _sign_in(new)
    reg = registry(root)
    assert reg["chains"][old_cid]["state"] == "retired"
    assert not (root / "auth.recovery" / f"{old_cid}.json").exists(), "retired record archived (RULE-A3)"
    assert list((root / "auth.recovery" / "archive").glob(f"{old_cid}.*"))
    row = read_json(root / "auth.json")["credential_pool"]["openai-codex"][0]
    assert row["id"] == "dev001" and row["refresh_token"] == new[1]
    assert row["last_status"] is None and row["label"] == "mine" and row["priority"] == 3
    _rows_by_chain(root)
    assert [e.id for e in load_pool("openai-codex").entries()] == ["dev001"]
    assert load_pool("openai-codex").select() is not None, "the re-authorized row is selectable"


def test_import_of_a_history_token_returns_the_current_pair(codex_home):
    from hermes_cli.auth_codex_authority import import_pair

    root, endpoint = codex_home
    r0 = endpoint.issue()
    write_store(root, singleton=r0)
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # R0 -> R1
    r1 = tuple(read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"][k]
               for k in ("access_token", "refresh_token"))
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # R1 -> R2
    current = tuple(read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"][k]
                    for k in ("access_token", "refresh_token"))
    before = (root / "auth.json").read_bytes()
    assert import_pair(*r1, reason="test") == current
    assert import_pair(*r0, reason="test") == current
    assert (root / "auth.json").read_bytes() == before, "predecessor material is never written"


def test_nothing_is_imported_while_a_login_is_paused(codex_home):
    from hermes_cli.auth_codex_authority import import_pair

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    endpoint.mode = "lost"
    with pytest.raises(AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert import_pair(*endpoint.issue(), reason="test") is None


def test_retry_override_allows_exactly_one_retry(codex_home):
    from hermes_cli.auth_codex_authority import grant_retry_override
    from hermes_cli.auth_codex_ops import apply_repair
    from hermes_cli.auth_codex_registry import RepairRequest

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    endpoint.mode = "lost"
    with pytest.raises(AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    (cid, chain), = registry(root)["chains"].items()
    first_attempt = chain["fence"]["attempt_id"]
    record_sha_a = None
    grant_retry_override()
    assert registry(root)["chains"][cid]["state"] == "active"
    calls = len(endpoint.calls)
    endpoint.valid[endpoint.calls[-1]] = True  # the operator was right: it never reached OpenAI
    with pytest.raises(AuthError):  # the retry: the endpoint still loses the response
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert len(endpoint.calls) == calls + 1, "exactly one retry"
    fence = registry(root)["chains"][cid]["fence"]
    assert fence["reason"] == "indeterminate" and fence["attempt_id"] != first_attempt
    with pytest.raises(AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert len(endpoint.calls) == calls + 1, "a new override is required"
    stale = RepairRequest(cid, chain["epoch"], "indeterminate", chain["owner"], record_sha_a, first_attempt)
    assert apply_repair(stale) is False, "a delayed repair for the old attempt is dropped (RULE-R1)"


def test_op_crash_before_decision_is_abandoned_and_after_decision_rolled_forward(codex_home, monkeypatch):
    from hermes_cli import auth_codex_ops as ops
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    _bootstrap()
    new = endpoint.issue()

    real_write_registry = ops.write_registry
    monkeypatch.setattr(ops, "write_registry", lambda data: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        _sign_in(new)
    monkeypatch.setattr(ops, "write_registry", real_write_registry)
    assert list((root / "auth.ops").glob("*.json")), "payload written, no decision"
    authority_dispatch()
    assert not list((root / "auth.ops").glob("*.json"))
    assert list((root / "auth.ops" / "archive").glob("*.abandoned.*.json"))
    assert read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"]["refresh_token"] != new[1]

    real_apply = ops.apply_payload
    monkeypatch.setattr(ops, "apply_payload", lambda body: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        _sign_in(new)
    monkeypatch.setattr(ops, "apply_payload", real_apply)
    assert registry(root)["ops"], "decided, not applied"
    calls = len(endpoint.calls)
    # An ORDINARY request (no scheduled job) finds the unfinished op and rolls it forward (r5-6).
    assert auth.resolve_codex_runtime_credentials(force_refresh=True)["api_key"] == new[0]
    assert len(endpoint.calls) == calls, "the recovered sign-in is current: nothing to refresh"
    store = read_json(root / "auth.json")
    assert store["providers"]["openai-codex"]["tokens"]["refresh_token"] == new[1], \
        "the NEW credentials are recovered from the payload"
    assert not registry(root)["ops"]


def test_replayed_payload_keeps_metadata_observed_since(codex_home, monkeypatch):
    from hermes_cli import auth_codex_ops as ops
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    _bootstrap()
    real_commit = ops.commit_op
    monkeypatch.setattr(ops, "commit_op", lambda op_id: (_ for _ in ()).throw(SystemExit("crash")))
    with pytest.raises(SystemExit):
        _sign_in(endpoint.issue())
    monkeypatch.setattr(ops, "commit_op", real_commit)
    store = json.loads((root / "auth.json").read_text(encoding="utf-8"))
    store["credential_pool"]["openai-codex"][0].update(last_status="exhausted", last_status_at=123.0, request_count=7)
    (root / "auth.json").write_text(json.dumps(store), encoding="utf-8")
    authority_dispatch()
    row = read_json(root / "auth.json")["credential_pool"]["openai-codex"][0]
    assert row["last_status"] == "exhausted" and row["request_count"] == 7


def test_late_failure_from_a_replaced_login_cannot_mark_the_replacement_dead(codex_home):
    from agent.credential_pool import load_pool

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    _bootstrap()
    stale_pool = load_pool("openai-codex")
    (entry,) = stale_pool.entries()
    _sign_in(endpoint.issue())  # re-login: the row keeps its id, moves to a new chain
    stale_pool.mark_exhausted_and_rotate(status_code=401, credential_id=entry.id,
                                         error_context={"reason": "invalid_grant"})
    row = read_json(root / "auth.json")["credential_pool"]["openai-codex"][0]
    assert row.get("last_status") in (None, "ok"), "the replacement login was marked dead by a stale save"


def test_unreadable_registry_blocks_refresh_and_sign_in_rebuilds_fenced(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    _bootstrap()
    (old_cid,) = registry(root)["chains"]
    (root / "auth.chains.json").write_text("{ damaged", encoding="utf-8")
    with pytest.raises(AuthError) as err:
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert err.value.code == "codex_recovery_fenced"
    assert endpoint.calls == []
    new = endpoint.issue()
    _sign_in(new)
    assert list(root.glob("auth.chains.json.corrupt.*")), "the damaged registry is preserved"
    states = {c["state"] for c in registry(root)["chains"].values()}
    assert "active" in states
    fenced = [c for c in registry(root)["chains"].values() if c["state"] != "active"]
    assert all(c["state"] == "retired" or c["fence"]["reason"] == "registry-rebuilt" for c in fenced)
    assert auth.resolve_codex_runtime_credentials(force_refresh=True)["api_key"]


def test_profile_holding_a_registered_login_cannot_be_deleted_or_renamed(codex_home):
    from hermes_cli import profiles

    root, endpoint = codex_home
    profile = make_profile(root, "work")
    write_store(profile, singleton=endpoint.issue())
    from hermes_cli.auth_codex_authority import authority_dispatch
    assert authority_dispatch() == "bootstrapped"
    with pytest.raises(ValueError, match="holds your Codex login"):
        profiles.delete_profile("work", yes=True)
    with pytest.raises(ValueError, match="holds your Codex login"):
        profiles.rename_profile("work", "play")
    assert profile.is_dir()
