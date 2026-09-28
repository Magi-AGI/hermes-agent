"""Stage 2 review round 2 (Codex, reviewed head 9aa81c2606): completion contracts, using real protocol and
helper implementations.

Only temporary homes and fake credentials. No live gateway/service operation.
I/O faults are injected below helpers that catch them and return failure values.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import profiles
from hermes_cli import profile_identity
from hermes_cli.auth_codex_registry import CodexAuthorityRefused
from tests.hermes_cli.codex_authority_env import codex_home, registry  # noqa: F401
from tests.hermes_cli.test_codex_profile_lifecycle import quiet_profiles, _work_with_login, _dispatch  # noqa: F401
from tests.hermes_cli.test_codex_stage2_r1_followups import _abandon_to_decision2

REAL_MIGRATE_IDENTITY = profile_identity._migrate_profile_identity
REAL_CREATE_WRAPPER = profiles.create_wrapper_script


def evidence(name, result):
    """The review saved these as JSON evidence; in the suite the assertion messages carry them."""


@pytest.mark.parametrize('deny', [True, False], ids=['identity-io-failure', 'identity-control'])
def test_identity_failure_keeps_rename_pending(codex_home, quiet_profiles, monkeypatch, deny):
    from hermes_state import SessionDB
    import hermes_state_registry

    root, endpoint = codex_home
    work, _pair = _work_with_login(root, endpoint)
    profiles.set_active_profile('work')
    scope = str(root / 'sessions')
    old_key = 'agent:work:review:chat'
    db = SessionDB(root / 'state.db')
    db.save_gateway_routing_entry(old_key, json.dumps({'session_key': old_key}), scope=scope)
    db.close()
    monkeypatch.setattr(profile_identity, '_migrate_profile_identity', REAL_MIGRATE_IDENTITY)
    monkeypatch.setattr(profiles, 'check_alias_collision', lambda _: 'test has no wrapper')
    calls = []
    real_acquire = hermes_state_registry.acquire

    def acquire(path, *args, **kwargs):
        calls.append(str(path))
        if deny and Path(path) == root / 'state.db':
            raise PermissionError(13, 'review: database inaccessible')
        return real_acquire(path, *args, **kwargs)

    monkeypatch.setattr(hermes_state_registry, 'acquire', acquire)
    error = None
    try:
        profiles.rename_profile('work', 'play')
    except ValueError as exc:
        error = str(exc)
    check = SessionDB(root / 'state.db')
    try:
        keys = sorted(check.load_gateway_routing_entries(scope=scope))
    finally:
        check.close()
    observed = {'deny': deny, 'error': error, 'ops': registry(root)['ops'], 'keys': keys,
                'active_profile': profiles.get_active_profile(), 'helper_calls': len(calls)}
    evidence('identity-failure' if deny else 'identity-control', observed)
    assert calls, observed
    if deny:
        assert old_key in keys, observed
        assert observed['ops'], 'Identity was not migrated, but COMMIT discarded its recovery operation: ' + str(observed)
    else:
        assert not observed['ops'] and error is None, observed
        assert keys == ['agent:play:review:chat'], observed


def test_wrapper_io_failure_keeps_rename_pending(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    monkeypatch.setattr(profiles, 'create_wrapper_script', REAL_CREATE_WRAPPER)
    monkeypatch.setattr(profiles, 'check_alias_collision', lambda _: None)
    target = profiles._wrapper_path('play')
    real_write = Path.write_text
    attempted = []

    def failing_write(path, *args, **kwargs):
        if path == target:
            attempted.append(str(path))
            raise PermissionError(13, 'review: wrapper inaccessible')
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', failing_write)
    error = None
    try:
        profiles.rename_profile('work', 'play')
    except ValueError as exc:
        error = str(exc)
    observed = {'error': error, 'ops': registry(root)['ops'],
                'wrapper_exists': target.exists(), 'attempted': bool(attempted)}
    evidence('wrapper-failure', observed)
    assert observed['attempted'] and not observed['wrapper_exists'], observed
    assert observed['ops'], 'Wrapper write failed, but COMMIT discarded its recovery operation: ' + str(observed)


def test_resolve_unrecoverable_reports_a_completed_abandonment_as_success(codex_home, quiet_profiles, monkeypatch):
    from hermes_cli.auth_codex_ops import resolve_unrecoverable

    root, endpoint = codex_home
    work, _pair, cid, op_id, epoch = _abandon_to_decision2(root, endpoint, monkeypatch)
    path = root / 'auth.ops' / (op_id + '.abandon.json')
    raw = path.read_bytes()
    path.write_bytes(b'{')
    _dispatch()
    assert registry(root)['ops'][op_id]['status'] == 'unrecoverable'
    path.write_bytes(raw)
    error, outcome = None, None
    try:
        outcome = resolve_unrecoverable(op_id)
    except CodexAuthorityRefused as exc:
        error = str(exc)
    after = registry(root)
    observed = {'error': error, 'outcome': outcome, 'ops': after['ops'],
                'epoch': after['chains'][cid]['epoch'], 'expected_epoch': epoch,
                'owner_exists': Path(after['chains'][cid]['owner']).is_file()}
    evidence('resolve-completed-abandonment', observed)
    assert not after['ops'] and observed['epoch'] == epoch and observed['owner_exists'], observed
    assert error is None, 'Recovery completed, but its command reports failure: ' + str(observed)
