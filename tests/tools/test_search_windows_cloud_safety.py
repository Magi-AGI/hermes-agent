"""Windows cloud-placeholder safety for file/content search.

OneDrive/Dropbox/iCloud "Files On-Demand" placeholders hydrate (download the
real bytes) simply because a recursive search opens or stats them.  These tests
prove that ``ShellFileOperations.search``:

  - refuses an explicit cloud root before any shell I/O;
  - excludes cloud dirs when searching a broad Windows home root;
  - leaves scoped, non-cloud project paths completely untouched (no regression
    for normal repo searches, including on non-Windows layouts).

All shell interaction is mocked — nothing here touches a real OneDrive/Dropbox
folder or the local filesystem's cloud state.
"""

from unittest.mock import MagicMock

from tools.file_operations import (
    ShellFileOperations,
    _windows_cloud_search_policy,
)


# ============================================================================
# _windows_cloud_search_policy — pure unit tests (no shell)
# ============================================================================

class TestWindowsCloudSearchPolicy:
    def test_explicit_cloud_root_blocks(self):
        policy = _windows_cloud_search_policy("C:/Users/TestUser/OneDrive - Personal")
        assert policy.block_reason is not None
        assert "cloud-synced" in policy.block_reason
        assert policy.add_excludes is False

    def test_dropbox_nested_path_blocks(self):
        policy = _windows_cloud_search_policy("C:/Users/TestUser/Dropbox/work/repo")
        assert policy.block_reason is not None
        assert policy.warning is None

    def test_windows_home_root_warns_and_excludes(self):
        policy = _windows_cloud_search_policy("C:/Users/TestUser")
        assert policy.block_reason is None
        assert policy.add_excludes is True
        assert policy.warning is not None
        assert "cloud-synced" in policy.warning

    def test_msys_home_root_warns_and_excludes(self):
        policy = _windows_cloud_search_policy("/c/Users/TestUser")
        assert policy.add_excludes is True
        assert policy.warning is not None

    def test_scoped_project_path_is_untouched(self):
        policy = _windows_cloud_search_policy("C:/Users/TestUser/projects/hermes")
        assert policy.block_reason is None
        assert policy.warning is None
        assert policy.add_excludes is False

    def test_non_windows_relative_path_is_untouched(self):
        policy = _windows_cloud_search_policy("src/tools")
        assert policy.block_reason is None
        assert policy.warning is None
        assert policy.add_excludes is False

    def test_posix_home_is_untouched(self):
        # /home/testuser is not the Windows C:/Users pattern — no cloud policy.
        policy = _windows_cloud_search_policy("/home/testuser")
        assert policy.block_reason is None
        assert policy.add_excludes is False


# ============================================================================
# ShellFileOperations.search — end-to-end command shaping
# ============================================================================

def _make_ops(cwd, monkeypatch=None, commands=None):
    """Build ShellFileOperations over a mocked terminal env.

    ``commands`` (if provided) accumulates every command string passed to
    ``env.execute``.  ``test -e`` probes report the path exists; all other
    commands return a single fake match line.
    """
    env = MagicMock(cwd=cwd)

    def execute(command, **kwargs):
        if commands is not None:
            commands.append(command)
        if "test -e" in command:
            return {"output": "exists", "returncode": 0}
        return {"output": "project/a.txt:1:needle\n", "returncode": 0}

    env.execute.side_effect = execute
    ops = ShellFileOperations(env)
    if monkeypatch is not None:
        monkeypatch.setattr(ops, "_has_command", lambda cmd: cmd == "rg")
    return ops, env


def test_explicit_windows_cloud_root_is_refused_before_shell_probe():
    env = MagicMock(cwd="C:/Users/TestUser")
    env.execute.side_effect = AssertionError("cloud root should be rejected before shell I/O")
    ops = ShellFileOperations(env)

    result = ops.search("needle", path="C:/Users/TestUser/OneDrive - Personal", target="content")

    assert result.error is not None
    assert "cloud-synced" in result.error
    assert "OneDrive" in result.error
    env.execute.assert_not_called()


def test_windows_home_root_rg_content_search_excludes_cloud_dirs(monkeypatch):
    commands: list[str] = []
    ops, _ = _make_ops("C:/Users/TestUser", monkeypatch, commands)

    result = ops.search("needle", path=".", target="content")

    assert result.error is None
    assert result.warning is not None
    assert "cloud-synced" in result.warning
    search_commands = [cmd for cmd in commands if cmd.startswith("set -o pipefail; rg")]
    assert search_commands, commands
    rg_command = search_commands[-1]
    assert "--glob '!OneDrive*/**'" in rg_command
    assert "--glob '!Dropbox*/**'" in rg_command
    assert "--glob '!iCloudDrive*/**'" in rg_command


def test_msys_windows_home_root_gets_same_cloud_safety(monkeypatch):
    commands: list[str] = []
    ops, _ = _make_ops("/c/Users/TestUser", monkeypatch, commands)

    result = ops.search("needle", path=".", target="content")

    assert result.error is None
    assert result.warning is not None
    assert any("--glob '!OneDrive*/**'" in command for command in commands)


def test_windows_home_root_files_search_excludes_cloud_dirs(monkeypatch):
    commands: list[str] = []
    ops, _ = _make_ops("C:/Users/TestUser", monkeypatch, commands)

    result = ops.search("*.py", path=".", target="files")

    assert result.error is None
    rg_file_commands = [cmd for cmd in commands if cmd.startswith("rg --files")]
    assert rg_file_commands, commands
    assert any("--glob '!OneDrive*/**'" in cmd for cmd in rg_file_commands)
    assert any("--glob '!Dropbox*/**'" in cmd for cmd in rg_file_commands)


def test_files_fallback_find_prunes_cloud_dirs(monkeypatch):
    """When only `find` is available, cloud dirs must be PRUNED (not just
    filtered out of output) so find never descends/stats the placeholder tree.
    Asserts the emitted find command uses a -prune expression positioned
    before the -type f match predicate."""
    commands: list[str] = []

    def execute(command, **kwargs):
        commands.append(command)
        return {"output": "", "returncode": 0}

    env = MagicMock(cwd="C:/Users/TestUser")
    env.execute.side_effect = execute
    ops = ShellFileOperations(env)
    monkeypatch.setattr(ops, "_has_command", lambda cmd: cmd == "find")

    ops._search_files("*.log", "C:/Users/TestUser", 50, 0, exclude_windows_cloud_dirs=True)

    find_cmds = [cmd for cmd in commands if cmd.lstrip().startswith("find ")]
    assert find_cmds, commands
    cmd = find_cmds[0]
    # Cloud dirs are pruned, not merely filtered from output.
    assert "-prune" in cmd
    assert "-name 'OneDrive*'" in cmd
    assert "-name 'Dropbox*'" in cmd
    assert "-name 'iCloudDrive*'" in cmd
    assert "-name 'Creative Cloud Files*'" in cmd
    # The prune must happen before the file-type/name match, so find skips the
    # subtree entirely instead of stat-ing it and discarding the result.
    assert cmd.index("-prune") < cmd.index("-type f -name")


def test_files_fallback_find_no_prune_for_scoped_path(monkeypatch):
    """A scoped (non-home-root) path adds no prune expression — the fallback
    find command is unchanged from upstream behavior."""
    commands: list[str] = []

    def execute(command, **kwargs):
        commands.append(command)
        if "test -e" in command:
            return {"output": "exists", "returncode": 0}
        return {"output": "", "returncode": 0}

    env = MagicMock(cwd="C:/Users/TestUser/projects/hermes")
    env.execute.side_effect = execute
    ops = ShellFileOperations(env)
    monkeypatch.setattr(ops, "_has_command", lambda cmd: cmd == "find")

    ops.search("*.log", path="C:/Users/TestUser/projects/hermes", target="files")

    find_cmds = [cmd for cmd in commands if cmd.lstrip().startswith("find ")]
    assert find_cmds, commands
    assert all("-prune" not in cmd for cmd in find_cmds)
    assert all("OneDrive" not in cmd for cmd in find_cmds)


def test_scoped_project_path_has_no_cloud_excludes_or_warning(monkeypatch):
    """A normal, non-cloud repo path is searched exactly as before — no
    warning and no cloud --glob exclusions injected."""
    commands: list[str] = []
    ops, _ = _make_ops("C:/Users/TestUser/projects/hermes", commands=commands, monkeypatch=monkeypatch)

    result = ops.search("needle", path="C:/Users/TestUser/projects/hermes", target="content")

    assert result.error is None
    assert result.warning is None
    search_commands = [cmd for cmd in commands if cmd.startswith("set -o pipefail; rg")]
    assert search_commands, commands
    assert all("OneDrive" not in cmd for cmd in search_commands)


def test_non_windows_cwd_relative_search_has_no_cloud_excludes(monkeypatch):
    """A POSIX working directory (no C:/Users pattern) is unaffected — the
    guard is a no-op off Windows-style home roots."""
    commands: list[str] = []
    ops, _ = _make_ops("/home/testuser/hermes", commands=commands, monkeypatch=monkeypatch)

    result = ops.search("needle", path=".", target="content")

    assert result.error is None
    assert result.warning is None
    search_commands = [cmd for cmd in commands if cmd.startswith("set -o pipefail; rg")]
    assert search_commands, commands
    assert all("OneDrive" not in cmd for cmd in search_commands)
