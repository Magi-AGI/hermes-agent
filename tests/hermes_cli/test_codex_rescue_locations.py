"""Legacy rescue files are found at EVERY path a store is reached by (design §7, stage 1).

Rescue detection scanned beside each store path AFTER aliases were collapsed to one canonical
store, and only for profiles that had an auth.json: a rev-0 rescue beside a profile whose auth.json
is a link to root's — or beside a profile that borrows root's login and has no auth.json at all —
was never seen, and root kept refreshing a chain with evidence that a predecessor had been consumed.
Rescue locations are now enumerated per profile directory before deduplication and each rescue is
mapped to the canonical store it belongs to (a borrowing profile's rescue fences root's chain).
Fake credentials only.
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import codex_home, make_profile, registry, write_store  # noqa: F401


def _rescue(directory):
    (directory / "auth.codex-rotation-rescue.json").write_text(
        json.dumps({"entries": {"singleton": {"access_token": "x", "refresh_token": "rt-rescued"}}}),
        encoding="utf-8")


def _root_refresh_is_blocked(root, endpoint):
    with pytest.raises(AuthError) as err:
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert err.value.code == "codex_recovery_fenced"
    assert endpoint.calls == [], "root refreshed a chain a rescue says was already consumed"
    (chain,) = registry(root)["chains"].values()
    assert chain["fence"]["reason"] == "legacy-rescue"


def test_a_rescue_beside_a_borrowing_profile_without_auth_json_blocks_root(codex_home):
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    assert authority_dispatch() == "bootstrapped"
    _rescue(make_profile(root, "work"))  # borrows root's login: no auth.json of its own
    _root_refresh_is_blocked(root, endpoint)


@pytest.mark.require_symlinks
def test_a_rescue_beside_a_profile_linked_to_root_blocks_root(codex_home):
    from hermes_cli.auth_codex_authority import authority_dispatch

    root, endpoint = codex_home
    write_store(root, singleton=endpoint.issue())
    work = make_profile(root, "work")
    (work / "auth.json").symlink_to(root / "auth.json")
    assert authority_dispatch() == "bootstrapped"
    _rescue(work)
    _root_refresh_is_blocked(root, endpoint)
