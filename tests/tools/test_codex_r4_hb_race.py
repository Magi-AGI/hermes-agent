"""Adopted from Codex preliminary review of 05be569a75 (review-artifacts/mcp-hbhc/codex-prelim-hb-race.py): event-ordered probes for the interrupted MCP OAuth config read."""

from __future__ import annotations

import builtins
import os

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_false_config_reappears_after_interrupted_load_before_failure_check(tmp_path, monkeypatch):
    import hermes_cli.config as config_mod
    from tools.mcp_oauth import _config_allows_unattended_oauth

    profile = tmp_path / "profile"
    profile.mkdir()
    token = set_hermes_home_override(profile)
    try:
        path = config_mod.get_config_path()
        restricted = "mcp:\n  interactive_oauth: false\n"
        path.write_text(restricted, encoding="utf-8")

        actual_open = builtins.open
        interrupted = False

        def open_during_replacement(file, *args, **kwargs):
            nonlocal interrupted
            if file == path and not interrupted:
                interrupted = True
                path.unlink()
                raise FileNotFoundError(2, "interrupted replacement", str(path))
            return actual_open(file, *args, **kwargs)

        monkeypatch.setattr(config_mod, "open", open_during_replacement, raising=False)
        actual_failure_check = config_mod.get_active_config_parse_failure

        def replacement_finishes_before_failure_check():
            assert interrupted, "the load must have reached and failed at open"
            assert not path.exists(), "the failed read must have recorded an unstattable sentinel"
            path.write_text(restricted, encoding="utf-8")
            return actual_failure_check()

        monkeypatch.setattr(
            config_mod, "get_active_config_parse_failure", replacement_finishes_before_failure_check
        )

        # The current implementation returns its fallback from the failed read, then
        # checks the *new* file's stat. The fallback must never authorize OAuth.
        assert _config_allows_unattended_oauth() is False
    finally:
        reset_hermes_home_override(token)


def test_same_signature_policy_flip_does_not_keep_cached_true(tmp_path):
    import hermes_cli.config as config_mod
    from hermes_cli.config import file_signature
    from tools.mcp_oauth import _config_allows_unattended_oauth

    profile = tmp_path / "profile"
    profile.mkdir()
    token = set_hermes_home_override(profile)
    try:
        path = config_mod.get_config_path()
        allowed = b"mcp:\n  interactive_oauth: true \n"
        restricted = b"mcp:\n  interactive_oauth: false\n"
        assert len(allowed) == len(restricted)
        path.write_bytes(allowed)
        assert _config_allows_unattended_oauth() is True
        original = path.stat()
        original_sig = file_signature(original)

        # A sync tool can rewrite in place while preserving the timestamp. On Windows
        # st_ctime is the creation time; the inode and size also remain the same.
        with path.open("r+b") as file:
            file.write(restricted)
            file.flush()
            os.fsync(file.fileno())
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert path.read_bytes() == restricted
        assert file_signature(path.stat()) == original_sig, "precondition: metadata-based cache hit"
        assert _config_allows_unattended_oauth() is False
    finally:
        reset_hermes_home_override(token)
