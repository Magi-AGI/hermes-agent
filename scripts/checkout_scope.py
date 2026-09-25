"""Scope for whole-tree source scans: this checkout's files, not another checkout's.

A directory holding its own ``.git`` (a file for a git worktree, a directory for a clone) is another
checkout, e.g. ``.worktrees/x`` or ``.claude/worktrees/x``. Its files are another commit's code, so
a scan that walks into it reports violations this tree does not have. A directory whose files the
enclosing repository tracks is this checkout's source even if it carries a ``.git``, and stays in
scope; a submodule (a gitlink entry) is a separate checkout.

Not for boundary checks that must see everything on disk (e.g. what enters a build context).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_GITLINK_MODE = b"160000"


def is_other_checkout(directory: Path) -> bool:
    directory = Path(directory)
    return os.path.lexists(directory / ".git") and not _tracked_by_enclosing_repo(directory)


def _tracked_by_enclosing_repo(directory: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(directory.parent), "ls-files", "--stage", "-z", "--", directory.name],
            capture_output=True, timeout=60, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(entry.split(b" ", 1)[0] != _GITLINK_MODE for entry in out.split(b"\0") if entry)
