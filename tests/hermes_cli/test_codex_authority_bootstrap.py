"""Chain authority bootstrap, stage-1 interception and legacy rescue files (design rev 9 §6, §7, §10).

Invariants:
- ABSENT registry at the first runtime use -> bootstrap migration; the singleton's pool alias is
  materialized IN the decision (RULE-K1) unless its source is suppressed (RULE-K2);
- identical token material in several places without a root row, or a token shared by two different
  pairs, is ambiguous: every such row is fenced (RULE-M4);
- a rescue file left by the withdrawn rev-0 code fences the chains its store owns OR borrows, and
  refresh is blocked (§7);
- read-only diagnostics on a cold start migrate nothing and write nothing (r6-7);
- runtime ``load_pool()`` creates, generates ids for and prunes no Codex rows (RULE-K1), and the
  Codex forked-grant consolidation does not run while other providers' heal still does (§10).
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, pool_row, read_json, registry, write_store)


def _dispatch():
    from hermes_cli.auth_codex_authority import authority_dispatch
    return authority_dispatch()


def test_bootstrap_materializes_the_singleton_alias(codex_home):
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    assert _dispatch() == "bootstrapped"
    reg = registry(root)
    (cid, chain), = reg["chains"].items()
    assert chain["state"] == "active" and chain["epoch"] == 1
    rows = {r["row"] for r in chain["rows"]}
    store = read_json(root / "auth.json")
    alias = store["credential_pool"]["openai-codex"]
    assert len(alias) == 1 and alias[0]["source"] == "device_code"
    assert rows == {"singleton", f"pool:{alias[0]['id']}"}
    assert store["providers"]["openai-codex"]["chain_id"] == cid == alias[0]["chain_id"]
    record = read_json(root / "auth.recovery" / f"{cid}.json")
    assert record["epoch"] == 1 and record["attempt"] is None
    assert not reg["ops"], "the bootstrap op is committed"


def test_bootstrap_keeps_a_suppressed_singleton_unselectable(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue(), suppressed=["device_code"])
    _dispatch()
    store = read_json(root / "auth.json")
    assert not (store.get("credential_pool") or {}).get("openai-codex"), "no selectable alias (RULE-K2)"
    assert store["suppressed_sources"]["openai-codex"] == ["device_code"]


def test_shared_token_material_is_ambiguous_and_fenced(codex_home):
    root, endpoint = codex_home
    access, refresh = endpoint.issue()
    other_access, _ = endpoint.issue()
    write_store(root, singleton=(access, refresh),
                pool=[pool_row("aa0001", (other_access, refresh), source="manual:device_code")])
    _dispatch()
    chains = registry(root)["chains"].values()
    assert chains and all(c["state"] == "fenced" and c["fence"]["reason"] == "ambiguous-lineage" for c in chains)
    with pytest.raises(AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert endpoint.calls == []


def test_legacy_rescue_in_a_borrowing_profile_fences_the_root_chain(codex_home, monkeypatch):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    profile = make_profile(root, "work")
    write_store(profile)
    (profile / "auth.codex-rotation-rescue.json").write_text(
        json.dumps({"entries": {"singleton": {"access_token": "x", "refresh_token": "rt-rescued"}}}),
        encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    with pytest.raises(AuthError) as err:
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert err.value.code == "codex_recovery_fenced"
    (chain,) = registry(root)["chains"].values()
    assert chain["fence"]["reason"] == "legacy-rescue"
    assert endpoint.calls == [], "a rescued token is never spent"
    assert not (profile / "auth.codex-rotation-rescue.json").exists(), "stage 2 archives it (untrusted)"
    assert list((root / "auth.ops" / "archive").glob("legacy-rescue.*.untrusted.json"))
    assert registry(root)["chains"][next(iter(registry(root)["chains"]))]["state"] == "fenced",         "a rescued pair the rows do not hold keeps the fence"


def test_cold_start_read_only_resolution_writes_nothing(codex_home):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    before = sorted(p.name for p in root.iterdir())
    result = auth.resolve_codex_runtime_credentials(read_only=True, force_refresh=True)
    assert result["api_key"]
    assert sorted(p.name for p in root.iterdir()) == before, "no registry, lock file or record"


def test_runtime_load_pool_creates_no_codex_rows_and_skips_codex_consolidation(codex_home, monkeypatch):
    from agent import credential_pool

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())  # registry ABSENT: no alias exists yet
    healed = []
    monkeypatch.setattr(auth, "heal_forked_single_use_oauth_grants", lambda p: healed.append(p))
    pool = credential_pool.load_pool("openai-codex")
    assert pool.entries() == []
    assert "credential_pool" not in read_json(root / "auth.json")
    assert healed == []
    credential_pool.load_pool("xai-oauth")
    assert healed == ["xai-oauth"], "other providers keep the consolidation heal"
