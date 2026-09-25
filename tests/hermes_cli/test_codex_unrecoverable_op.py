"""An ownership op whose payload cannot be read keeps its entry and reservation (RULE-M1, RULE-G4).

Its chains are fenced ``op-unrecoverable``, but the op entry stays in the registry (status
``unrecoverable``): a payload that was only TEMPORARILY unreadable is rolled forward once it reads
again — the entry used to be deleted, so the next pass archived the payload as "abandoned" and the
decided sign-in was lost — and the op's reservation holds until an explicit, durable resolution
(``hermes auth recovery resolve <op>``), never released silently. Fake credentials only.
"""

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_codex_registry import CodexAuthorityRefused
from tests.hermes_cli.codex_authority_env import codex_home, read_json, registry, write_store  # noqa: F401


class Killed(SystemExit):
    pass


def _decided_sign_in_left_unapplied(root, endpoint, monkeypatch):
    from hermes_cli import auth_codex_ops as ops
    from hermes_cli.auth_codex_authority import authority_dispatch

    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    new = endpoint.issue()
    with monkeypatch.context() as crash:
        crash.setattr(ops, "apply_payload", lambda body: (_ for _ in ()).throw(Killed("x")))
        with pytest.raises(Killed):
            auth._save_codex_tokens({"access_token": new[0], "refresh_token": new[1]})
    (op_id,) = registry(root)["ops"]
    return op_id, new


def test_a_temporarily_unreadable_payload_is_rolled_forward_once_readable(codex_home, monkeypatch):
    from hermes_cli import auth_codex_ops as ops
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    op_id, new = _decided_sign_in_left_unapplied(root, endpoint, monkeypatch)
    real_read = ops.read_witnessed_bytes

    def unreadable_payload(path):
        if str(path).endswith(f"{op_id}.json"):
            raise PermissionError(13, "sharing violation")
        return real_read(path)

    with monkeypatch.context() as io_error:
        io_error.setattr(ops, "read_witnessed_bytes", unreadable_payload)
        authority_dispatch()
    assert registry(root)["ops"][op_id]["status"] == "unrecoverable", "the op entry (reservation) was dropped"
    authority_dispatch()  # readable again
    assert not registry(root)["ops"]
    tokens = read_json(root / "auth.json")["providers"]["openai-codex"]["tokens"]
    assert tokens["refresh_token"] == new[1], "the decided sign-in was lost"
    assert auth.resolve_codex_runtime_credentials()["api_key"] == new[0]


def test_an_unrecoverable_op_keeps_its_reservation_until_resolved(codex_home, monkeypatch):
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    op_id, _new = _decided_sign_in_left_unapplied(root, endpoint, monkeypatch)
    (root / "auth.ops" / f"{op_id}.json").unlink()  # the payload is gone for good
    authority_dispatch()
    again = endpoint.issue()
    with pytest.raises(CodexAuthorityRefused, match="recovery resolve"):
        auth._save_codex_tokens({"access_token": again[0], "refresh_token": again[1]})
    from hermes_cli.auth_codex_ops import resolve_unrecoverable
    assert resolve_unrecoverable(op_id) == "released"
    assert not registry(root)["ops"]
    assert all(c["state"] != "active" for c in registry(root)["chains"].values()), \
        "resolving never re-activates what the lost payload decided"
    auth._save_codex_tokens({"access_token": again[0], "refresh_token": again[1]})
    assert auth.resolve_codex_runtime_credentials()["api_key"] == again[0]
