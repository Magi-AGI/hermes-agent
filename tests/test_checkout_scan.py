"""Whole-tree source guards scan this checkout only.

Nested worktrees under ``.claude/worktrees/`` (and any other checkout below the repo root) hold
another commit's code; the guards walked into them and reported violations this tree does not
have (deploy-gate A8).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import test_managed_runtime_resolution
from tests.hermes_cli import test_config_read_guard, test_model_alias_credentials


def _checkout_with_nested_checkouts(root: Path) -> Path:
    (root / ".git").mkdir()
    own = root / "hermes_cli" / "own.py"
    own.parent.mkdir()
    own.write_text("")
    worktree = root / ".claude" / "worktrees" / "wt"  # a git worktree: .git is a file
    clone = root / "vendor" / "clone"                 # a clone or submodule: .git is a directory
    for other in (worktree, clone):
        (other / "hermes_cli").mkdir(parents=True)
        (other / "hermes_cli" / "theirs.py").write_text("")
    (worktree / ".git").write_text("gitdir: elsewhere/.git/worktrees/wt\n")
    (clone / ".git").mkdir()
    return own


def _config_read_guard(root, monkeypatch):
    monkeypatch.setattr(test_config_read_guard, "REPO_ROOT", root)
    return [path for _rel, path in test_config_read_guard._iter_source_files()]


def _managed_runtime_guard(root, monkeypatch):
    monkeypatch.setattr(test_managed_runtime_resolution, "REPO_ROOT", root)
    return test_managed_runtime_resolution._source_files()


def _alias_cache_guard(root, _monkeypatch):
    cls = test_model_alias_credentials.TestNoProductionCodeMutatesTheAliasCacheInPlace
    return [path for path, _rel in cls._production_sources(root)]


@pytest.mark.parametrize("scan", [_config_read_guard, _managed_runtime_guard, _alias_cache_guard])
def test_whole_tree_guards_skip_other_checkouts(tmp_path, monkeypatch, scan):
    own = _checkout_with_nested_checkouts(tmp_path)
    assert sorted(scan(tmp_path, monkeypatch)) == [own]
