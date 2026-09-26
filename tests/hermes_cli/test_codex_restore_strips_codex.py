"""Stage 1: a restore NEVER brings Codex credential material back.

Nothing in a backup proves a Codex refresh token is still unspent, and restoring the authority's
own files would roll its evidence back. So a restore writes no protocol file (registry and its
copies, retired-token index, ``auth.recovery/``, ``auth.ops/``, rescue files, locks), no Codex CLI
credential (``_external/.codex/...``), and every restored ``auth.json`` has its Codex material
removed while the destination's CURRENT Codex material is kept exactly as it is — so a restore over
a live Codex login neither restores old tokens nor breaks that login's registry. Every other
provider restores as upstream does. A Codex user signs in again after restoring. Fake credentials.
"""

import json
import zipfile
from types import SimpleNamespace

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, pool_row, read_json, registry, write_store  # noqa: F401
from tests.hermes_cli.codex_restore_children import restore, runtime_startup

ANTHROPIC = {"id": "anth01", "label": "claude", "auth_type": "oauth", "priority": 0, "source": "manual",
             "access_token": "sk-ant-oat-restored", "refresh_token": "rt-ant-restored"}
OPENROUTER = {"id": "orkey1", "label": "router", "auth_type": "api_key", "priority": 0, "source": "manual",
              "access_token": "sk-or-restored"}
PROTOCOL = ("auth.chains.json", "auth.chains.retired.json", "auth.recovery/c0ffee.json", "auth.ops/op1.json",
            "auth.codex-rotation-rescue.json")


def _archive(tmp_path, endpoint):
    """A backup whose auth.json holds Codex tokens (singleton + pool row) next to other providers,
    plus the authority's protocol files and a Codex CLI credential under ``_external/``."""
    old, old_pool, cli = endpoint.issue(), endpoint.issue(), endpoint.issue()
    store = {"version": 1, "active_provider": "openai-codex",
             "providers": {"openai-codex": {"tokens": {"access_token": old[0], "refresh_token": old[1]}}},
             "credential_pool": {"openai-codex": [pool_row("oldrow", old_pool, source="manual:device_code")],
                                 "anthropic": [ANTHROPIC], "openrouter": [OPENROUTER]}}
    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:  # protocol files BEFORE auth.json: a crash lands after them
        zf.writestr("config.yaml", "model: {}\n")
        for rel in PROTOCOL:
            zf.writestr(rel, json.dumps({"version": 1, "ops": {}, "chains": {}, "from": "the backup"}))
        zf.writestr("auth.json", json.dumps(store))
        zf.writestr("_external/.codex/auth.json", json.dumps({"tokens": {"access_token": cli[0],
                                                                          "refresh_token": cli[1]}}))
        zf.writestr("zz-last.txt", "restored after auth.json")
    return archive, {old[0], old[1], old_pool[0], old_pool[1], cli[0], cli[1]}


def _codex_sections(store):
    return {"provider": (store.get("providers") or {}).get("openai-codex"),
            "pool": (store.get("credential_pool") or {}).get("openai-codex"),
            "suppressed": (store.get("suppressed_sources") or {}).get("openai-codex")}


def _protocol_state(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and (p.name.startswith("auth.chains") or "auth.recovery" in p.parts
                                or "auth.ops" in p.parts or p.name == "auth.codex-rotation-rescue.json")}


@pytest.fixture
def quiet_backup(monkeypatch):
    from hermes_cli import backup
    monkeypatch.setattr(backup, "_revive_gateway_after_import", lambda hermes_root: None)
    monkeypatch.setattr(backup, "_restore_profile_wrappers", lambda hermes_root: [])
    return backup


@pytest.mark.parametrize("install", ["fresh", "live-codex-login"])
def test_a_backup_never_brings_codex_material_back(codex_home, tmp_path, quiet_backup, capsys, install):
    from agent.credential_pool import load_pool
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    userhome = tmp_path / "userhome"
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    archive, backup_tokens = _archive(tmp_path, endpoint)
    live = None
    if install == "live-codex-login":
        live = endpoint.issue()
        write_store(root, singleton=live)
        assert authority_dispatch() == "bootstrapped"
    before_protocol, before_codex = _protocol_state(root), _codex_sections(read_json(root / "auth.json")) \
        if live else None

    quiet_backup.run_import(SimpleNamespace(zipfile=str(archive), force=True))

    assert _protocol_state(root) == before_protocol, "a protocol file came from the backup"
    assert not (userhome / ".codex").exists(), "a Codex CLI credential came from the backup"
    store = read_json(root / "auth.json")
    assert not backup_tokens & set(json.dumps(store).split('"')), "Codex tokens from the backup landed"
    if live:
        assert _codex_sections(store) == before_codex, "the live Codex login was changed"
        assert registry(root)["chains"] and all(c["state"] == "active" for c in registry(root)["chains"].values())
    else:
        assert _codex_sections(store) == {"provider": None, "pool": None, "suppressed": None}
        assert store.get("active_provider") != "openai-codex"
    for provider, row in (("anthropic", ANTHROPIC), ("openrouter", OPENROUTER)):
        entry = load_pool(provider).select()
        assert entry is not None and entry.access_token == row["access_token"], provider
    assert "sign in" in capsys.readouterr().out.lower(), "the restore output tells a Codex user to sign in"
    started = runtime_startup(tmp_path, userhome, root)
    assert not backup_tokens & set(started["presented"]), f"a backup token reached the endpoint: {started}"
    if not live:
        assert started["presented"] == []


@pytest.mark.parametrize("install", ["fresh", "live-codex-login"])
def test_a_restore_that_dies_midway_leaves_no_codex_material_from_the_backup(codex_home, tmp_path, install):
    """A restorer killed right after the store merge; then a SEPARATE process starts normally and
    forces a refresh: no backup token is presented (nothing at all on a fresh install; only the live
    login's own token otherwise), and the live login is exactly as before."""
    from hermes_cli.auth_codex_authority import authority_dispatch
    root, endpoint = codex_home
    userhome = tmp_path / "userhome"
    (root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    archive, backup_tokens = _archive(tmp_path, endpoint)
    live = None
    if install == "live-codex-login":
        live = endpoint.issue()
        write_store(root, singleton=live)
        assert authority_dispatch() == "bootstrapped"
    before_protocol = _protocol_state(root)
    before_codex = _codex_sections(read_json(root / "auth.json")) if live else None
    proc, log, _gate = restore(archive, tmp_path, userhome, point="after-auth-json", mode="die")
    out, err = proc.communicate(timeout=180)
    assert proc.returncode == 137, err[-2000:]
    assert _protocol_state(root) == before_protocol, "a protocol file came from the backup"
    assert not (userhome / ".codex").exists()
    on_disk = (root / "auth.json").read_text(encoding="utf-8") if (root / "auth.json").exists() else ""
    assert not any(token in on_disk for token in backup_tokens), "Codex tokens from the backup are on disk"
    if live:
        assert _codex_sections(read_json(root / "auth.json")) == before_codex, "the live Codex login was changed"

    started = runtime_startup(tmp_path, userhome, root)
    assert not backup_tokens & set(started["presented"]), f"a backup token reached the endpoint: {started}"
    assert started["presented"] == ([live[1]] if live else []), started
