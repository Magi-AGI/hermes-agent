"""Scope for whole-tree source guards: this checkout only.

A directory holding its own ``.git`` (a file for a git worktree, a directory for a clone or a
submodule) is another checkout: ``.worktrees/``, ``.claude/worktrees/``, a scratch clone. Its
files belong to another commit, so a guard that walks into it reports violations this tree does
not have. Guards that walk the repo prune such directories with ``is_other_checkout``.
"""

from __future__ import annotations

import os
from pathlib import Path


def is_other_checkout(directory: Path) -> bool:
    return os.path.lexists(directory / ".git")
