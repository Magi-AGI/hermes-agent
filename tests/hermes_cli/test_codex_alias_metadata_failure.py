"""Stage 2 review round 5 (Codex, reviewed head 420a1ca4d9): missing must not include an unreadable
metadata probe.

Real temporary files and rename/registry code; fake credentials, no live services.
Run via scripts/run_tests_isolated.py with -p tests.conftest.
"""
import errno
import os
from pathlib import Path

import pytest

from hermes_cli import profiles
from hermes_cli.auth_codex_profile_ops import recover_op
from tests.hermes_cli.codex_authority_env import codex_home, registry  # noqa: F401
from tests.hermes_cli.test_codex_profile_lifecycle import quiet_profiles, _work_with_login  # noqa: F401

REAL_CREATE_WRAPPER = profiles.create_wrapper_script
REAL_REMOVE_WRAPPER = profiles.remove_wrapper_script


@pytest.mark.parametrize("fault_kind", ["permission", "io", "none"])
def test_metadata_failure_preserves_new_alias_and_recovery(codex_home, quiet_profiles, monkeypatch, fault_kind):
    root, endpoint = codex_home
    _work_with_login(root, endpoint)
    monkeypatch.setattr(profiles, "create_wrapper_script", REAL_CREATE_WRAPPER)
    monkeypatch.setattr(profiles, "remove_wrapper_script", REAL_REMOVE_WRAPPER)
    monkeypatch.setattr(profiles, "check_alias_collision", lambda _: None)
    target = profiles._wrapper_path("play")
    target.parent.mkdir(parents=True, exist_ok=True)
    sentinel = b"@echo off\r\necho keep this unrelated command\r\n"
    target.write_bytes(sentinel)
    real_lstat = os.lstat
    real_read = Path.read_text
    probes, reads = [], []

    def lstat_with_fault(path, *args, **kwargs):
        if Path(path) == target:
            probes.append(str(path))
            if fault_kind == "permission":
                raise PermissionError(errno.EACCES, "review: metadata access denied", str(path))
            if fault_kind == "io":
                raise OSError(errno.EIO, "review: metadata read failed", str(path))
        return real_lstat(path, *args, **kwargs)

    def read_spy(path, *args, **kwargs):
        if path == target:
            reads.append(str(path))
        return real_read(path, *args, **kwargs)

    error = None
    with monkeypatch.context() as failure:
        failure.setattr(os, "lstat", lstat_with_fault)
        failure.setattr(Path, "read_text", read_spy)
        try:
            profiles.rename_profile("work", "play")
        except ValueError as exc:
            error = str(exc)
    ops = registry(root)["ops"]
    observed = {"fault": fault_kind, "metadata_probes": len(probes), "content_reads": len(reads),
                "error": error, "ops": ops, "preserved": target.read_bytes() == sentinel,
                "contents": target.read_text(encoding="utf-8")}
    assert probes, observed
    assert observed["preserved"], "A metadata error was treated as absence; unrelated alias overwritten: " + str(observed)
    if fault_kind != "none":
        assert error is not None and ops, observed
        op_id, op = next(iter(ops.items()))
        assert op["phase"] == "finalizing" and op["status"] == "lifecycle-stuck", observed
        assert recover_op(op_id) == "rolled_forward"
        assert not registry(root)["ops"] and target.read_bytes() == sentinel
    else:
        assert error is None and not ops, observed


@pytest.mark.parametrize("fail_swap", [False, True], ids=["replace-success", "replace-fails"])
def test_wrapper_publication_preserves_hard_link_and_failure_state(codex_home, monkeypatch, fail_swap):
    root, _endpoint = codex_home
    target = REAL_CREATE_WRAPPER("play", target="work")
    assert target is not None
    original = target.read_bytes()
    linked = target.parent / "another-name"
    os.link(target, linked)
    real_replace = os.replace

    def replace_with_fault(src, dst, *args, **kwargs):
        if fail_swap and Path(dst) == target:
            raise PermissionError(errno.EACCES, "review: final swap blocked", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace_with_fault)
    result = REAL_CREATE_WRAPPER("play", target="play")
    assert linked.read_bytes() == original
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))
    if fail_swap:
        assert result is None and target.read_bytes() == original
    else:
        assert result == target and b"hermes -p play" in target.read_bytes()
        assert not os.path.samefile(target, linked)
