"""Codex login transfers between stores (design rev 9.1 RULE-M3, §13 r5-3; stage 2).

A transfer moves a chain, its owner rows and its owner-local record (attempt, fence evidence, full
history) to another store as ONE ownership op: ``state`` / ``fence`` are copied unchanged, only
``owner`` changes, epoch +1 and the record's epoch rewritten in the same APPLY. The destination is
written before the source is emptied, so a crash at any step leaves the decided state recoverable
and never a row in two chains. Fake credentials only.
"""

import pytest

from hermes_cli import auth_codex_ops as ops
from hermes_cli.auth_codex_profile_ops import transfer_chain
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, make_profile, read_json, registry, write_store)
from tests.hermes_cli.test_codex_profile_lifecycle import _dispatch, _key, _only_chain, _refresh_in


class Killed(SystemExit):
    pass


def _profile_login(root, endpoint):
    work = make_profile(root, "work")
    pair = endpoint.issue()
    write_store(work, singleton=pair)
    write_store(root)
    _dispatch()
    return work, pair


def _codex_rows(path):
    store = read_json(path)
    rows = []
    if (store.get("providers") or {}).get("openai-codex"):
        rows.append("singleton")
    rows += [e["id"] for e in (store.get("credential_pool") or {}).get("openai-codex") or ()]
    return rows


def test_transfer_moves_rows_and_record_and_refresh_continues(codex_home, monkeypatch):
    root, endpoint = codex_home
    work, pair = _profile_login(root, endpoint)
    _refresh_in(monkeypatch, work)  # history: R0 -> R1
    cid, before = _only_chain(root)
    history = read_json(work / "auth.recovery" / f"{cid}.json")["history"]
    monkeypatch.setenv("HERMES_HOME", str(work))
    result = transfer_chain(root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    chain = registry(root)["chains"][cid]
    assert result["chain_id"] == cid and chain["owner"] == _key(root / "auth.json")
    assert chain["epoch"] == before["epoch"] + 1 and chain["state"] == "active"
    assert all(r["store"] == _key(root / "auth.json") for r in chain["rows"])
    record = read_json(root / "auth.recovery" / f"{cid}.json")
    assert record["epoch"] == chain["epoch"] and record["history"] == history, "history moves with the chain"
    assert not (work / "auth.recovery" / f"{cid}.json").exists()
    assert list((work / "auth.recovery" / "archive").glob(f"{cid}.transferred.*.json"))
    index = root / "auth.chains.retired.json"
    assert not index.exists() or cid not in read_json(index)["chains"], "a live chain is not retired material"
    assert not _codex_rows(work / "auth.json"), "the source keeps no copy"
    current = read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"]["refresh_token"]
    calls = len(endpoint.calls)
    assert _refresh_in(monkeypatch, root)
    assert endpoint.calls[calls:] == [current], "the transferred chain continued from its current token"


@pytest.mark.parametrize("reason", ["indeterminate", "registry-rebuilt", "legacy-rescue"])
def test_transfer_of_a_fenced_chain_stays_fenced(codex_home, monkeypatch, reason):
    root, endpoint = codex_home
    work, _pair = _profile_login(root, endpoint)
    cid, _ = _only_chain(root)
    from hermes_cli.auth_codex_registry import migration_lock, read_registry, write_registry
    with migration_lock():
        data = read_registry().data
        data["chains"][cid].update(state="fenced", fence={"reason": reason, "epoch": 1})
        write_registry(data)
    monkeypatch.setenv("HERMES_HOME", str(work))
    transfer_chain(root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    chain = registry(root)["chains"][cid]
    assert chain["state"] == "fenced" and chain["fence"] == {"reason": reason, "epoch": 1}
    with pytest.raises(AuthError):
        _refresh_in(monkeypatch, root)
    assert endpoint.calls == []


def test_transfer_refuses_a_destination_with_its_own_login(codex_home, monkeypatch):
    root, endpoint = codex_home
    work = make_profile(root, "work")
    write_store(work, singleton=endpoint.issue())
    write_store(root, singleton=endpoint.issue())
    _dispatch()
    snapshot = registry(root)
    monkeypatch.setenv("HERMES_HOME", str(work))
    with pytest.raises(AuthError, match="already has its own Codex login"):
        transfer_chain(root)
    assert registry(root) == snapshot


@pytest.mark.parametrize("boundary", ["payload", "decision", "apply-destination", "apply-source", "commit"])
def test_transfer_crash_at_every_step_recovers_the_decided_state(codex_home, monkeypatch, boundary):
    root, endpoint = codex_home
    work, _pair = _profile_login(root, endpoint)
    cid, before = _only_chain(root)
    work_key = _key(work / "auth.json")
    with monkeypatch.context() as crash:
        if boundary == "payload":
            crash.setattr(ops, "write_registry", lambda data: (_ for _ in ()).throw(Killed(boundary)))
        elif boundary == "decision":
            crash.setattr(ops, "apply_payload", lambda body: (_ for _ in ()).throw(Killed(boundary)))
        elif boundary == "commit":
            crash.setattr(ops, "commit_op", lambda *a, **k: (_ for _ in ()).throw(Killed(boundary)))
        else:
            real = ops._apply_store_edits
            victim = work_key if boundary == "apply-source" else _key(root / "auth.json")

            def edits(path, items):
                if _key(path) == victim:
                    raise Killed(boundary)
                return real(path, items)
            crash.setattr(ops, "_apply_store_edits", edits)
        monkeypatch.setenv("HERMES_HOME", str(work))
        with pytest.raises(Killed):
            transfer_chain(root)
        monkeypatch.setenv("HERMES_HOME", str(root))
    _dispatch()  # the job alone
    reg = registry(root)
    assert not reg["ops"]
    chain = reg["chains"][cid]
    rows_in = {r["store"] for r in chain["rows"]}
    if boundary == "payload":
        assert chain == before and _codex_rows(work / "auth.json")
        assert _refresh_in(monkeypatch, work)
    else:
        assert chain["owner"] == _key(root / "auth.json") and rows_in == {_key(root / "auth.json")}
        assert not _codex_rows(work / "auth.json"), "the source row was removed on recovery"
        assert read_json(root / "auth.recovery" / f"{cid}.json")["epoch"] == chain["epoch"]
        assert _refresh_in(monkeypatch, root)
    seen = set()
    for c in reg["chains"].values():
        for r in c["rows"]:
            assert (r["store"], r["row"]) not in seen, "a row in two chains"
            seen.add((r["store"], r["row"]))
    assert len(set(endpoint.calls)) == len(endpoint.calls), "a refresh token was presented twice"


def test_transfer_refuses_ends_that_are_one_file(codex_home, monkeypatch):
    """The destination's auth.json is the source file under another name. Through a bind-mounted
    directory, writing the destination and then emptying the source would empty both; a hard link
    (used here: it can be made in a test) is the same identity to the check, which refuses both."""
    import os
    root, endpoint = codex_home
    work, _pair = _profile_login(root, endpoint)
    other = make_profile(root, "other")
    os.link(work / "auth.json", other / "auth.json")
    snapshot = registry(root)
    monkeypatch.setenv("HERMES_HOME", str(work))
    with pytest.raises(AuthError, match="same credential file"):
        transfer_chain(other)
    assert registry(root) == snapshot and _codex_rows(work / "auth.json")
