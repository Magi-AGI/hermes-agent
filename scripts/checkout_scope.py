"""Scope for whole-tree source scans: this checkout's files, not another checkout's.

A directory holding its own ``.git`` (a file for a git worktree, a directory for a clone) is another
checkout, e.g. ``.worktrees/x`` or ``.claude/worktrees/x``. Its files are another commit's code, so
a scan that walks into it reports violations this tree does not have. A directory under which the
scanned repository (the scan ROOT's, never a nested one's) tracks files is this checkout's source
even if it carries a ``.git``, and stays in scope; a submodule (a gitlink entry) is a separate
checkout. When ownership cannot be determined (git missing, failing, or timing out) the directory
stays in scope: a scan reading too much reports noise, one reading too little hides violations.

Not for boundary checks that must see everything on disk (e.g. what enters a build context).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_GITLINK_MODE = b"160000"


def is_other_checkout(directory: Path, root: Path) -> bool:
    directory = Path(directory)
    if not os.path.lexists(directory / ".git"):
        return False
    try:
        rel = directory.relative_to(root).as_posix()
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--stage", "-z", "--", rel],
            capture_output=True, timeout=60, check=False,
        )
    except (ValueError, OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    entries = [entry for entry in proc.stdout.split(b"\0") if entry]
    return all(entry.split(b" ", 1)[0] == _GITLINK_MODE for entry in entries)
