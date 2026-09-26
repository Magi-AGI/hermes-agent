"""A restore protects the SAME authority root the runtime later uses (stage 1).

The restore guard resolved an explicit home's authority with different rules from the runtime
(``get_default_hermes_root``) for a home directly beneath the platform-default root, or a profile of a
custom root that has no marker files yet. Both now use one resolver
(``hermes_constants.hermes_root_for_home``), so the restore's migration lock and store checks are the
ones the runtime honours. With the stage-1 rule (a restore never brings Codex material back) a
restored snapshot's old Codex login never reaches the endpoint from a normal startup in either
layout. The runtime runs in a separate process; fake credentials only.
"""

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, write_store  # noqa: F401
from tests.hermes_cli.codex_restore_children import runtime_startup


@pytest.mark.parametrize("layout", ["beneath-default-root", "custom-root-profile"])
def test_a_restored_snapshot_brings_no_codex_login_where_the_runtime_looks(codex_home, tmp_path, monkeypatch,
                                                                           layout):
    from hermes_cli import backup
    from hermes_cli.auth_codex_registry import authority_root_for, root_home
    from hermes_constants import _get_platform_default_hermes_home

    _root, endpoint = codex_home
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))  # the native root, sandboxed
    if layout == "beneath-default-root":
        home = _get_platform_default_hermes_home() / "work"
    else:
        home = tmp_path / "custom-root" / "profiles" / "work"  # the custom root has no marker files yet
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert authority_root_for(home) == root_home(), "restore and runtime must agree on the authority"
    monkeypatch.setenv("HERMES_HOME", str(_root))
    old = endpoint.issue()
    write_store(home, singleton=old)
    snapshot_id = backup.create_quick_snapshot(label="w", hermes_home=home)
    write_store(home)  # the Codex login was removed since the snapshot
    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=home) is True
    started = runtime_startup(tmp_path, tmp_path / "userhome", home)
    assert started["presented"] == [], f"a restored token reached the endpoint: {started}"
