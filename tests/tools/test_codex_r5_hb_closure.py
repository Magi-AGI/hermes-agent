"""Adopted from the Codex closure review of 7152d57c83 (review-artifacts/mcp-hbhc/codex-closure-r4/hb). Adversarial H-B probes."""

from __future__ import annotations

import asyncio
import codecs
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@contextmanager
def profile_at(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    token = set_hermes_home_override(path)
    try:
        yield path / "config.yaml"
    finally:
        reset_hermes_home_override(token)


def test_explicit_null_leaf_must_fail_closed(tmp_path):
    """A present but non-boolean policy value must not mean the default true."""
    from tools.mcp_oauth import _config_allows_unattended_oauth

    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text("mcp:\n  interactive_oauth: null\n", encoding="utf-8")
        assert _config_allows_unattended_oauth() is False


def test_null_leaf_must_not_open_browser(tmp_path, monkeypatch):
    import tools.mcp_oauth as oauth

    opened = []
    monkeypatch.setattr(oauth, "_stdin_is_console", lambda: True)
    monkeypatch.setattr(oauth, "_can_open_browser", lambda: True)
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: opened.append(url) or True)
    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text("mcp:\n  interactive_oauth:\n", encoding="utf-8")
        with pytest.raises(oauth.OAuthNonInteractiveError):
            asyncio.run(oauth._make_redirect_handler(49123)("https://idp.example.invalid/authorize"))
        assert opened == []


def test_null_leaf_must_not_bind_callback(tmp_path, monkeypatch):
    import tools.mcp_oauth as oauth

    monkeypatch.setattr(oauth, "_stdin_is_console", lambda: True)
    monkeypatch.setattr(oauth, "_start_callback_server",
                        lambda *_: (_ for _ in ()).throw(AssertionError("listener bind reached")))
    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text("mcp:\n  interactive_oauth: null\n", encoding="utf-8")
        with pytest.raises(oauth.OAuthNonInteractiveError):
            asyncio.run(oauth._make_callback_waiter(49123, timeout=0.1)())


def test_null_leaf_must_refuse_model_authorize(tmp_path):
    from tools.connectors.mcp import model_oauth_refusal

    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text("mcp:\n  interactive_oauth:\n", encoding="utf-8")
        assert model_oauth_refusal(["srv"]) is not None


def test_managed_explicit_null_leaf_must_fail_closed(tmp_path, monkeypatch):
    from hermes_cli import managed_scope
    from tools.mcp_oauth import _config_allows_unattended_oauth

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("mcp:\n  interactive_oauth: null\n", encoding="utf-8")
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text("mcp:\n  interactive_oauth: false\n", encoding="utf-8")
        assert _config_allows_unattended_oauth() is False


def test_managed_false_precedes_user_true(tmp_path, monkeypatch):
    from hermes_cli import managed_scope
    from tools.mcp_oauth import _config_allows_unattended_oauth

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("mcp:\n  interactive_oauth: false\n", encoding="utf-8")
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text("mcp:\n  interactive_oauth: true\n", encoding="utf-8")
        assert _config_allows_unattended_oauth() is False


def test_utf8_bom_false_is_honored(tmp_path):
    from tools.mcp_oauth import _config_allows_unattended_oauth

    with profile_at(tmp_path / "p") as cfg:
        cfg.write_bytes(codecs.BOM_UTF8 + b"mcp:\n  interactive_oauth: false\n")
        assert _config_allows_unattended_oauth() is False


def test_large_valid_config_false_is_honored(tmp_path):
    from tools.mcp_oauth import _config_allows_unattended_oauth

    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text(("# config comment\n" * 600_000) +
                       "mcp:\n  interactive_oauth: false\n", encoding="utf-8")
        assert cfg.stat().st_size > 10_000_000
        assert _config_allows_unattended_oauth() is False


def test_error_after_open_during_read_fails_closed(tmp_path, monkeypatch):
    import builtins
    from hermes_cli import config as config_mod
    from tools.mcp_oauth import _config_allows_unattended_oauth

    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text("mcp:\n  interactive_oauth: false\n", encoding="utf-8")
        real_open = builtins.open
        seen = []

        class BrokenRead:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                seen.append("read")
                raise OSError("read interrupted after open")

        def patched_open(file, *args, **kwargs):
            return BrokenRead() if Path(file) == cfg else real_open(file, *args, **kwargs)

        monkeypatch.setattr(config_mod, "open", patched_open, raising=False)
        assert _config_allows_unattended_oauth() is False
        assert seen == ["read"]


def test_profile_override_reaches_real_mcp_loop(tmp_path):
    from tools import mcp_tool_loop
    from tools.mcp_oauth import _config_allows_unattended_oauth

    async def decide():
        return _config_allows_unattended_oauth()

    p_false, p_true = tmp_path / "false", tmp_path / "true"
    for path, text in ((p_false, "mcp:\n  interactive_oauth: false\n"),
                       (p_true, "mcp:\n  interactive_oauth: true\n")):
        path.mkdir()
        (path / "config.yaml").write_text(text, encoding="utf-8")
    mcp_tool_loop._ensure_mcp_loop()
    try:
        with profile_at(p_false):
            assert mcp_tool_loop._run_on_mcp_loop(decide, timeout=10) is False
        with profile_at(p_true):
            assert mcp_tool_loop._run_on_mcp_loop(decide, timeout=10) is True
    finally:
        mcp_tool_loop._stop_mcp_loop()


def test_managed_true_does_not_mask_a_broken_user_file(tmp_path, monkeypatch):
    """Claimed fail policy: any present, unreadable user config fails closed."""
    from hermes_cli import managed_scope
    from tools.mcp_oauth import _config_allows_unattended_oauth

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("mcp:\n  interactive_oauth: true\n", encoding="utf-8")
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    with profile_at(tmp_path / "p") as cfg:
        cfg.write_text("mcp: [broken\n", encoding="utf-8")
        assert _config_allows_unattended_oauth() is False
