"""§13 fault matrix at the stage-1 scope: a crash at every boundary, then the END STATE.

A "crash" is a ``SystemExit`` raised at the boundary (the process dies there: nothing after it runs,
no handler in the protocol catches it); the "next process" is a fresh call through the real entry
point. The fake endpoint's ledger (``endpoint.calls``) is the oracle for "no automatic replay".

Refresh boundaries (design §5 step 6):
- after the intent is recorded, before the POST        -> next call fences indeterminate, no POST;
- after the response arrived, before ``pending``         -> same (the successor existed only in memory);
- after ``pending``, before the rows are written         -> next call ADOPTS the successor, no POST;
- after the rows, before the completion record           -> next call completes it, no POST.
Ownership-op boundaries (§4): after PAYLOAD, after DECISION, after the store edits of APPLY, before
COMMIT -> abandoned before the decision, rolled forward to the decided state after it — by the
scheduled job alone as well as by an ordinary request.
"""

import pytest

import hermes_cli.auth as auth
from hermes_cli import auth_codex_refresh_txn as txn
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (  # noqa: F401
    codex_home, read_json, registry, write_store)


class Killed(SystemExit):
    pass


def _tokens(root):
    t = read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"]
    return t["access_token"], t["refresh_token"]


def _crash_write_attempt(monkeypatch, *, after_state=None, before_completion=False):
    real = txn._write_attempt

    def wrapped(owner, record, attempt, **kw):
        if before_completion and attempt is None and record.get("attempt") is None and kw == {} \
                and record.get("history") and record["history"][-1].get("outcome") == "refreshed":
            raise Killed("killed before the completion record")
        result = real(owner, record, attempt, **kw)
        if after_state and attempt and attempt.get("state") == after_state:
            raise Killed(f"killed after {after_state}")
        return result

    monkeypatch.setattr(txn, "_write_attempt", wrapped)


@pytest.mark.parametrize("boundary", ["after-intent", "after-response"])
def test_crash_before_pending_fences_and_never_replays(codex_home, monkeypatch, boundary):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    with monkeypatch.context() as crash:
        if boundary == "after-intent":
            _crash_write_attempt(crash, after_state="intent")
        else:
            real_endpoint = endpoint.__call__

            def dies_after_response(*a, **k):
                real_endpoint(*a, **k)
                raise Killed("killed after the response arrived")

            crash.setattr(auth, "refresh_codex_oauth_pure", dies_after_response)
        with pytest.raises(Killed):
            auth.resolve_codex_runtime_credentials(force_refresh=True)
    posts = len(endpoint.calls)
    with pytest.raises(AuthError) as err:
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert err.value.code == "codex_refresh_indeterminate"
    assert len(endpoint.calls) == posts, "an intent found at start is never replayed"


def test_crash_after_pending_adopts_the_successor(codex_home, monkeypatch):
    root, endpoint = codex_home
    before = endpoint.issue()
    write_store(root, singleton=before)
    with monkeypatch.context() as crash:
        _crash_write_attempt(crash, after_state="pending")
        with pytest.raises(Killed):
            auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert _tokens(root) == before, "the rows were never written"
    posts = len(endpoint.calls)
    # The next transaction reconciles first: rows hold the predecessor PAIR -> adopt the successor.
    # The caller started from the predecessor, so the successor is returned as a peer's rotation.
    key = auth.resolve_codex_runtime_credentials(force_refresh=True)["api_key"]
    assert len(endpoint.calls) == posts, "the recorded successor is adopted, nothing is re-sent"
    assert _tokens(root) != before and key == _tokens(root)[0]
    (chain,) = registry(root)["chains"].values()
    assert chain["state"] == "active"


def test_crash_after_rows_completes_without_a_post(codex_home, monkeypatch):
    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    with monkeypatch.context() as crash:
        _crash_write_attempt(crash, before_completion=True)
        with pytest.raises(Killed):
            auth.resolve_codex_runtime_credentials(force_refresh=True)
    rotated = _tokens(root)
    posts = len(endpoint.calls)
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # reconciles (completes), then refreshes
    assert len(endpoint.calls) == posts + 1
    assert endpoint.calls[-1] == rotated[1], "the refresh continued from the completed successor"


@pytest.mark.parametrize("boundary", ["payload", "decision", "apply-stores", "commit"])
@pytest.mark.parametrize("recovered_by", ["request", "job"])
def test_sign_in_crash_at_every_op_step(codex_home, monkeypatch, boundary, recovered_by):
    from hermes_cli import auth_codex_ops as ops
    from hermes_cli.auth_codex_authority import authority_dispatch, startup_dispatch

    root, endpoint = codex_home
    old = endpoint.issue()
    write_store(root, singleton=old)
    assert authority_dispatch() == "bootstrapped"
    new = endpoint.issue()
    with monkeypatch.context() as crash:
        if boundary == "payload":
            crash.setattr(ops, "write_registry", lambda data: (_ for _ in ()).throw(Killed("x")))
        elif boundary == "decision":
            crash.setattr(ops, "apply_payload", lambda body: (_ for _ in ()).throw(Killed("x")))
        elif boundary == "apply-stores":
            crash.setattr(ops, "_apply_record_action", lambda action: (_ for _ in ()).throw(Killed("x")))
        else:
            crash.setattr(ops, "commit_op", lambda op_id: (_ for _ in ()).throw(Killed("x")))
        with pytest.raises(Killed):
            auth._save_codex_tokens({"access_token": new[0], "refresh_token": new[1]})
    decided = boundary != "payload"
    current = new if decided else old
    if recovered_by == "job":
        startup_dispatch()  # the scheduled job alone, no request
        assert _tokens(root) == current, (boundary, recovered_by)
        assert not list((root / "auth.ops").glob("*.json")), "undecided payload archived / op committed"
    else:
        # An ordinary request: admission finds the unfinished op, dispatches, then refreshes the
        # recovered (decided) credential — never the replaced one.
        key = auth.resolve_codex_runtime_credentials(force_refresh=True)["api_key"]
        if decided:
            assert old[1] not in endpoint.calls, "the replaced login is never refreshed"
            # Rows not yet applied: the recovered pair is returned as-is (a peer's move); applied:
            # the recovered credential itself is refreshed.
            assert key == new[0] or endpoint.calls[-1] == new[1], (boundary, recovered_by)
        else:
            assert endpoint.calls[-1] == old[1]
    reg = registry(root)
    assert not reg["ops"], "no op left unfinished"
    active = [c for c in reg["chains"].values() if c["state"] == "active"]
    assert len(active) == 1
