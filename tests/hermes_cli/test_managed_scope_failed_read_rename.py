"""CA-P2-1: a malformed managed config must not become a clean absence while its directory is
renamed (REPORT-ROUND5, kept in scope by Lake 2026-09-30).

The vanish grace existed but was keyed on the PARSE cache, and a malformed file is never cached,
so the admin's broken-but-present policy read as "no managed scope" for the rename window -- the
one window in which an env provider can pass. Red on 60448cd8e7 + the three MCP P2 commits.
"""

from __future__ import annotations

def test_malformed_managed_config_is_not_clean_absence_during_directory_rename(tmp_path, monkeypatch):
    """A managed file whose read FAILED is a known file: a vanished directory must stay a failed read.

    The grace window (``VANISHED_GRACE_SECONDS``) is keyed on the parse cache, and a malformed file
    is never cached, so the admin's broken-but-present policy reads as "no managed scope at all"
    for the rename window -- which is exactly the window in which an env provider can pass.
    """
    from hermes_cli import managed_scope

    managed = tmp_path / "etc-hermes"
    managed.mkdir()
    (managed / "config.yaml").write_text("model: [malformed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        broken = managed_scope.read_managed_config()
        assert broken.error, "the malformed managed config was not observed as a failed read"

        # The admin's atomic replace: the directory is renamed out of the way for an instant.
        managed.rename(tmp_path / "etc-hermes.replacing")
        during = managed_scope.read_managed_config()
        assert during.error, (
            "a managed config that FAILED to read became a clean absence while its directory was "
            "renamed: for up to one selection an env provider can pass")
    finally:
        managed_scope.invalidate_managed_cache()


def test_managed_env_read_failure_also_survives_a_directory_rename(tmp_path, monkeypatch):
    """Same rule for the managed ``.env``: a failed read is not an absence."""
    from hermes_cli import managed_scope

    managed = tmp_path / "etc-hermes"
    managed.mkdir()
    # A .env whose bytes are not valid UTF-8: _parse_managed_env decodes strictly, so this is a
    # parse failure rather than an absence.
    (managed / ".env").write_bytes(b"KEY=\xff\xfe\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    try:
        broken = managed_scope._read_managed_file(".env", managed_scope._ENV_CACHE,
                                                  managed_scope._parse_managed_env)
        assert broken.error, "the malformed managed .env was not observed as a failed read"
        managed.rename(tmp_path / "etc-hermes.replacing")
        during = managed_scope._read_managed_file(".env", managed_scope._ENV_CACHE,
                                                  managed_scope._parse_managed_env)
        assert during.error, "a failed managed .env read became a clean absence during a rename"
    finally:
        managed_scope.invalidate_managed_cache()
