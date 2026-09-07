"""Tests for the 2026-09-06 anti-bloat mechanism: a mechanical dry-run guard on the curator's
preview fork, and a size-ceiling guard on the routine background-review pass. See
DESIGN-SKILL-CURATOR-ANTI-BLOAT-2026-09-06.md for the full design."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tools.skill_manager_tool import _create_skill, _find_skill, skill_manage
from tools.skill_provenance import (
    BACKGROUND_REVIEW,
    CURATOR_CONSOLIDATION,
    CURATOR_CONSOLIDATION_DRY_RUN,
    is_background_review,
    is_curator_dry_run,
    is_routine_background_review,
    reset_current_write_origin,
    set_current_write_origin,
)

VALID_SKILL_CONTENT = """\
---
name: test-skill
description: A test skill for unit testing.
---

# Test Skill

Step 1: Do the thing.
"""


@contextmanager
def _skill_dir(tmp_path):
    with patch("tools.skill_manager_tool.SKILLS_DIR", tmp_path), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[tmp_path]):
        yield


@contextmanager
def _as_origin(origin: str, *, curator_managed: bool = True):
    """Run the body under the given write-origin, with ownership pre-satisfied (all three
    autonomous origins hit the same ownership guard before reaching the guards under test)."""
    with patch("tools.skill_usage._is_curator_managed_record", return_value=curator_managed):
        token = set_current_write_origin(origin)
        try:
            yield
        finally:
            reset_current_write_origin(token)


# ---------------------------------------------------------------------------
# skill_provenance: origin taxonomy
# ---------------------------------------------------------------------------


class TestOriginTaxonomy:
    def test_all_three_autonomous_origins_are_background_review(self):
        for origin in (BACKGROUND_REVIEW, CURATOR_CONSOLIDATION_DRY_RUN, CURATOR_CONSOLIDATION):
            token = set_current_write_origin(origin)
            try:
                assert is_background_review() is True
            finally:
                reset_current_write_origin(token)

    def test_only_background_review_is_routine(self):
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert is_routine_background_review() is True
        finally:
            reset_current_write_origin(token)
        for origin in (CURATOR_CONSOLIDATION_DRY_RUN, CURATOR_CONSOLIDATION, "foreground"):
            token = set_current_write_origin(origin)
            try:
                assert is_routine_background_review() is False
            finally:
                reset_current_write_origin(token)

    def test_only_dry_run_origin_is_curator_dry_run(self):
        token = set_current_write_origin(CURATOR_CONSOLIDATION_DRY_RUN)
        try:
            assert is_curator_dry_run() is True
        finally:
            reset_current_write_origin(token)
        for origin in (BACKGROUND_REVIEW, CURATOR_CONSOLIDATION, "foreground"):
            token = set_current_write_origin(origin)
            try:
                assert is_curator_dry_run() is False
            finally:
                reset_current_write_origin(token)


# ---------------------------------------------------------------------------
# Mechanical dry-run mutation guard (Codex's required amendment #1)
# ---------------------------------------------------------------------------


class TestCuratorDryRunMutationGuard:
    """The prompt banner is advisory; this guard is what actually stops a mutating call."""

    @pytest.mark.parametrize("action,kwargs", [
        ("create", {"content": VALID_SKILL_CONTENT}),
        ("edit", {"content": VALID_SKILL_CONTENT}),
        ("patch", {"old_string": "Do the thing.", "new_string": "Do the new thing."}),
        ("write_file", {"file_path": "references/x.md", "file_content": "x"}),
        ("remove_file", {"file_path": "references/x.md"}),
        ("delete", {"absorbed_into": ""}),
    ])
    def test_every_mutating_action_is_refused_during_dry_run(self, tmp_path, action, kwargs):
        with _skill_dir(tmp_path):
            _create_skill("test-skill", VALID_SKILL_CONTENT)
            with _as_origin(CURATOR_CONSOLIDATION_DRY_RUN):
                raw = skill_manage(action=action, name="test-skill", **kwargs)
        result = json.loads(raw)
        assert result["success"] is False
        assert "dry-run" in result["error"].lower()

    def test_read_only_actions_are_unaffected_by_dry_run(self, tmp_path):
        """skills_list/skill_view never go through skill_manage, but a non-mutating
        skill_manage call (there are none today) or a read tool must never be touched by
        this guard — sanity check that the guard is keyed on action, not blanket-refusing."""
        from tools.skill_manager_guards import _curator_dry_run_mutation_guard
        with _as_origin(CURATOR_CONSOLIDATION_DRY_RUN):
            assert _curator_dry_run_mutation_guard("not_a_real_action") is None

    def test_foreground_writes_are_never_blocked_by_the_dry_run_guard(self, tmp_path):
        with _skill_dir(tmp_path):
            _create_skill("test-skill", VALID_SKILL_CONTENT)
            raw = skill_manage(
                action="patch", name="test-skill",
                old_string="Do the thing.", new_string="Do the new thing.")
        assert json.loads(raw)["success"] is True

    def test_live_curator_consolidation_apply_is_not_blocked_by_the_dry_run_guard(self, tmp_path):
        """CURATOR_CONSOLIDATION (the real --consolidate apply, not the preview) must still be
        able to mutate — only CURATOR_CONSOLIDATION_DRY_RUN is mechanically blocked."""
        with _skill_dir(tmp_path):
            _create_skill("test-skill", VALID_SKILL_CONTENT)
            with _as_origin(CURATOR_CONSOLIDATION):
                from tools.skill_manager_guards import mark_background_review_skill_read
                mark_background_review_skill_read(tmp_path / "test-skill" / "SKILL.md")
                raw = skill_manage(
                    action="patch", name="test-skill",
                    old_string="Do the thing.", new_string="Do the new thing.")
        assert json.loads(raw)["success"] is True


# ---------------------------------------------------------------------------
# Size-ceiling guard (Codex's required amendment #2 — lives in _guarded_write)
# ---------------------------------------------------------------------------


def _oversized_skill(tmp_path, name="big-skill", *, ref_files: int = 0) -> None:
    _create_skill(name, VALID_SKILL_CONTENT)
    skill_dir = tmp_path / name
    # Pad AFTER the real content (inside an HTML comment, so it doesn't break frontmatter
    # parsing) rather than replacing it, so "Do the thing." stays patchable/shrinkable.
    padded = VALID_SKILL_CONTENT + f"\n<!-- {'x' * 50_000} -->\n"  # over the 40KB default
    (skill_dir / "SKILL.md").write_text(padded, encoding="utf-8")
    if ref_files:
        refs = skill_dir / "references"
        refs.mkdir(exist_ok=True)
        for i in range(ref_files):
            (refs / f"r{i}.md").write_text("content", encoding="utf-8")


class TestSizeCeilingGuard:
    def test_routine_pass_refused_growing_an_oversized_skill_md(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _oversized_skill(tmp_path)
            with _as_origin(BACKGROUND_REVIEW):
                from tools.skill_manager_guards import mark_background_review_skill_read
                mark_background_review_skill_read(tmp_path / "big-skill" / "SKILL.md")
                raw = skill_manage(
                    action="patch", name="big-skill",
                    old_string="Do the thing.", new_string="Do the new thing, and here's a whole extra sentence.")
        result = json.loads(raw)
        assert result["success"] is False
        assert "ceiling" in result["error"].lower() or "40" in result["error"]

    def test_routine_pass_refused_adding_a_new_reference_file_over_the_file_count_ceiling(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _create_skill("ref-heavy", VALID_SKILL_CONTENT)
            skill_dir = tmp_path / "ref-heavy"
            refs = skill_dir / "references"
            refs.mkdir()
            for i in range(10):  # already at the default ceiling
                (refs / f"r{i}.md").write_text("x", encoding="utf-8")
            with _as_origin(BACKGROUND_REVIEW):
                raw = skill_manage(
                    action="write_file", name="ref-heavy",
                    file_path="references/one-too-many.md", file_content="new content")
        result = json.loads(raw)
        assert result["success"] is False
        assert "reference" in result["error"].lower()

    def test_shrinking_edit_is_allowed_even_over_the_ceiling(self, tmp_path, monkeypatch):
        """Antigravity's asymmetric-guard requirement: deletions/shrinks must never be blocked —
        blocking them would make an oversized skill impossible to fix by hand."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _oversized_skill(tmp_path)
            skill_md = tmp_path / "big-skill" / "SKILL.md"
            original = skill_md.read_text(encoding="utf-8")
            with _as_origin(BACKGROUND_REVIEW):
                from tools.skill_manager_guards import mark_background_review_skill_read
                mark_background_review_skill_read(skill_md)
                # A full-rewrite edit with strictly less content than the original.
                raw = skill_manage(action="edit", name="big-skill", content=original[: len(original) // 2])
        assert json.loads(raw)["success"] is True

    def test_removing_a_reference_file_is_allowed_even_over_the_ceiling(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _oversized_skill(tmp_path, ref_files=12)
            with _as_origin(BACKGROUND_REVIEW):
                from tools.skill_manager_guards import mark_background_review_skill_read
                mark_background_review_skill_read(tmp_path / "big-skill" / "references" / "r0.md")
                raw = skill_manage(action="remove_file", name="big-skill", file_path="references/r0.md")
        assert json.loads(raw)["success"] is True

    def test_foreground_writes_are_never_capped_by_the_ceiling(self, tmp_path, monkeypatch):
        """Antigravity's actor-scope requirement: a user-directed session must be able to keep
        extending a large skill it explicitly asked for — only the autonomous routine pass is
        capped."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _oversized_skill(tmp_path)
            raw = skill_manage(
                action="patch", name="big-skill",
                old_string="Do the thing.", new_string="Do the new thing, and here's a whole extra sentence.")
        assert json.loads(raw)["success"] is True

    def test_curator_live_consolidation_apply_is_never_capped_by_the_ceiling(self, tmp_path, monkeypatch):
        """The consolidation-apply fork's entire job is shrinking an oversized skill — it must
        not be blocked by the very ceiling that flagged the skill in the first place, even on a
        write that (mid-restructure) happens to look additive."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _oversized_skill(tmp_path)
            with _as_origin(CURATOR_CONSOLIDATION):
                from tools.skill_manager_guards import mark_background_review_skill_read
                mark_background_review_skill_read(tmp_path / "big-skill" / "SKILL.md")
                raw = skill_manage(
                    action="patch", name="big-skill",
                    old_string="Do the thing.", new_string="Do the new thing, and here's a whole extra sentence.")
        assert json.loads(raw)["success"] is True

    def test_skill_under_the_ceiling_is_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _create_skill("small-skill", VALID_SKILL_CONTENT)
            with _as_origin(BACKGROUND_REVIEW):
                from tools.skill_manager_guards import mark_background_review_skill_read
                mark_background_review_skill_read(tmp_path / "small-skill" / "SKILL.md")
                raw = skill_manage(
                    action="patch", name="small-skill",
                    old_string="Do the thing.", new_string="Do the new thing.")
        assert json.loads(raw)["success"] is True

    def test_nested_reference_files_count_toward_the_ceiling(self, tmp_path, monkeypatch):
        """Codex's amendment #5: write_file allows nested paths under references/
        (references/sub/x.md); a direct-children-only count would silently exempt anything
        nested from the ceiling."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _create_skill("ref-heavy", VALID_SKILL_CONTENT)
            skill_dir = tmp_path / "ref-heavy"
            nested = skill_dir / "references" / "sub"
            nested.mkdir(parents=True)
            for i in range(10):  # already at the default ceiling, all nested one level down
                (nested / f"r{i}.md").write_text("x", encoding="utf-8")
            with _as_origin(BACKGROUND_REVIEW):
                raw = skill_manage(
                    action="write_file", name="ref-heavy",
                    file_path="references/sub/one-too-many.md", file_content="new content")
        result = json.loads(raw)
        assert result["success"] is False
        assert "reference" in result["error"].lower()

    def test_a_nested_new_reference_file_is_itself_counted_as_the_additive_write(self, tmp_path, monkeypatch):
        """The write that WOULD tip a nested-heavy skill over the ceiling must itself be the one
        refused, matching the flat (non-nested) behavior already covered above."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with _skill_dir(tmp_path):
            _create_skill("ref-heavy-2", VALID_SKILL_CONTENT)
            skill_dir = tmp_path / "ref-heavy-2"
            nested = skill_dir / "references" / "sub"
            nested.mkdir(parents=True)
            for i in range(9):  # one below the ceiling
                (nested / f"r{i}.md").write_text("x", encoding="utf-8")
            with _as_origin(BACKGROUND_REVIEW):
                raw = skill_manage(
                    action="write_file", name="ref-heavy-2",
                    file_path="references/sub/tenth.md", file_content="new content")
        # Exactly at the ceiling (10) is allowed; the guard only refuses when it would GROW past.
        assert json.loads(raw)["success"] is True

    def test_ceiling_refusal_records_a_consolidation_recommended_flag(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        monkeypatch.setenv("HERMES_HOME", str(home))
        with _skill_dir(tmp_path):
            _oversized_skill(tmp_path)
            with _as_origin(BACKGROUND_REVIEW):
                from tools.skill_manager_guards import mark_background_review_skill_read
                mark_background_review_skill_read(tmp_path / "big-skill" / "SKILL.md")
                skill_manage(
                    action="patch", name="big-skill",
                    old_string="Do the thing.", new_string="Do the new thing, and here's more.")
        from agent import curator
        assert "big-skill" in curator.skills_needing_consolidation_preview()
