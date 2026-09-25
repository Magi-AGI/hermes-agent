"""Rescue evidence that cannot be CHECKED is never treated as absent (design §7, fail closed).

The rescue scan used ``os.path.lexists`` (False on ANY ``OSError``, a permission error included)
and suppressed every ``OSError`` from the profile enumeration: an access error hid a legacy rescue
holding the successor of the consumed pair in auth.json, and admission presented that consumed
pair again. Only a genuinely missing path (ENOENT / ENOTDIR) is absent now; a failed metadata check
or enumeration refuses admission (or fences), and no refresh request reaches the endpoint.
Fake credentials only.
"""

import os

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import codex_home, make_profile, write_store  # noqa: F401

RESCUE = "auth.codex-rotation-rescue.json"


def _rescue(directory):
    (directory / RESCUE).write_text('{"entries": {"singleton": {"refresh_token": "rt-successor"}}}',
                                    encoding="utf-8")


@pytest.mark.parametrize("failure", ["rescue-stat", "profile-enumeration"])
def test_an_unverifiable_rescue_scan_sends_nothing(codex_home, monkeypatch, failure):
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())  # the pair a rev-0 rescue says was consumed
    work = make_profile(root, "work")
    assert authority_dispatch() == "bootstrapped"
    if failure == "rescue-stat":
        _rescue(root)
        real = os.lstat

        def lstat(path, *a, **k):
            if os.fspath(path).endswith(RESCUE):
                raise PermissionError(13, "Access is denied", os.fspath(path))
            return real(path, *a, **k)
        monkeypatch.setattr(os, "lstat", lstat)
    else:
        _rescue(work)  # beside a borrowing profile: found only by enumerating profiles/
        real = os.scandir
        profiles_root = os.path.normcase(os.fspath(root / "profiles"))

        def scandir(path=".", *a, **k):
            if os.path.normcase(os.fspath(path)) == profiles_root:
                raise PermissionError(13, "Access is denied", os.fspath(path))
            return real(path, *a, **k)
        monkeypatch.setattr(os, "scandir", scandir)
    with pytest.raises(AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert endpoint.calls == [], "a refresh was sent although the rescue evidence could not be checked"
