"""Tests for AIAgent._repair_tool_call — CASE-ONLY tool-name repair (Magi divergence, round 6).

Upstream repaired names by lowercasing, ``-``/space -> ``_``, camelCase splitting, ``_tool``
suffix stripping (#14784), XML-fragment trimming (#33007) and difflib fuzzy matching (#520).
Any of those can land on a different tool than the one the model named, so Magi removed them
(Lake, 2026-09-30): only a name that differs from exactly one registered name (across every
profile) by letter case, and that is visible to the session, is renamed. Everything the old
tests below expected to be repaired now returns None, and the turn answers with an honest error.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


VALID = {
    "todo", "patch", "browser_click", "browser_navigate", "web_search", "read_file",
    "write_file", "terminal", "execute_code", "session_search",
}
REGISTERED = frozenset(VALID | {"read_terminal"})


@pytest.fixture
def repair():
    """A bound _repair_tool_call on a shell agent (the routine reads only valid_tool_names and
    the registry's all-scope name set, which is patched per test)."""
    from run_agent import AIAgent
    stub = SimpleNamespace(valid_tool_names=VALID)
    return AIAgent._repair_tool_call.__get__(stub, AIAgent)


@pytest.fixture
def registered():
    from tools.registry import registry

    with patch.object(registry, "get_tool_names_all_scopes", return_value=REGISTERED) as names:
        yield names


class TestCaseOnlyRepair:
    def test_exact_visible_name_passes_through(self, repair, registered):
        assert repair("browser_click") == "browser_click"

    def test_case_only_difference_is_repaired(self, repair, registered):
        assert repair("Write_File") == "write_file"
        assert repair("TERMINAL") == "terminal"

    def test_case_only_match_that_is_not_visible_is_not_repaired(self, repair, registered):
        """``Read_Terminal`` matches the registered read_terminal, which this session cannot
        call directly: never renamed (the error path gives the tool_call correction if the
        bridge reaches it)."""
        assert repair("Read_Terminal") is None

    def test_two_case_insensitive_matches_anywhere_block_the_repair(self, repair, registered):
        registered.return_value = REGISTERED | {"Write_File"}  # e.g. another profile's plugin tool
        assert repair("WRITE_FILE") is None

    def test_registry_read_failure_repairs_nothing(self, repair):
        from tools.registry import registry

        with patch.object(registry, "get_tool_names_all_scopes", side_effect=OSError("registry read failed")):
            assert repair("Write_File") is None


class TestNoOtherVariants:
    """Formerly repaired (upstream #520 / #14784 / #33007); now None by design."""

    @pytest.mark.parametrize("name", [
        "write file",             # space -> underscore (was repaired)
        "write-file",             # hyphen -> underscore
        "BrowserClick",           # camelCase split (#14784)
        "TodoTool_tool",          # suffix stripping (#14784)
        "Patch_tool",
        'terminal" parameter="command" string="true',  # VolcEngine XML fragment (#33007)
        'terminal"',
        '"terminal"',
        "termnal",                # fuzzy (#520)
        "read_termnal",
    ])
    def test_not_repaired(self, repair, registered, name):
        assert repair(name) is None


class TestEdgeCases:
    def test_empty_string(self, repair, registered):
        assert repair("") is None
