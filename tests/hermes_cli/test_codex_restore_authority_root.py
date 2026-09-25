"""A restore protects the SAME authority root the runtime later uses (stage 1).

The restore guard resolved an explicit home's authority with different rules from the runtime
(``get_default_hermes_root``): for a home directly beneath the platform-default root, or a profile
of a custom root that has no marker files yet, the restore wrote its barrier and fenced registry in
the wrong directory; a normal startup then found no registry at its own root and bootstrapped the
restored tokens ACTIVE. Both now use one resolver (``hermes_constants.hermes_root_for_home``).
The runtime runs in a separate process; fake credentials only.
"""

import pytest

from tests.hermes_cli.codex_authority_env import codex_home, write_store  # noqa: F401
from tests.hermes_cli.codex_restore_children import runtime_startup


@pytest.mark.parametrize("layout", ["beneath-default-root", "custom-root-profile"])
def test_a_restored_login_is_fenced_where_the_runtime_looks(codex_home, tmp_path, monkeypatch, layout):
    from hermes_cli import backup
    from hermes_constants import _get_platform_default_hermes_home

    _root, endpoint = codex_home
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))  # the native root, sandboxed
    if layout == "beneath-default-root":
        home = _get_platform_default_hermes_home() / "work"
    else:
        home = tmp_path / "custom-root" / "profiles" / "work"  # the custom root has no marker files yet
    home.mkdir(parents=True)
    write_store(home, singleton=endpoint.issue())
    snapshot_id = backup.create_quick_snapshot(label="w", hermes_home=home)
    assert backup.restore_quick_snapshot(snapshot_id, hermes_home=home) is True
    started = runtime_startup(tmp_path, tmp_path / "userhome", home)
    assert started["presented"] == [], f"a restored token reached the endpoint: {started}"
