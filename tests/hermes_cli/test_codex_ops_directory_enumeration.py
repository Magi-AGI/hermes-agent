"""Stage 2 review round 6 (Codex, reviewed head 7f35c693c0): an operations directory whose listing is
denied is never an empty payload list, and dispatch never reports it as done. Fake credentials only."""

import errno
import os

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, write_store  # noqa: F401


def _orphan(codex_home):
    from hermes_cli.auth_codex_authority import authority_dispatch
    from hermes_cli.auth_codex_registry import read_registry

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    directory = root / "auth.ops"
    orphan = directory / "undecided-review-payload.json"
    orphan.write_text("{}", encoding="utf-8")
    return directory, orphan, read_registry()


def _deny_enumeration(monkeypatch, directory):
    original = os.scandir
    target = os.path.normcase(os.path.abspath(directory))

    def denied(path):
        if not isinstance(path, int) and os.path.normcase(os.path.abspath(path)) == target:
            raise PermissionError(errno.EACCES, "review: directory listing denied", str(directory))
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)


def test_uninspectable_ops_directory_is_not_an_empty_payload_list(codex_home, monkeypatch):
    from hermes_cli.auth_codex_ops import unreferenced_payloads

    directory, orphan, view = _orphan(codex_home)
    assert unreferenced_payloads(view) == [orphan]
    _deny_enumeration(monkeypatch, directory)
    assert directory.stat(), "metadata remains readable; only listing is denied"
    with pytest.raises(PermissionError, match="directory listing denied"):
        unreferenced_payloads(view)


def test_dispatch_does_not_report_success_when_payload_enumeration_is_denied(codex_home, monkeypatch):
    from hermes_cli.auth_codex_authority import authority_dispatch
    from hermes_cli.auth_codex_registry import read_registry

    directory, orphan, view = _orphan(codex_home)
    _deny_enumeration(monkeypatch, directory)
    outcome = authority_dispatch()
    assert orphan.read_bytes() == b"{}", "undecided evidence must be retained"
    assert read_registry().chains == view.chains, "no credential authority should change"
    assert outcome == "failed", f"unreadable operations directory reported {outcome!r}"


def test_readable_orphan_is_archived_normally(codex_home):
    from hermes_cli.auth_codex_authority import authority_dispatch

    directory, orphan, _view = _orphan(codex_home)
    assert authority_dispatch() == "done"
    assert not orphan.exists()
    assert any(p.read_bytes() == b"{}" for p in (directory / "archive").glob("*.json"))


def test_startup_does_not_report_idle_when_payload_enumeration_is_denied(codex_home, monkeypatch):
    """The CLI startup hook turned any failure of its "is there work" check into "idle"."""
    from hermes_cli.auth_codex_authority import startup_dispatch

    directory, orphan, _view = _orphan(codex_home)
    _deny_enumeration(monkeypatch, directory)
    assert startup_dispatch() == "failed"
    assert orphan.read_bytes() == b"{}"
