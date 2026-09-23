"""Automatic git probes never write the index.

``git status`` normally refreshes a stat-dirty index and writes it back under ``.git/index.lock``.
The session-start workspace snapshot (``agent/coding_context.py``) and the TUI branch probe run
``bounded_git_probe``, which kills git at its timeout; a probe killed mid-refresh left a stale
``index.lock`` in the user's repo (seen twice in one Windows test run), and the user's next
commit/checkout then fails until the lock is deleted. Probes run with ``GIT_OPTIONAL_LOCKS=0``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from hermes_cli._subprocess_compat import bounded_git_probe, noninteractive_git_env

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env=noninteractive_git_env())


def _stat_dirty_repo(tmp_path):
    """A repo whose tracked file's mtime moved without a content change: a plain ``git status``
    rewrites the index to record the new stat data."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "root")
    (repo / "tracked.txt").write_text("same content\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "add")
    index = repo / ".git" / "index"
    old = time.time() - 3600
    os.utime(index, (old, old))  # the index predates the file's new mtime
    os.utime(repo / "tracked.txt", (old + 60, old + 60))
    return repo, index


def test_a_status_probe_does_not_rewrite_the_index(tmp_path):
    repo, index = _stat_dirty_repo(tmp_path)
    before = index.stat().st_mtime_ns

    out = bounded_git_probe(["git", "-C", str(repo), "status", "--porcelain=2", "--branch"], timeout=30.0)

    assert "branch.head" in out, out
    assert index.stat().st_mtime_ns == before, "the probe took index.lock and rewrote the index"
    assert not (repo / ".git" / "index.lock").exists()


def test_control_a_plain_status_does_rewrite_the_stat_dirty_index(tmp_path):
    """Control: proves the fixture really is stat-dirty, so the test above is not vacuous."""
    repo, index = _stat_dirty_repo(tmp_path)
    before = index.stat().st_mtime_ns
    _git(repo, "status", "--porcelain=2")
    assert index.stat().st_mtime_ns != before
