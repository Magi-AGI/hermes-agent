"""Whole-tree source scans read this checkout's files, and only this checkout's.

Nested worktrees under ``.claude/worktrees/`` (and any other checkout below the repo root) hold
another commit's code; the scans walked into them and reported violations this tree does not have
(deploy-gate A8). A directory the repo itself tracks stays in scope even if it carries a ``.git``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests import test_managed_runtime_resolution
from tests.checkout_scan import is_other_checkout
from tests.hermes_cli import test_config_read_guard, test_model_alias_credentials

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

OWN = "hermes_cli/own.py"
PARENT_OWNED = "vendor/lib/owned.py"   # tracked here, but vendor/lib is a repository of its own
PARENT_OWNED_DEEP = "vendor/lib/sub/deep.py"  # tracked here, below another .git inside vendor/lib
WORKTREE = ".claude/worktrees/wt/hermes_cli/theirs.py"
CLONE = "clones/other/hermes_cli/theirs.py"
OURS = {OWN, PARENT_OWNED, PARENT_OWNED_DEEP}


def _script(name: str, monkeypatch):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # dataclasses look the module up while loading
    spec.loader.exec_module(module)
    return module


def _checkout(root: Path) -> Path:
    for rel in (*OURS, WORKTREE, CLONE):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "--", *OURS], check=True)
    # A real repository whose own (empty) index must not decide what the scanned repo owns.
    subprocess.run(["git", "init", "-q", str(root / "vendor" / "lib")], check=True)
    (root / "vendor" / "lib" / "sub" / ".git").write_text("gitdir: elsewhere/.git\n", encoding="utf-8")
    (root / ".claude" / "worktrees" / "wt" / ".git").write_text("gitdir: elsewhere/.git/worktrees/wt\n",
                                                                 encoding="utf-8")
    (root / "clones" / "other" / ".git").mkdir()
    return root


def _config_read_guard(root, monkeypatch):
    monkeypatch.setattr(test_config_read_guard, "REPO_ROOT", root)
    return [path for _rel, path in test_config_read_guard._iter_source_files()]


def _managed_runtime_guard(root, monkeypatch):
    monkeypatch.setattr(test_managed_runtime_resolution, "REPO_ROOT", root)
    return test_managed_runtime_resolution._source_files()


def _alias_cache_guard(root, monkeypatch):
    # The guard derives the repo from its own file: <repo>/tests/hermes_cli/<file>.
    monkeypatch.setattr(test_model_alias_credentials, "__file__", str(root / "tests" / "hermes_cli" / "t.py"))
    cls = test_model_alias_credentials.TestNoProductionCodeMutatesTheAliasCacheInPlace
    return [path for path, _rel in cls._production_sources()]


def _compat_pointers(root, monkeypatch):
    module = _script("check_compat_pointers.py", monkeypatch)
    monkeypatch.setattr(module, "ROOT", root)
    return list(module._py_files())


def _tmp_literals(root, monkeypatch):
    return list(_script("check_no_tmp_literals.py", monkeypatch).iter_files(root))


def _windows_footguns(root, monkeypatch):
    module = _script("check-windows-footguns.py", monkeypatch)
    monkeypatch.setattr(module, "REPO_ROOT", root)
    return list(module.iter_files([root]))


@pytest.mark.parametrize("scan", [_config_read_guard, _managed_runtime_guard, _alias_cache_guard,
                                  _compat_pointers, _tmp_literals, _windows_footguns])
def test_whole_tree_scans_read_this_checkout_only(tmp_path, monkeypatch, scan):
    root = _checkout(tmp_path.resolve())
    scanned = {Path(p).resolve().relative_to(root).as_posix() for p in scan(root, monkeypatch)}
    assert scanned & {*OURS, WORKTREE, CLONE} == OURS


@pytest.mark.parametrize("failure", ["git-missing", "git-errors"])
def test_undeterminable_ownership_keeps_the_directory(tmp_path, monkeypatch, failure):
    """Dropping a directory hides its violations; reading a foreign one only adds noise."""
    root = _checkout(tmp_path.resolve())
    worktree = root / ".claude" / "worktrees" / "wt"
    assert is_other_checkout(worktree, root)  # what a working lookup says
    if failure == "git-missing":
        monkeypatch.setenv("PATH", "")
    else:
        (root / ".git").rename(root / "was-dot-git")  # the root is no repository any more: git exits 128
    assert not is_other_checkout(worktree, root)
