"""Git Bash is found wherever Git for Windows is installed (A17 r1, Antigravity).

Git for Windows puts only ``<Git>\\cmd`` on PATH, and the fixed guesses (``%ProgramFiles%\\Git``,
``%LOCALAPPDATA%\\...``) name one drive: an install on another drive (this deploy host keeps Git on
``E:``) left no candidate, and a bare ``bash`` would be System32's WSL launcher.
"""

import pytest

from tools.environments.local import _windows_bash_candidates


@pytest.mark.windows_only
def test_bash_beside_the_git_on_path_is_a_candidate(tmp_path, monkeypatch):
    git_root = tmp_path / "Tools" / "Git"
    (git_root / "cmd").mkdir(parents=True)
    (git_root / "bin").mkdir()
    (git_root / "cmd" / "git.exe").write_bytes(b"")
    (git_root / "bin" / "bash.exe").write_bytes(b"")
    empty = tmp_path / "empty"
    empty.mkdir()
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.setenv(var, str(empty))
    monkeypatch.setenv("PATH", str(git_root / "cmd"))

    assert str(git_root / "bin" / "bash.exe") in _windows_bash_candidates(None)
