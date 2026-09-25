"""A restore never makes a Codex login refreshable again (credential recovery protocol, stage 1).

Nothing in a backup proves its refresh tokens are unspent: back up R0, rotate to R1, reinstall at
the same path, restore — the restored registry and chain record used to authorize R0 again. Every
restore that brings Codex credential material in (Codex tokens in an auth.json, or any protocol
file) now lands with every restored chain durably FENCED ``restored-from-backup``:

- a RESTORE BARRIER is committed before the first byte is extracted and honoured by every
  dispatcher / ownership op / refresh admission until the fence is durable, so a crash at any step
  (mid-extraction included, with an auth.json on disk and no registry) never leaves a restored or
  unregistered credential refreshable or bootstrapped active;
- ``--retry-refresh`` refuses the fence; the exit is a fresh sign-in, which replaces the chain and
  keeps the row's label;
- other providers' restored credentials restore and work normally.

A restore over an install that already HAS Codex authority state stays refused (it would overwrite
newer protocol evidence): ``test_codex_credential_state_restore``. Fake credentials only.
"""

import json
import shutil
import zipfile
from types import SimpleNamespace

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from hermes_cli.auth_codex_registry import CodexAuthorityRefused
from tests.hermes_cli.codex_authority_env import codex_home, pool_row, read_json, registry, write_store  # noqa: F401


class Killed(SystemExit):
    pass


def _backup_then_reinstall(root, endpoint, tmp_path):
    """Back up an install holding R0 (bootstrapped), spend R0 (-> R1), wipe the install."""
    from hermes_cli.auth_codex_authority import authority_dispatch

    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    r0 = endpoint.issue()
    write_store(root, singleton=r0, pool=[pool_row("alias1", r0, label="mine")])
    assert authority_dispatch() == "bootstrapped"
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                zf.writestr(p.relative_to(root).as_posix(), p.read_bytes())
    auth.resolve_codex_runtime_credentials(force_refresh=True)  # R0 is spent
    shutil.rmtree(root)
    root.mkdir()
    return archive, r0


@pytest.fixture
def quiet_backup(monkeypatch):
    from hermes_cli import backup
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    return backup


@pytest.mark.parametrize("crash", [None, "mid-extraction", "before-fence", "mid-fence", "before-barrier-clear"])
def test_a_restored_codex_login_is_fenced_at_every_step(codex_home, tmp_path, monkeypatch, quiet_backup, crash):
    from hermes_cli import auth_codex_ops as ops
    from hermes_cli.auth_codex_authority import grant_retry_override

    root, endpoint = codex_home
    archive, _r0 = _backup_then_reinstall(root, endpoint, tmp_path)
    posts = len(endpoint.calls)
    with monkeypatch.context() as inject:
        if crash == "mid-extraction":  # auth.json lands, then the process dies (registry maybe not yet)
            real = quiet_backup._extract_member_atomically

            def extract(zf, member, target, *a, **k):
                real(zf, member, target, *a, **k)
                if member == "auth.json":
                    raise Killed("x")
            inject.setattr(quiet_backup, "_extract_member_atomically", extract)
        elif crash == "before-fence":
            from hermes_cli import auth_codex_restore as restore
            inject.setattr(restore, "fence_restored", lambda: (_ for _ in ()).throw(Killed("x")))
        elif crash == "mid-fence":  # the fencing registry write itself dies
            from hermes_cli import auth_codex_registry as reg
            for module in (reg, ops):
                inject.setattr(module, "write_registry", lambda data: (_ for _ in ()).throw(Killed("x")))
        elif crash == "before-barrier-clear":  # fenced, but the barrier is still there
            from hermes_cli.auth_credential_io import unlink_retrying
            inject.setattr("hermes_cli.auth_credential_io.unlink_retrying",
                           lambda path: (_ for _ in ()).throw(Killed("x"))
                           if str(path).endswith("auth.restore-barrier.json") else unlink_retrying(path))
        run = lambda: quiet_backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))  # noqa: E731
        if crash:
            with pytest.raises(Killed):
                run()
        else:
            run()
    # The next process: an ordinary forced refresh never presents a restored token.
    with pytest.raises(AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert len(endpoint.calls) == posts, "a restored refresh token was presented to the endpoint"
    live = [c for c in registry(root)["chains"].values() if c["state"] != "retired"]
    assert live and all(c["state"] == "fenced" and c["fence"]["reason"] == "restored-from-backup" for c in live)
    assert not (root / "auth.restore-barrier.json").exists()
    (cid,) = [c for c, e in registry(root)["chains"].items() if e["state"] != "retired"]
    with pytest.raises(CodexAuthorityRefused):
        grant_retry_override(chain=cid)
    fresh = endpoint.issue()
    auth._save_codex_tokens({"access_token": fresh[0], "refresh_token": fresh[1]})
    assert auth.resolve_codex_runtime_credentials()["api_key"] == fresh[0]
    alias = next(r for r in read_json(root / "auth.json")["credential_pool"]["openai-codex"] if r["id"] == "alias1")
    assert alias["refresh_token"] == fresh[1] and alias["label"] == "mine", "sign-in replaces, keeps the label"


def test_other_providers_restore_and_work_normally(codex_home, tmp_path, quiet_backup):
    from agent.credential_pool import load_pool

    root, endpoint = codex_home
    anthropic = {"id": "anth01", "label": "claude", "auth_type": "oauth", "priority": 0, "source": "manual",
                 "access_token": "sk-ant-oat-restored", "refresh_token": "rt-ant-restored"}
    api_key = {"id": "orkey1", "label": "router", "auth_type": "api_key", "priority": 0, "source": "manual",
               "access_token": "sk-or-restored"}
    store_extra = {"anthropic": [anthropic], "openrouter": [api_key]}
    archive, _r0 = _backup_then_reinstall(root, endpoint, tmp_path)
    # Add the other providers' rows to the archived auth.json.
    with zipfile.ZipFile(archive) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    store = json.loads(members["auth.json"])
    store["credential_pool"].update(store_extra)
    members["auth.json"] = json.dumps(store).encode()
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    quiet_backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    for provider, row in (("anthropic", anthropic), ("openrouter", api_key)):
        entry = load_pool(provider).select()
        assert entry is not None and entry.access_token == row["access_token"], provider
        assert entry.last_status in (None, "ok"), provider


def test_a_sign_in_right_after_an_interrupted_restore_is_not_fenced_as_restored(codex_home):
    """An interrupted restore left the barrier AND a damaged registry. A fresh sign-in must finish
    the restore's fence FIRST; it used to rebuild the registry without consuming the barrier, and
    the next dispatch then fenced the NEW login as restored-from-backup."""
    from hermes_cli.auth_codex_authority import authority_dispatch
    from hermes_cli.auth_codex_registry import migration_lock
    from hermes_cli.auth_codex_restore import write_restore_barrier

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    with migration_lock():
        write_restore_barrier("restoring it from a backup")
    (root / "auth.chains.json").write_text("{ damaged by the interrupted restore", encoding="utf-8")
    fresh = endpoint.issue()
    auth._save_codex_tokens({"access_token": fresh[0], "refresh_token": fresh[1]})
    authority_dispatch()
    assert auth.resolve_codex_runtime_credentials()["api_key"] == fresh[0]
    active = [c for c in registry(root)["chains"].values() if c["state"] == "active"]
    assert len(active) == 1, "the fresh sign-in was fenced as restored"
    assert not (root / "auth.restore-barrier.json").exists()
