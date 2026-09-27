"""RULE-L1 across every stage-1/2 path: migration lock -> profile lifecycle locks (sorted) -> store
locks (stage-2 review r1-4).

``lock_order_guard`` wraps the ONE file-lock helper every protocol lock goes through
(``hermes_cli.auth._file_lock``: the migration lock, lifecycle locks, store locks) and fails the test
on any NEW acquisition of a lower-ranked lock while the thread holds a higher-ranked one, and on a
lifecycle lock taken out of sorted order. Re-entry of a lock already held is not an acquisition.
Each scenario then drives a real entry point (fake credentials only).
"""

import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

import hermes_cli.auth as auth
from hermes_cli import auth_codex_profile_ops as pops
from hermes_cli import profiles
from tests.hermes_cli.codex_authority_env import codex_home, make_profile, pool_row, registry, write_store  # noqa: F401
from tests.hermes_cli.test_codex_profile_lifecycle import (  # noqa: F401
    _dispatch, _failing_move, _refresh_in, _work_with_login, quiet_profiles)


def _rank(path: Path) -> int:
    if path.name == "auth.migration.lock":
        return 0
    if path.parent.name == ".locks":
        return 1
    return 2


@pytest.fixture
def lock_order_guard(monkeypatch):
    real = auth._file_lock
    held = threading.local()
    violations = []

    @contextmanager
    def guarded(lock_path, holder, *args, **kwargs):
        stack = getattr(held, "stack", None)
        if stack is None:
            stack = held.stack = []
        path = Path(lock_path)
        fresh = getattr(holder, "depth", 0) == 0
        if fresh:
            rank = _rank(path)
            for other in stack:
                if _rank(other) > rank:
                    violations.append(f"{path} (rank {rank}) acquired while holding {other}")
                elif _rank(other) == rank == 1 and str(other).lower() > str(path).lower():
                    violations.append(f"lifecycle {path.name} acquired after {other.name} (unsorted)")
        with real(lock_path, holder, *args, **kwargs):
            if fresh:
                stack.append(path)
            try:
                yield
            finally:
                if fresh:
                    stack.remove(path)

    monkeypatch.setattr(auth, "_file_lock", guarded)
    yield violations
    assert not violations, violations


def test_refresh_sign_in_pool_and_dispatch_follow_the_lock_order(codex_home, lock_order_guard, monkeypatch):
    root, endpoint = codex_home
    pair = endpoint.issue()
    write_store(root, singleton=pair, pool=[pool_row("rootpo", endpoint.issue(), source="manual:device_code")])
    work = make_profile(root, "work")
    write_store(work, singleton=pair)  # a copy row of root's chain: refresh locks work AND root
    other = make_profile(root, "other")
    write_store(other, singleton=endpoint.issue())
    _dispatch()
    assert _refresh_in(monkeypatch, work)
    assert _refresh_in(monkeypatch, other)
    from agent.credential_pool import load_pool
    assert load_pool("openai-codex").try_refresh_matching(credential_id="rootpo") is not None
    monkeypatch.setenv("HERMES_HOME", str(other))
    fresh = endpoint.issue()
    auth._save_codex_tokens({"access_token": fresh[0], "refresh_token": fresh[1]})
    monkeypatch.setenv("HERMES_HOME", str(root))
    from hermes_cli.auth_codex_authority import migrate
    migrate()


def test_profile_ops_follow_the_lock_order(codex_home, quiet_profiles, lock_order_guard, monkeypatch):
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    zeta = make_profile(root, "zeta")
    write_store(zeta, singleton=endpoint.issue())
    from hermes_cli.auth_codex_authority import migrate
    migrate()  # registers zeta's login (the bootstrap ran before it existed)
    with monkeypatch.context() as m:
        _failing_move(m)
        with pytest.raises(ValueError):
            profiles.rename_profile("work", "alpha")  # a destination sorting before other profiles
    (op_id,) = registry(root)["ops"]
    assert pops.recover_op(op_id) == "rolled_forward"
    with monkeypatch.context() as m:
        _failing_move(m)
        with pytest.raises(ValueError):
            profiles.rename_profile("zeta", "beta")
    (op_id,) = registry(root)["ops"]
    assert pops.recover_op(op_id, abandon=True) == "abandoned"
    profiles.rename_profile("zeta", "beta")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "alpha"))
    pops.transfer_chain(root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    profiles.delete_profile("beta", yes=True)
