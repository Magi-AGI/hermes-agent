"""The per-tool-call plugin gate does not touch the filesystem (deploy gate D3a).

Every tool result passes the post_tool_call gate (has_hook -> get_plugin_manager) on the tool
worker before the turn gets it. The gate resolved the Hermes home on every call; on 2026-09-14 a
worker sat parked in exactly that stat() while the turn waited on it.
"""

from __future__ import annotations

from pathlib import Path

from hermes_cli import plugins


def test_repeated_manager_lookups_do_not_resolve_the_home_again(monkeypatch):
    first = plugins.get_plugin_manager()
    resolved: list[Path] = []
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: resolved.append(self) or real_resolve(self, *a, **k))

    assert all(plugins.get_plugin_manager() is first for _ in range(3))
    assert resolved == []
