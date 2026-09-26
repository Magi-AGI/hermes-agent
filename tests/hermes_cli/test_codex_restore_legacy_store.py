"""Keeping this machine's Codex login never disables a legacy store's other logins (stage 1).

A restore writes the backup's auth.json minus Codex material PLUS the destination's current Codex
sections. Restoring a legacy-format store (``systems.nous_portal``) onto an install with a Codex
login added ``providers.openai-codex`` to it, so the loader took its modern-format branch and never
converted the legacy Nous section: the Nous login vanished. The legacy format is now converted with
upstream's own conversion BEFORE the Codex sections are merged. Fake credentials only.
"""

import json
import zipfile
from types import SimpleNamespace

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, read_json, write_store  # noqa: F401

NOUS = {"access_token": "nous-at-restored", "refresh_token": "nous-rt-restored", "token_type": "Bearer",
        "client_id": "hermes-cli"}


@pytest.mark.parametrize("how", ["import", "snapshot"])
def test_a_legacy_nous_login_and_the_live_codex_login_are_both_usable_after_restore(
        codex_home, tmp_path, monkeypatch, how):
    import hermes_cli.auth as auth
    from hermes_cli import backup
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    live = endpoint.issue()
    write_store(root, singleton=live)
    assert authority_dispatch() == "bootstrapped"
    kept_codex = read_json(root / "auth.json")["providers"]["openai-codex"]
    legacy = json.dumps({"systems": {"nous_portal": NOUS}}).encode()

    if how == "import":
        archive = tmp_path / "old.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("config.yaml", "model: {}\n")
            zf.writestr("auth.json", legacy)
        backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))
    else:
        snap = backup._quick_snapshot_root(root) / "20260926-000000-legacy"
        snap.mkdir(parents=True)
        (snap / "auth.json").write_bytes(legacy)
        (snap / "manifest.json").write_text(json.dumps({"id": snap.name, "files": {"auth.json": len(legacy)}}),
                                            encoding="utf-8")
        assert backup.restore_quick_snapshot(snap.name, hermes_home=root) is True

    nous = auth.get_provider_auth_state("nous")
    assert nous is not None and nous.get("access_token") == NOUS["access_token"], "the legacy Nous login vanished"
    assert auth._load_auth_store()["providers"]["openai-codex"] == kept_codex
    assert auth.resolve_codex_runtime_credentials()["api_key"] == live[0], "the live Codex login is not usable"
