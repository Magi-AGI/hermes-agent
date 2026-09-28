"""Stage 2 review round 3 (Codex, reviewed head 7f0d03dd6c): required finalization must distinguish
unreadable from absent.

Exercises real rename/registry code and real profile helper implementations. Only
temporary homes and fake credentials; fixture stubs prevent live process/service work.
Run via scripts/run_tests_isolated.py, with -p tests.conftest for this external file.
"""
import json
from pathlib import Path

import pytest

from hermes_cli import profiles
from hermes_cli.auth_codex_profile_ops import recover_op
from tests.hermes_cli.codex_authority_env import codex_home, registry  # noqa: F401
from tests.hermes_cli.test_codex_profile_lifecycle import quiet_profiles, _work_with_login  # noqa: F401

REAL_CREATE_WRAPPER = profiles.create_wrapper_script
REAL_REMOVE_WRAPPER = profiles.remove_wrapper_script


@pytest.mark.parametrize("field", ["active_profile", "old_wrapper"])
@pytest.mark.parametrize("denied", [True, False], ids=["read-denied", "control"])
def test_unreadable_required_state_does_not_commit(codex_home, quiet_profiles, monkeypatch, field, denied):
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    profiles.set_active_profile("work")
    monkeypatch.setattr(profiles, "create_wrapper_script", REAL_CREATE_WRAPPER)
    monkeypatch.setattr(profiles, "remove_wrapper_script", REAL_REMOVE_WRAPPER)
    monkeypatch.setattr(profiles, "check_alias_collision", lambda _: None)
    old_wrapper = REAL_CREATE_WRAPPER("work")
    assert old_wrapper is not None
    active_path = profiles._get_active_profile_path()
    blocked = active_path if field == "active_profile" else old_wrapper
    read_text = Path.read_text
    attempted = []

    def read_with_fault(path, *args, **kwargs):
        if path == blocked:
            attempted.append(str(path))
            if denied:
                raise PermissionError(13, "review: sharing/access failure reading required state")
        return read_text(path, *args, **kwargs)

    failure = None
    with monkeypatch.context() as fault:
        fault.setattr(Path, "read_text", read_with_fault)
        try:
            profiles.rename_profile("work", "play")
        except ValueError as exc:
            failure = str(exc)

    observed = {
        "field": field, "denied": denied, "attempts": len(attempted),
        "error": failure, "ops": registry(root)["ops"],
        "active_profile": read_text(active_path).strip(),
        "old_wrapper_exists": old_wrapper.exists(),
        "old_wrapper_text": read_text(old_wrapper) if old_wrapper.exists() else None,
        "new_wrapper_exists": profiles._wrapper_path("play").exists(),
        "old_profile_exists": (root / "profiles" / "work").exists(),
        "new_profile_exists": (root / "profiles" / "play").exists(),
    }
    assert attempted, observed
    assert observed["new_profile_exists"] and not observed["old_profile_exists"], observed
    if denied:
        assert observed["ops"], "COMMIT lost recovery despite unreadable required state: " + str(observed)
        assert failure is not None, observed
        op_id, op = next(iter(observed["ops"].items()))
        assert op["phase"] == "finalizing" and op["status"] == "lifecycle-stuck", observed
        assert recover_op(op_id) == "rolled_forward"
        assert not registry(root)["ops"]
    else:
        assert failure is None and not observed["ops"], observed
    assert profiles.get_active_profile() == "play"
    assert not old_wrapper.exists()


@pytest.mark.parametrize("denied", [True, False], ids=["honcho-read-denied", "honcho-control"])
def test_advisory_honcho_failure_is_reported(codex_home, quiet_profiles, monkeypatch, capsys, denied):
    root, endpoint = codex_home
    work, _ = _work_with_login(root, endpoint)
    source = work / "honcho.json"
    target = root / "profiles" / "play" / "honcho.json"
    source.write_text(json.dumps({"hosts": {"hermes_work": {"aiPeer": "work"}}}), encoding="utf-8")
    monkeypatch.setattr(profiles, "check_alias_collision", lambda _: None)
    read_text = Path.read_text
    attempts = []

    def read_with_fault(path, *args, **kwargs):
        if path == target:
            attempts.append(str(path))
            if denied:
                raise PermissionError(13, "review: Honcho config unreadable")
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "read_text", read_with_fault)
        profiles.rename_profile("work", "play")
    output = capsys.readouterr().out
    hosts = json.loads(read_text(target))["hosts"]
    observed = {"denied": denied, "attempts": len(attempts), "hosts": hosts,
                "ops": registry(root)["ops"], "output": output}
    assert attempts and not observed["ops"], observed  # advisory: must not prevent commit
    if denied:
        assert "hermes_work" in hosts, observed
        assert "honcho" in output.lower(), "Advisory failure was silent: " + str(observed)
    else:
        assert "hermes_play" in hosts and "hermes_work" not in hosts, observed
