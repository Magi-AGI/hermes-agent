"""Stage 2 review round 1 (Codex, reviewed head 15f5796b3f): the five reproduced blockers.

Real protocol operations, sandbox files, fake token endpoint. Service/process shutdown and wrapper
integration use the existing quiet fixture; no live install or credentials are used. Boundary
injection leaves durable state for the actual dispatcher/recovery to process.
"""

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import auth, profiles
from hermes_cli import auth_codex_profile_ops as pops
from hermes_cli.auth_constants import AuthError
from tests.hermes_cli.codex_authority_env import (
    codex_home, make_profile, pool_row, read_json, registry, write_store)
from tests.hermes_cli.test_codex_profile_lifecycle import (
    quiet_profiles, _dispatch, _failing_move, _key, _work_with_login)
from tests.hermes_cli.test_codex_lifecycle_recovery import _stuck_rename


def evidence(name, data):
    """The review saved these as JSON evidence; in the suite the assertion messages carry them."""


def test_recovery_finishes_the_user_visible_rename(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, _pair = _work_with_login(root, endpoint)
    profiles.set_active_profile('work')
    events = []
    monkeypatch.setattr(profiles, 'remove_wrapper_script', lambda name: events.append(['remove-wrapper', name]))
    monkeypatch.setattr(profiles, 'create_wrapper_script', lambda name: events.append(['create-wrapper', name]))
    import hermes_cli.profile_identity as identity
    monkeypatch.setattr(identity, '_migrate_profile_identity', lambda *args: events.append(['identity', *args[:2]]))
    with monkeypatch.context() as stuck:
        _failing_move(stuck)
        with pytest.raises(ValueError, match='recover --op'):
            profiles.rename_profile('work', 'play')
    op_id, = registry(root)['ops']
    outcome = pops.recover_op(op_id)
    result = dict(outcome=outcome, old_exists=work.exists(),
                  new_exists=(root / 'profiles' / 'play').is_dir(),
                  active_profile=profiles.get_active_profile(), events=events,
                  ops=registry(root)['ops'])
    evidence('rename-user-state', result)
    assert result['new_exists'] and not result['old_exists'] and not result['ops'], result
    assert result['active_profile'] == 'play', result
    assert ['remove-wrapper', 'work'] in events and ['create-wrapper', 'play'] in events, result
    assert ['identity', 'work', 'play'] in events, result


def test_old_process_cannot_select_another_account_after_rename(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    root_pair = endpoint.issue('root-account')
    work_pair = endpoint.issue('work-account')
    work = make_profile(root, 'work')
    write_store(root, singleton=root_pair, pool=[pool_row('root-row', root_pair)])
    write_store(work, singleton=work_pair)
    _dispatch()
    monkeypatch.setenv('HERMES_HOME', str(work))
    assert auth.resolve_codex_runtime_credentials()['api_key'] == work_pair[0]
    monkeypatch.setenv('HERMES_HOME', str(root))
    profiles.rename_profile('work', 'play')
    # The original backend still has the old HERMES_HOME, while the root login
    # belongs to a different account. It may fail or follow the move, never mix.
    monkeypatch.setenv('HERMES_HOME', str(work))
    try:
        result = auth.resolve_codex_runtime_credentials()
        outcome = dict(kind='selected', root_account=result['api_key'] == root_pair[0],
                       work_account=result['api_key'] == work_pair[0], source=result.get('source'))
    except AuthError as exc:
        outcome = dict(kind='refused', code=exc.code, message=str(exc))
    evidence('renamed-process-account', outcome)
    assert outcome.get('root_account') is not True, outcome
    assert not work.exists(), 'the stale process recreated the old profile'


@pytest.mark.windows_only
def test_deleting_a_junction_alias_keeps_the_target_login(codex_home, quiet_profiles):
    root, endpoint = codex_home
    work, _pair = _work_with_login(root, endpoint)
    before = registry(root)
    alias = root / 'profiles' / 'alias'
    result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(work)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert alias.resolve() == work.resolve()
    # This is the actual delete API. Some Python builds refuse to rmtree a
    # junction. Neither refusal nor removal may retire the *target* login.
    delete_error = None
    try:
        profiles.delete_profile('alias', yes=True)
    except RuntimeError as exc:
        delete_error = str(exc)
    after = registry(root)
    observed = dict(target_exists=work.is_dir(), alias_exists=alias.exists(),
                    before_states={cid: e['state'] for cid, e in before['chains'].items()},
                    after_states={cid: e['state'] for cid, e in after['chains'].items()},
                    target_has_codex=bool(read_json(work / 'auth.json').get('providers', {}).get('openai-codex')),
                    delete_error=delete_error)
    evidence('delete-junction-target', observed)
    assert observed['target_exists'], observed
    assert after['chains'] == before['chains'] and observed['target_has_codex'], observed


class Interrupted(BaseException):
    pass


def test_unreadable_compensation_never_reinstates_the_forward_decision(codex_home, quiet_profiles, monkeypatch):
    root, endpoint = codex_home
    work, _pair, cid, _before, op_id = _stuck_rename(root, endpoint, monkeypatch)
    with monkeypatch.context() as stop:
        stop.setattr(pops, 'apply_payload', lambda *a, **k: (_ for _ in ()).throw(Interrupted()))
        with pytest.raises(Interrupted):
            pops.recover_op(op_id, abandon=True)
    decided = registry(root)
    assert decided['ops'][op_id]['direction'] == 'abandon'
    epoch = decided['ops'][op_id]['abandon_epochs'][cid]
    comp = root / 'auth.ops' / (op_id + '.abandon.json')
    raw = comp.read_bytes()
    comp.write_bytes(b'{')  # temporary unreadability / corruption of PAYLOAD-2
    _dispatch()
    assert registry(root)['ops'][op_id]['status'] == 'unrecoverable'
    comp.write_bytes(raw)
    _dispatch()
    after = registry(root)
    chain = after['chains'][cid]
    result = dict(expected_owner=_key(work / 'auth.json'), expected_epoch=epoch,
                  owner=chain['owner'], epoch=chain['epoch'], state=chain['state'],
                  lifecycle=chain.get('lifecycle'), ops=after['ops'],
                  owner_dir_exists=Path(chain['owner']).parent.is_dir(),
                  record_epoch=read_json(work / 'auth.recovery' / (cid + '.json'))['epoch'])
    evidence('compensation-recovery', result)
    assert chain['owner'] == result['expected_owner'] and chain['epoch'] == epoch, result
    assert not after['ops'] and chain.get('lifecycle') is None, result


@pytest.mark.windows_only
def test_custom_owner_refresh_and_borrower_rename_have_no_lock_cycle(codex_home, quiet_profiles, tmp_path, monkeypatch):
    import threading
    import time
    from contextlib import contextmanager
    from hermes_constants import _get_platform_default_hermes_home
    from hermes_cli.auth_codex_lifecycle import holds_lifecycle_lock
    from hermes_cli.auth_codex_refresh_txn import refresh_row

    _root, endpoint = codex_home
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'native'))
    root = _get_platform_default_hermes_home()
    root.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(root))
    work = make_profile(root, 'work')
    pair = endpoint.issue()
    write_store(root, singleton=pair)
    write_store(work, singleton=pair)
    _dispatch()  # identical migrated rows: one root-owned chain with a work alias
    custom = root / 'aaa'
    custom.mkdir()
    (custom / 'config.yaml').write_text('model: {}\n')
    alias = root / 'profiles' / 'destination'
    made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(custom)],
                          capture_output=True, text=True, timeout=10)
    assert made.returncode == 0, made.stderr
    pops.transfer_chain(alias)  # legitimate --transfer-to destination; work's alias stays
    (cid, chain), = [(cid, e) for cid, e in registry(root)['chains'].items() if e['state'] == 'active']
    assert chain['owner'] == _key(custom / 'auth.json')
    assert any(r['store'] == _key(work / 'auth.json') for r in chain['rows'])

    owner_held = threading.Event()
    mover_lifecycle_held = threading.Event()
    owner_key, work_key = _key(custom / 'auth.json'), _key(work / 'auth.json')
    real_store_lock = auth._auth_store_lock
    real_lifecycle_locks = pops.lifecycle_locks
    events, results = [], {}

    @contextmanager
    def scheduled_store_lock(timeout_seconds=30, *, target_path=None):
        name = threading.current_thread().name
        key = _key(target_path)
        if name == 'review-refresh' and key == work_key:
            events.append('refresh-waiting-for-work-lifecycle')
        if name == 'review-move' and key == owner_key:
            events.append('mover-waiting-for-custom-store')
        with real_store_lock(min(timeout_seconds, 1.0), target_path=target_path):
            if name == 'review-refresh' and key == owner_key:
                events.append('refresh-holds-custom-store')
                owner_held.set()
                # A correct all-lifecycles-before-any-store plan already owns
                # work here and does not need this scheduler pause.
                if not holds_lifecycle_lock('work'):
                    assert mover_lifecycle_held.wait(5), events
            yield

    @contextmanager
    def scheduled_lifecycle_locks(names, timeout_seconds=30):
        with real_lifecycle_locks(names, min(timeout_seconds, 1.0)):
            if threading.current_thread().name == 'review-move':
                events.append('mover-holds-work-lifecycle')
                mover_lifecycle_held.set()
            yield

    monkeypatch.setattr(auth, '_auth_store_lock', scheduled_store_lock)
    monkeypatch.setattr(pops, 'lifecycle_locks', scheduled_lifecycle_locks)

    def run(name, fn):
        start = time.monotonic()
        try:
            fn()
            results[name] = {'result': 'ok'}
        except BaseException as exc:
            results[name] = {'error': type(exc).__name__, 'message': str(exc)}
        results[name]['elapsed'] = time.monotonic() - start

    refresher = threading.Thread(name='review-refresh', target=run, args=('refresh', lambda: refresh_row(
        work / 'auth.json', 'singleton', pair, force=True, needs_refresh=lambda _: True, timeout_seconds=1)))
    mover = threading.Thread(name='review-move', target=run,
                             args=('rename', lambda: profiles.rename_profile('work', 'play')))
    refresher.start()
    assert owner_held.wait(5), results
    mover.start()
    refresher.join(12)
    mover.join(12)
    observed = {'results': results, 'events': events, 'alive': [t.name for t in (refresher, mover) if t.is_alive()]}
    evidence('custom-owner-lock-cycle', observed)
    assert not observed['alive'], observed
    assert all(r.get('result') == 'ok' for r in results.values()), observed
