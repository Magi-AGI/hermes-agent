"""Crash recovery of a restore fences exactly the stores the restore wrote, whoever recovers.

The restore barrier recorded only the action and a timestamp, and recovery discovered stores from
the RECOVERING process's own active home. A restore into ``<native-root>/work/auth.json`` that
died after extraction and was recovered by a process running from the root never saw ``work``:
the barrier was cleared without fencing it, and a later startup from ``work`` bootstrapped the
restored token ACTIVE. The barrier now records every destination store / protocol path BEFORE
extraction, and recovery fences exactly those (plus its own view) before clearing it. Real
processes; fake credentials only.
"""

from tests.hermes_cli.codex_authority_env import codex_home, write_store  # noqa: F401
from tests.hermes_cli.codex_restore_children import (
    RECOVERER, SNAPSHOT_DIER, run_in, runtime_startup)


def test_a_restore_that_died_is_fenced_even_when_another_home_recovers_it(codex_home, tmp_path, monkeypatch):
    from hermes_cli import backup
    from hermes_constants import _get_platform_default_hermes_home

    _root, endpoint = codex_home
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    native = _get_platform_default_hermes_home()
    work = native / "work"  # a nonstandard active store beneath the native root
    work.mkdir(parents=True)
    write_store(work, singleton=endpoint.issue())
    snapshot_id = backup.create_quick_snapshot(label="w", hermes_home=work)
    userhome = tmp_path / "userhome"

    code, out, err = run_in(SNAPSHOT_DIER, [snapshot_id, work], userhome, work)
    assert code == 137, err[-2000:]
    assert (native / "auth.restore-barrier.json").exists()
    # The ROOT recovers first: its own store enumeration does not include ``work``.
    code, out, err = run_in(RECOVERER, [tmp_path / "presented-root.log", 5.0, "none"], userhome, native)
    assert code == 0, err[-2000:]
    started = runtime_startup(tmp_path, userhome, work)  # then a normal startup from ``work``
    assert started["presented"] == [], f"a restored token reached the endpoint: {started}"
