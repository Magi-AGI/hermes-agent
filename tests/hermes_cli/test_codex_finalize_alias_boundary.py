"""Stage 2 review round 4 (Codex, reviewed head 5ca2841a4d): real PATH lookup and finalization I/O.

Use the isolated runner with -p tests.conftest. All files are temporary and all
credentials fake; the shared quiet_profiles fixture disables live service work.
"""
import json
import os
from pathlib import Path

import pytest

from hermes_cli import profiles
from hermes_cli.auth_codex_profile_ops import recover_op
from tests.hermes_cli.codex_authority_env import codex_home, registry  # noqa: F401
from tests.hermes_cli.test_codex_profile_lifecycle import quiet_profiles, _work_with_login  # noqa: F401

REAL_CREATE_WRAPPER = profiles.create_wrapper_script
REAL_REMOVE_WRAPPER = profiles.remove_wrapper_script


@pytest.mark.windows_only  # exercises the Windows where.exe lookup
@pytest.mark.parametrize("on_path,contents", [
    (True, "unreadable"), (False, "unreadable"), (False, "absent"), (False, "ours"),
], ids=["on-path-unreadable", "off-path-unreadable", "missing-control", "owned-control"])
def test_new_alias_is_inspected_independently_of_path(codex_home, quiet_profiles, monkeypatch, on_path, contents):
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    monkeypatch.setattr(profiles, "create_wrapper_script", REAL_CREATE_WRAPPER)
    monkeypatch.setattr(profiles, "remove_wrapper_script", REAL_REMOVE_WRAPPER)
    alias_name = "review-target"
    target = profiles._wrapper_path(alias_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b"@echo off\r\necho unrelated existing command\r\n"
    if contents == "unreadable":
        target.write_bytes(sentinel)
    elif contents == "ours":
        assert REAL_CREATE_WRAPPER(alias_name, target="work") == target
    system32 = str(Path(os.environ["SystemRoot"]) / "System32")
    monkeypatch.setenv("PATH", str(target.parent) + os.pathsep + system32 if on_path else system32)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    # Real where.exe lookup. The existing off-PATH file is not a PATH collision.
    collision_before = profiles.check_alias_collision(alias_name)
    assert bool(collision_before) == on_path
    read_text = Path.read_text
    attempts = []

    def read_with_fault(path, *args, **kwargs):
        if path == target:
            attempts.append(str(path))
            if contents == "unreadable":
                raise PermissionError(13, "review: existing new-alias file cannot be read")
        return read_text(path, *args, **kwargs)

    failure = None
    with monkeypatch.context() as fault:
        fault.setattr(Path, "read_text", read_with_fault)
        try:
            profiles.rename_profile("work", alias_name)
        except ValueError as exc:
            failure = str(exc)
    after = target.read_bytes() if target.exists() else None
    observed = {"on_path": on_path, "contents": contents, "read_attempts": len(attempts),
                "collision_before": collision_before, "error": failure,
                "ops": registry(root)["ops"], "new_alias_bytes": after.decode("utf-8") if after else None,
                "sentinel_preserved": after == sentinel}
    if contents == "unreadable":
        assert after == sentinel, "Unreadable existing command was overwritten: " + str(observed)
        assert failure is not None and observed["ops"], observed
        op_id, op = next(iter(observed["ops"].items()))
        assert op["status"] == "lifecycle-stuck" and op["phase"] == "finalizing", observed
        assert recover_op(op_id) == "rolled_forward"
        assert not registry(root)["ops"]
        assert target.read_bytes() == sentinel
    else:
        assert failure is None and not observed["ops"], observed
        assert f"hermes -p {alias_name}" in read_text(target)


def test_honcho_write_failure_warns_without_blocking(codex_home, quiet_profiles, monkeypatch, capsys):
    import utils

    root, endpoint = codex_home
    work, _ = _work_with_login(root, endpoint)
    source = work / "honcho.json"
    target = root / "profiles" / "play" / "honcho.json"
    source.write_text(json.dumps({"hosts": {"hermes_work": {"aiPeer": "work"}}}), encoding="utf-8")
    monkeypatch.setattr(profiles, "check_alias_collision", lambda _: None)
    write_json = utils.atomic_json_write
    attempts = []

    def write_with_fault(path, *args, **kwargs):
        if Path(path) == target:
            attempts.append(str(path))
            raise PermissionError(13, "review: Honcho config write blocked")
        return write_json(path, *args, **kwargs)

    monkeypatch.setattr(utils, "atomic_json_write", write_with_fault)
    profiles.rename_profile("work", "play")
    output = capsys.readouterr().out
    observed = {"attempts": len(attempts), "ops": registry(root)["ops"], "output": output,
                "hosts": json.loads(target.read_text(encoding="utf-8"))["hosts"]}
    assert attempts and not observed["ops"], observed
    assert "hermes_work" in observed["hosts"], observed
    assert "Honcho" in output and "could not write" in output, observed
