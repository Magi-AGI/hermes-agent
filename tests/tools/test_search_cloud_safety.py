"""Regression tests for Windows cloud-sync safety in search_files.

Searching a Windows home directory that contains OneDrive/Dropbox Files-On-
Demand placeholders can hydrate huge cloud trees. The search tool must fail
closed for broad home/cloud roots and prune known cloud-sync directories from
normal parent searches.
"""

from __future__ import annotations

import tools.file_operations as file_ops
from tools.file_operations import ShellFileOperations


class FakeLocalWindowsEnv:
    def __init__(self, cwd: str, *, rg: bool = True, grep: bool = True, find: bool = True):
        self.cwd = cwd
        self.available = {"rg": rg, "grep": grep, "find": find}
        self.commands: list[str] = []

    def execute(self, command: str, cwd: str | None = None, **_kwargs):
        self.commands.append(command)

        if command.startswith("command -v "):
            tool = command.split()[2]
            return {"output": "yes\n" if self.available.get(tool, False) else "", "returncode": 0}

        if command.startswith("test -e "):
            return {"output": "exists\n", "returncode": 0}

        # Search commands are only inspected by these tests; no real search is
        # necessary and no filesystem traversal should happen.
        return {"output": "", "returncode": 1}


def _force_local_windows(monkeypatch):
    monkeypatch.setattr(file_ops, "_is_local_windows_search_backend", lambda _env: True, raising=False)
    monkeypatch.setattr(file_ops, "_windows_home_path", lambda: "C:/Users/Alice", raising=False)


def test_default_dot_search_is_refused_when_cwd_is_windows_home(monkeypatch):
    _force_local_windows(monkeypatch)
    env = FakeLocalWindowsEnv("C:/Users/Alice")
    ops = ShellFileOperations(env)

    result = ops.search("needle", path=".", target="content")

    assert result.error is not None
    assert "home directory" in result.error
    assert "explicit project subdirectory" in result.error
    assert env.commands == []


def test_direct_cloud_sync_root_search_is_refused(monkeypatch):
    _force_local_windows(monkeypatch)
    env = FakeLocalWindowsEnv("C:/Users/Alice")
    ops = ShellFileOperations(env)

    result = ops.search("*.md", path="C:/Users/Alice/OneDrive - Work", target="files")

    assert result.error is not None
    assert "cloud-sync directory" in result.error
    assert "OneDrive - Work" in result.error
    assert env.commands == []


def test_rg_content_search_prunes_cloud_sync_directories(monkeypatch):
    _force_local_windows(monkeypatch)
    env = FakeLocalWindowsEnv("C:/Users/Alice/work", rg=True)
    ops = ShellFileOperations(env)

    ops.search("needle", path="C:/Users/Alice/work", target="content")

    rg_command = next(command for command in env.commands if "rg --line-number" in command)
    assert "--glob '!OneDrive*/**'" in rg_command
    assert "--glob '!Dropbox/**'" in rg_command
    assert "--glob '!iCloudDrive/**'" in rg_command


def test_grep_content_search_prunes_cloud_sync_directories(monkeypatch):
    _force_local_windows(monkeypatch)
    env = FakeLocalWindowsEnv("C:/Users/Alice/work", rg=False, grep=True)
    ops = ShellFileOperations(env)

    ops.search("needle", path="C:/Users/Alice/work", target="content")

    grep_command = next(command for command in env.commands if "grep -rnH" in command)
    assert "--exclude-dir='OneDrive*'" in grep_command
    assert "--exclude-dir='Dropbox'" in grep_command
    assert "--exclude-dir='iCloudDrive'" in grep_command


def test_find_file_search_prunes_cloud_sync_directories(monkeypatch):
    _force_local_windows(monkeypatch)
    env = FakeLocalWindowsEnv("C:/Users/Alice/work", rg=False, find=True)
    ops = ShellFileOperations(env)

    ops.search("*.py", path="C:/Users/Alice/work", target="files")

    find_command = next(command for command in env.commands if command.startswith("find "))
    assert "-prune" in find_command
    assert "-iname 'OneDrive*'" in find_command
    assert "-iname 'Dropbox'" in find_command
    assert "-iname 'iCloudDrive'" in find_command
