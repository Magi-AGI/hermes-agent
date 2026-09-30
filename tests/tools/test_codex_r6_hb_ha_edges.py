"""Adopted from the Codex round-5 closure review of 1a17623a12 (codex-closure-r5/hb_ha): H-B/H-A edge probes."""

from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


def test_managed_true_does_not_mask_user_file_lost_between_stat_and_open(tmp_path, monkeypatch):
    from hermes_cli import config as config_mod, managed_scope
    from tools.mcp_oauth import _config_allows_unattended_oauth

    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("mcp:\n  interactive_oauth: true\n", encoding="utf-8")
    monkeypatch.setattr(managed_scope, "get_managed_dir", lambda: managed)
    with profile_at(tmp_path / "user") as user_config:
        user_config.write_text("mcp:\n  interactive_oauth: false\n", encoding="utf-8")
        actual_open = builtins.open
        opened_user = []

        def lost_at_open(file, *args, **kwargs):
            if Path(file) == user_config:
                opened_user.append(True)
                raise FileNotFoundError("removed after stat")
            return actual_open(file, *args, **kwargs)

        monkeypatch.setattr(config_mod, "open", lost_at_open, raising=False)
        assert _config_allows_unattended_oauth() is False
        assert opened_user == [True]


def test_present_nonmapping_mcp_section_does_not_look_like_present_leaf(tmp_path):
    """Classify section type errors separately from a present invalid policy leaf."""
    from hermes_cli.config import CONFIG_KEY_ABSENT, read_config_value_fresh
    from tools.mcp_oauth import _config_allows_unattended_oauth

    with profile_at(tmp_path / "user") as config:
        config.write_text("mcp: []\n", encoding="utf-8")
        assert read_config_value_fresh("mcp", "interactive_oauth") is CONFIG_KEY_ABSENT
        assert _config_allows_unattended_oauth() is True


def test_explicit_login_remains_forced_with_present_null(tmp_path, monkeypatch):
    from tools import mcp_oauth as oauth

    monkeypatch.setattr(oauth, "_stdin_is_console", lambda: False)
    with profile_at(tmp_path / "user") as config:
        config.write_text("mcp:\n  interactive_oauth: null\n", encoding="utf-8")
        assert oauth._is_interactive() is False
        with oauth.force_interactive_oauth():
            assert oauth._is_interactive() is True
            with oauth.suppress_interactive_oauth():
                assert oauth._is_interactive() is False


def test_list_changed_replaces_old_cache_hint_with_new_first_page_hint(tmp_path):
    """A live list_changed refresh should not reuse a connect-time cache TTL."""
    import mcp.types as types
    from tools import mcp_schema_cache as cache
    from tools import mcp_tool_registration as registration
    from tools.mcp_tool import MCPServerTask
    from tools.registry import ToolRegistry

    with profile_at(tmp_path / "user"):
        name = "r5_cache_hint_refresh"
        cfg = {"url": "https://mcp.example.invalid/mcp", "auth": "oauth", "lazy": True}
        tool = types.Tool.model_validate({
            "name": "search", "description": "Search", "inputSchema": {"type": "object"},
        })
        server = MCPServerTask(name)
        server._config = cfg
        server._tools = [tool]
        server._list_cache_meta = {}  # first live list had no TTL, so cache never expires
        registry = ToolRegistry()
        first = types.ListToolsResult.model_validate({
            "tools": [], "nextCursor": "page-two", "ttlMs": 0,
        })
        second = types.ListToolsResult.model_validate({
            "tools": [{"name": "search", "description": "Search",
                       "inputSchema": {"type": "object"}}],
            "ttlMs": 60_000,
        })
        pages = iter((first, second))

        async def list_tools(**_kwargs):
            return next(pages)

        server.session = SimpleNamespace(list_tools=list_tools)
        with patch("tools.registry.registry", registry), patch.object(
            registration, "_select_utility_schemas", return_value=[]
        ):
            server._registered_tool_names = registration._register_server_tools(name, server, cfg)
            assert server._registered_tool_names
            fingerprint = cache.config_fingerprint(cfg)
            assert cache.get_cached_entry(name, fingerprint) is not None
            asyncio.run(server._refresh_tools())
            written = cache._load_all()[name]
            assert written.get("ttl_ms") == 0, written
            assert cache.get_cached_entry(name, fingerprint) is None
