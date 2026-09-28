"""Stage 2 review round 7 (Codex, reviewed head 3c7242e3a5): strict directory listings must still
match names the way the platform compares them.

The round-6 sweep replaced ``Path.glob("*.json")`` with a case-sensitive ``endswith(".json")``: on
Windows a ``<chain>.JSON`` record that ``read_record()`` still reads was skipped by the registry
rebuild, which forgot the consumed token it listed, admitted its import as a new login and let the
endpoint receive it twice. Every converted listing now shares one filter (``listdir_matching``).
Plus the reviewer's listing-denial checks for the rebuild and rescue archival. Fake credentials only.
"""

import errno
import json
import os

import pytest

import hermes_cli.auth as auth
from tests.hermes_cli.codex_authority_env import codex_home, registry, write_store  # noqa: F401


def _deny_listing(monkeypatch, directory):
    original = os.scandir
    target = os.path.normcase(os.path.abspath(directory))

    def denied(path):
        if not isinstance(path, int) and os.path.normcase(os.path.abspath(path)) == target:
            raise PermissionError(errno.EACCES, "review: listing denied", str(directory))
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)


def test_rebuild_listing_denial_preserves_the_damaged_registry(codex_home, monkeypatch):
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    damaged = b"{ damaged registry"
    (root / "auth.chains.json").write_bytes(damaged)
    before_store = (root / "auth.json").read_bytes()
    fresh = endpoint.issue()
    _deny_listing(monkeypatch, root / "auth.recovery")
    with pytest.raises(PermissionError, match="listing denied"):
        auth._save_codex_tokens({"access_token": fresh[0], "refresh_token": fresh[1]})
    assert (root / "auth.chains.json").read_bytes() == damaged
    assert (root / "auth.json").read_bytes() == before_store
    assert endpoint.calls == []


def test_rescue_archive_listing_denial_keeps_original_and_fence(codex_home, monkeypatch):
    from hermes_cli.auth_codex_authority import authority_dispatch
    from tests.hermes_cli.test_codex_rescue_archival import RESCUE, _rescue

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    _rescue(root, endpoint.issue())
    original = (root / RESCUE).read_bytes()
    _deny_listing(monkeypatch, root / "auth.ops" / "archive")
    assert authority_dispatch() == "failed"
    assert (root / RESCUE).read_bytes() == original
    assert all(c["state"] == "fenced" for c in registry(root)["chains"].values())
    assert endpoint.calls == []


@pytest.mark.windows_only
@pytest.mark.parametrize("extension", [".json", ".JSON", ".Json"])
def test_rebuild_keeps_consumed_history_for_case_equivalent_record_names(codex_home, extension):
    from hermes_cli.auth_codex_authority import import_pair
    from hermes_cli.auth_codex_records import read_record

    root, endpoint = codex_home
    used = endpoint.issue()
    write_store(root, singleton=used)
    auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert endpoint.calls == [used[1]]
    (chain_id,) = registry(root)["chains"]
    record = root / "auth.recovery" / f"{chain_id}.json"
    if extension != record.suffix:
        record.rename(record.with_suffix(extension))
    assert read_record(root / "auth.json", chain_id).status == "ok", "normal lookup accepts this name"
    (root / "auth.chains.json").write_text("{ damaged registry", encoding="utf-8")
    fresh = endpoint.issue()
    auth._save_codex_tokens({"access_token": fresh[0], "refresh_token": fresh[1]})
    result = import_pair(*used, reason="review-windows-record-casing")
    if result is not None:
        from hermes_cli.auth_constants import AuthError
        try:
            auth.resolve_codex_runtime_credentials(force_refresh=True)
        except AuthError:
            pass
    assert endpoint.calls.count(used[1]) == 1, "the fake endpoint received the consumed refresh token again"
    assert result is None, "rebuild forgot a consumed predecessor and admitted its import as a new login"


@pytest.mark.windows_only
@pytest.mark.parametrize("extension", [".JSON", ".Json"])
def test_an_undecided_payload_with_a_case_variant_name_is_archived(codex_home, extension):
    from hermes_cli.auth_codex_authority import authority_dispatch
    from hermes_cli.auth_codex_ops import unreferenced_payloads
    from hermes_cli.auth_codex_registry import read_registry

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    orphan = root / "auth.ops" / f"undecided{extension}"
    orphan.write_text("{}", encoding="utf-8")
    assert [p.name for p in unreferenced_payloads(read_registry())] == [orphan.name]
    assert authority_dispatch() == "done"
    assert not orphan.exists()
    assert any(p.read_bytes() == b"{}" for p in (root / "auth.ops" / "archive").iterdir())


@pytest.mark.windows_only
def test_a_rescue_archive_with_a_case_variant_name_is_reused(codex_home):
    """A replay (copy done, original not yet removed) finds the archive already holding the bytes
    whatever the case of its name, instead of writing a second copy."""
    from hermes_cli.auth_codex_rescue import archive_rescue
    from hermes_cli.auth_codex_registry import migration_lock
    from hermes_cli.auth_credential_io import sha256_hex

    root, endpoint = codex_home
    raw = json.dumps({"entries": {"singleton": {"access_token": "a", "refresh_token": "r"}}}).encode()
    sha = sha256_hex(raw)
    archive = root / "auth.ops" / "archive"
    archive.mkdir(parents=True)
    existing = archive / f"legacy-rescue.{sha[:24]}.previous.UNTRUSTED.JSON"
    existing.write_bytes(raw)
    rescue = root / "auth.codex-rotation-rescue.json"
    rescue.write_bytes(raw)
    with migration_lock():
        assert archive_rescue(rescue, raw, sha) == existing
    assert not rescue.exists() and [p.name for p in archive.iterdir()] == [existing.name]
