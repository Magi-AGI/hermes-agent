from unittest.mock import MagicMock

from tools.file_operations import ShellFileOperations


def test_explicit_windows_cloud_root_is_refused_before_shell_probe():
    env = MagicMock(cwd="C:/Users/Lake")
    env.execute.side_effect = AssertionError("cloud root should be rejected before shell I/O")
    ops = ShellFileOperations(env)

    result = ops.search("needle", path="C:/Users/Lake/OneDrive - Personal", target="content")

    assert result.error is not None
    assert "cloud-synced" in result.error
    assert "OneDrive" in result.error
    env.execute.assert_not_called()


def test_windows_home_root_rg_content_search_excludes_cloud_dirs(monkeypatch):
    env = MagicMock(cwd="C:/Users/Lake")
    commands: list[str] = []

    def execute(command, **kwargs):
        commands.append(command)
        if "test -e" in command:
            return {"output": "exists", "returncode": 0}
        return {"output": "project/a.txt:1:needle\n", "returncode": 0}

    env.execute.side_effect = execute
    ops = ShellFileOperations(env)
    monkeypatch.setattr(ops, "_has_command", lambda cmd: cmd == "rg")

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
    env = MagicMock(cwd="/c/Users/Lake")
    commands: list[str] = []

    def execute(command, **kwargs):
        commands.append(command)
        if "test -e" in command:
            return {"output": "exists", "returncode": 0}
        return {"output": "project/a.txt:1:needle\n", "returncode": 0}

    env.execute.side_effect = execute
    ops = ShellFileOperations(env)
    monkeypatch.setattr(ops, "_has_command", lambda cmd: cmd == "rg")

    result = ops.search("needle", path=".", target="content")

    assert result.error is None
    assert result.warning is not None
    assert any("--glob '!OneDrive*/**'" in command for command in commands)
