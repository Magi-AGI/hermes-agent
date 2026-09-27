"""Tests for agent.coding_context — RuntimeMode seam, resolver, toolset, git probe."""

import json
import os
import subprocess
import shutil
from pathlib import Path

import pytest

from agent import coding_context as cc


@pytest.fixture(autouse=True)
def _git_probe_bound_is_a_hang_guard(monkeypatch):
    # These tests check what the workspace probe reports, not how fast git answers: at the 2.5 s
    # production bound a loaded runner dropped `rev-parse --git-common-dir` and with it the
    # "Worktree: linked" line. The bound itself is covered by tests/hermes_cli/test_bounded_probe_run.py.
    monkeypatch.setattr(cc, "_GIT_TIMEOUT", 60.0)


def test_coding_guidance_advertises_persistent_terminal_state():
    assert "Terminal state persists across calls" in cc.CODING_AGENT_GUIDANCE
    assert "Activate a virtualenv" in cc.CODING_AGENT_GUIDANCE
    assert "instead of re-sourcing it before every test command" in cc.CODING_AGENT_GUIDANCE


def _git_init(path):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(path),
    }
    # Commit a source file so the fixture is a real *code* workspace: a bare git
    # repo with no code no longer flips into the coding posture (see
    # _detect_profile_name / _has_code_files), so "a code repo" needs code.
    (Path(path) / "main.py").write_text("print('hi')\n")
    for args in (
        ["init", "-q", "-b", "main"],
        # Git for Windows' system config sets core.autocrlf=true; the LF-committed file then
        # reads as modified and the workspace is never "clean".
        ["config", "core.autocrlf", "false"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init commit"],
    ):
        subprocess.run([shutil.which("git"), "-C", str(path), *args], check=True, env=env)


# ── resolver ──────────────────────────────────────────────────────────────

class TestIsCodingContext:



    def test_auto_bare_git_repo_without_code_stays_general(self, tmp_path):
        # A git repo of only prose (notes/writing/research — a big non-coding use
        # case) is NOT a code workspace: .git alone must not flip the posture.
        cfg = {"agent": {"coding_context": "auto"}}
        env = {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path),
        }
        (tmp_path / "notes.md").write_text("# my novel\n")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "notes"]):
            subprocess.run([shutil.which("git"), "-C", str(tmp_path), *args], check=True, env=env)

        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is False
        # …but adding a manifest or source file makes it a code workspace.
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is True


    def test_default_mode_is_auto(self, tmp_path):
        # Unknown/missing value normalizes to auto.
        _git_init(tmp_path)
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config={}) is True


# ── toolset substitution ────────────────────────────────────────────────────

class TestCodingSelection:
    def test_selects_coding_under_focus(self, tmp_path):
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "focus"}}
        out = cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg)
        assert out is not None
        assert out[0] == cc.CODING_TOOLSET

    def test_auto_is_prompt_only(self, tmp_path):
        # Default posture must never override the user's configured toolsets —
        # off-by-default toolsets are already off, and explicit opt-ins
        # (image-gen, spotify, …) survive entering a code workspace.
        _git_init(tmp_path)
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.coding_selection(platform="cli", cwd=tmp_path, config=cfg) is None
        # …while the prompt posture is still active.
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is True






# ── git/workspace probe ─────────────────────────────────────────────────────

class TestWorkspaceBlock:
    def test_empty_outside_repo(self, tmp_path):
        assert cc.build_coding_workspace_block(tmp_path) == ""

    def test_reports_branch_and_clean_status(self, tmp_path):
        _git_init(tmp_path)
        block = cc.build_coding_workspace_block(tmp_path)
        assert "Workspace" in block
        assert f"Root: {tmp_path.resolve()}" in block or "Root:" in block
        assert "Branch: main" in block
        assert "Status: clean" in block
        assert "init commit" in block

    def test_reports_dirty_counts(self, tmp_path):
        _git_init(tmp_path)
        (tmp_path / "untracked.txt").write_text("hi")
        block = cc.build_coding_workspace_block(tmp_path)
        assert "untracked" in block
        assert "clean" not in block.split("Status:")[1].splitlines()[0]


_UNKNOWN = "unknown (git probe unavailable at session start)"


def _git_probes_fail(monkeypatch, *subcommands):
    """git probes whose subcommand is in ``subcommands`` give nothing back, as a timed-out or
    failed ``bounded_git_probe`` does; the rest run for real."""
    real = cc.bounded_git_probe

    def probe(argv, timeout):
        return "" if any(sub in argv for sub in subcommands) else real(argv, timeout=timeout)

    monkeypatch.setattr(cc, "bounded_git_probe", probe)


def _canned_git(monkeypatch, *, status=None):
    """No real git: the quick probes answer at once with a clean ``main`` repo's output, and
    ``status`` runs the given callable (default: clean). Timing tests then depend only on the
    probe they hold, not on how fast git starts on a loaded runner."""
    answers = {"symbolic-ref": "main", "rev-parse": ".git\n.git", "log": "abc1234 init commit"}

    def probe(argv, timeout):
        if "status" in argv:
            return status() if status else "# branch.oid abc\n# branch.head main\n"
        return next(out for sub, out in answers.items() if sub in argv)

    monkeypatch.setattr(cc, "bounded_git_probe", probe)


class TestWorkspaceSnapshotProbeFailures:
    """A git probe that did not answer is reported as unknown, never as a normal state, and the
    other probes' facts survive (A19a: a failed `git status` read as "Status: clean" with no Branch
    line; under load or on a fresh large worktree it took longer than its budget)."""

    def test_failed_status_is_unknown_not_clean_and_the_branch_survives(self, tmp_path, monkeypatch):
        _git_init(tmp_path)
        (tmp_path / "main.py").write_text("print('changed')\n")
        _git_probes_fail(monkeypatch, "status")
        block = cc.build_coding_workspace_block(tmp_path)
        assert f"- Status: {_UNKNOWN}" in block and "Status: clean" not in block
        assert "- Branch: main" in block  # from the cheap symbolic-ref probe
        assert "init commit" in block
        assert "incomplete" in block.splitlines()[0]

    @pytest.mark.parametrize(("subcommand", "unknown_line", "kept"), [
        ("log", f"- Recent commits: {_UNKNOWN}", ["- Branch: main", "- Status: clean"]),
        ("rev-parse", f"- Worktree: {_UNKNOWN}", ["- Branch: main", "- Status: clean", "init commit"]),
        ("symbolic-ref", None, ["- Branch: main", "- Status: clean", "init commit"]),  # status still names it
    ])
    def test_one_failed_probe_leaves_the_others_facts(self, tmp_path, monkeypatch, subcommand, unknown_line, kept):
        _git_init(tmp_path)
        _git_probes_fail(monkeypatch, subcommand)
        block = cc.build_coding_workspace_block(tmp_path)
        for line in kept:
            assert line in block, (line, block)
        if unknown_line:
            assert unknown_line in block and "incomplete" in block.splitlines()[0]
        else:
            assert _UNKNOWN not in block and "incomplete" not in block

    def test_every_probe_failed(self, tmp_path, monkeypatch):
        _git_init(tmp_path)
        _git_probes_fail(monkeypatch, "status", "symbolic-ref", "rev-parse", "log")
        block = cc.build_coding_workspace_block(tmp_path)
        for fact in ("Branch", "Worktree", "Status", "Recent commits"):
            assert f"- {fact}: {_UNKNOWN}" in block, fact
        assert "clean" not in block and "incomplete" in block.splitlines()[0]

    def test_a_probe_past_the_shared_deadline_is_unknown_without_waiting_for_it(self, tmp_path, monkeypatch):
        import threading
        import time

        _git_init(tmp_path)
        stuck = threading.Event()
        _canned_git(monkeypatch, status=lambda: stuck.wait(60) and "")  # never answers in budget
        monkeypatch.setattr(cc, "_GIT_TIMEOUT", 1.0)
        try:
            started = time.monotonic()
            block = cc.build_coding_workspace_block(tmp_path)
            elapsed = time.monotonic() - started
        finally:
            stuck.set()
        assert f"- Status: {_UNKNOWN}" in block and "- Branch: main" in block
        assert elapsed < 10.0  # the shared 1 s deadline, far from the 60 s stuck probe

    def test_an_answer_after_the_deadline_is_not_used(self, tmp_path, monkeypatch):
        from tests.driven_clock import DrivenClock

        _git_init(tmp_path)
        clock = DrivenClock("monotonic")
        monkeypatch.setattr(cc, "time", clock)

        def late_status():
            clock.advance(cc._GIT_TIMEOUT + 1)  # git answered, but only after the cutoff
            return "# branch.oid abc\n# branch.head main\n"

        _canned_git(monkeypatch, status=late_status)
        block = cc.build_coding_workspace_block(tmp_path)
        assert f"- Status: {_UNKNOWN}" in block and "Status: clean" not in block

    @pytest.mark.parametrize("failure", ["refused", "bootstrap-dies"])
    def test_a_lane_that_never_starts_is_unknown_and_bounded(self, tmp_path, monkeypatch, failure):
        import time

        _git_init(tmp_path)
        _canned_git(monkeypatch)
        monkeypatch.setattr(cc, "_GIT_TIMEOUT", 1.0)
        real_start = cc._thread.start_new_thread

        def start(fn, args):
            if args != (("status",),):
                return real_start(fn, args)
            if failure == "refused":
                raise RuntimeError("can't start new thread")
            return real_start(lambda *_: None, ())  # the thread exists but never runs its lane

        monkeypatch.setattr(cc._thread, "start_new_thread", start)
        started = time.monotonic()
        block = cc.build_coding_workspace_block(tmp_path)
        assert time.monotonic() - started < 10.0  # the 1 s deadline, not a hang
        assert f"- Status: {_UNKNOWN}" in block and "- Branch: main" in block and "init commit" in block

    def test_a_refused_lane_still_pins_the_snapshot(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from agent import system_prompt

        _git_init(tmp_path)
        monkeypatch.setattr(system_prompt, "resolve_context_cwd", lambda: tmp_path)
        agent = SimpleNamespace(valid_tool_names={"terminal"}, platform="cli", model="gpt-4o",
                                _frozen_workspace_snapshot=None)
        real_start = cc._thread.start_new_thread

        def refuse_status_lane(fn, args):
            if args == (("status",),):
                raise RuntimeError("can't start new thread")
            return real_start(fn, args)

        with monkeypatch.context() as starved:
            starved.setattr(cc._thread, "start_new_thread", refuse_status_lane)
            first = system_prompt._coding_parts(agent)
        assert first[1] and f"- Status: {_UNKNOWN}" in first[1][0]
        assert system_prompt._coding_parts(agent) == first  # threads available again: same bytes

    # Normal absence is not failure: complete snapshot, no "unknown".
    def test_no_commits_yet_is_normal(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hi')\n")
        subprocess.run([shutil.which("git"), "-C", str(tmp_path), "init", "-q", "-b", "main"], check=True)
        block = cc.build_coding_workspace_block(tmp_path)
        assert "- Branch: main" in block and "Recent commits" not in block
        assert _UNKNOWN not in block and "incomplete" not in block

    def test_detached_head_and_no_upstream_are_normal(self, tmp_path):
        _git_init(tmp_path)
        block = cc.build_coding_workspace_block(tmp_path)
        assert "- Branch: main\n" in block + "\n"  # no upstream: no arrow, still complete
        subprocess.run([shutil.which("git"), "-C", str(tmp_path), "checkout", "-q", "--detach"], check=True)
        block = cc.build_coding_workspace_block(tmp_path)
        assert "- Branch: (detached HEAD)" in block
        assert _UNKNOWN not in block and "incomplete" not in block

    def test_incomplete_snapshot_is_byte_identical_on_rebuild_after_git_recovers(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from agent import system_prompt

        _git_init(tmp_path)
        monkeypatch.setattr(system_prompt, "resolve_context_cwd", lambda: tmp_path)
        agent = SimpleNamespace(valid_tool_names={"terminal"}, platform="cli", model="gpt-4o",
                                _frozen_workspace_snapshot=None)
        with monkeypatch.context() as broken:
            _git_probes_fail(broken, "status")
            first = system_prompt._coding_parts(agent)
        assert first[1] and _UNKNOWN in first[1][0]
        assert cc.build_coding_workspace_block(tmp_path).count(_UNKNOWN) == 0  # git has recovered
        assert system_prompt._coding_parts(agent) == first


# ── project facts (verify-loop detection) ───────────────────────────────────

class TestProjectFacts:




    def test_worktree_detected_without_primary_path(self, tmp_path):
        # A linked worktree should be detected, but the output must NOT contain
        # the absolute path to the primary tree — exposing that path causes the
        # model to sometimes run commands in the wrong directory.
        main_tree = tmp_path / "main"
        main_tree.mkdir()
        _git_init(main_tree)
        worktree = tmp_path / "worktree"
        subprocess.run(
            ["git", "-C", str(main_tree), "worktree", "add", "-b", "wt-branch", str(worktree)],
            check=True,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        block = cc.build_coding_workspace_block(worktree)
        assert "Worktree: linked" in block
        # The primary tree path must NOT appear anywhere in the output.
        assert str(main_tree.resolve()) not in block
        assert str(main_tree) not in block
        # The worktree root IS the reported root.
        assert f"Root: {worktree.resolve()}" in block or "Root:" in block



    def test_detect_project_facts_structured(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest", "dev": "vite"}})
        )
        (tmp_path / "pnpm-lock.yaml").write_text("")
        facts = cc.detect_project_facts(tmp_path)
        assert facts.manifests == ["package.json"]
        assert facts.package_managers == ["pnpm"]
        assert facts.verify_commands == ["pnpm run test"]  # dev excluded
        assert facts.context_files == []

    def test_project_facts_for_matches_prompt_block(self, tmp_path):
        # Invariant: the structured facts the UI consumes must not drift from the
        # commands the prompt snapshot renders — one detector feeds both.
        _git_init(tmp_path)
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest", "lint": "eslint ."}})
        )
        (tmp_path / "pnpm-lock.yaml").write_text("")
        facts = cc.project_facts_for(tmp_path)
        assert facts is not None
        verify_line = cc.build_coding_workspace_block(tmp_path).split("Verify:")[1].splitlines()[0]
        assert facts["verifyCommands"]
        for cmd in facts["verifyCommands"]:
            assert cmd in verify_line



# ── $HOME dotfiles guard ────────────────────────────────────────────────────

class TestHomeDotfilesGuard:
    def test_dotfiles_repo_at_home_is_not_coding(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        _git_init(home)
        monkeypatch.setattr(Path, "home", lambda: home)
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=home, config=cfg) is False
        # …and a plain subdirectory of the dotfiles repo stays general too.
        docs = home / "Documents"
        docs.mkdir()
        assert cc.is_coding_context(platform="cli", cwd=docs, config=cfg) is False


    def test_real_project_under_dotfiles_home_still_detects(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        _git_init(home)
        monkeypatch.setattr(Path, "home", lambda: home)
        proj = home / "www" / "app"
        proj.mkdir(parents=True)
        (proj / "package.json").write_text("{}")
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=proj, config=cfg) is True



# ── prompt assembly integration ─────────────────────────────────────────────

class TestStatusParsing:
    def test_parse_status_counts_and_branch(self):
        porcelain = (
            "# branch.head feature\n"
            "# branch.upstream origin/feature\n"
            "# branch.ab +2 -1\n"
            "1 M. N... 100644 100644 100644 aaa bbb staged.py\n"
            "1 .M N... 100644 100644 100644 ccc ddd modified.py\n"
            "? new.py\n"
            "u UU N... 1 2 3 abc def conflict.py\n"
        )
        branch, counts = cc._parse_status(porcelain)
        assert branch["head"] == "feature"
        assert branch["upstream"] == "origin/feature"
        assert branch["ahead"] == "2" and branch["behind"] == "1"
        assert counts["staged"] == 1
        assert counts["modified"] == 1
        assert counts["untracked"] == 1
        assert counts["conflicts"] == 1


# ── RuntimeMode seam ────────────────────────────────────────────────────────

class TestRuntimeMode:
    def test_resolves_coding_in_repo(self, tmp_path):
        _git_init(tmp_path)
        mode = cc.resolve_runtime_mode(platform="cli", cwd=tmp_path, config={})
        assert mode.is_coding is True
        assert mode.kind == "coding"
        assert mode.profile is cc.CODING_PROFILE

    def test_resolves_general_outside_workspace(self, tmp_path):
        mode = cc.resolve_runtime_mode(platform="cli", cwd=tmp_path, config={})
        assert mode.is_coding is False
        assert mode.kind == "general"
        # General posture pins no toolset and injects no blocks.
        assert mode.toolset_selection() is None
        assert mode.system_blocks() == []





    def test_no_instructions_block_when_unset(self, tmp_path):
        _git_init(tmp_path)
        mode = cc.resolve_runtime_mode(platform="cli", cwd=tmp_path, config={"agent": {"coding_context": "on"}})
        assert not any("Operator instructions" in b for b in mode.system_blocks())



# ── edit-format steering (per-model harness tuning) ──────────────────────────

class TestEditFormatSteering:
    def test_family_detection(self):
        assert cc._model_family("openai/gpt-5.4") == "patch"
        assert cc._model_family("openai/codex-mini") == "patch"
        assert cc._model_family("anthropic/claude-opus-4.8") == "replace"
        assert cc._model_family("anthropic/claude-sonnet-4") == "replace"
        # Gemini + open-weight coding models (RL'd on str_replace-style
        # editors) steer to replace, not neutral.
        for m in (
            "google/gemini-3-pro", "deepseek-v3.2", "qwen3-coder",
            "moonshot/kimi-k2", "zai/glm-4.6", "nousresearch/hermes-4-405b",
        ):
            assert cc._model_family(m) == "replace"
        # Unknown family and no model both fall through to neutral wording.
        assert cc._model_family("acme/foo-1") is None
        assert cc._model_family(None) is None
        assert cc._model_family("") is None

    def test_openai_family_gets_v4a_nudge(self, tmp_path):
        _git_init(tmp_path)
        mode = cc.resolve_runtime_mode(
            platform="cli", cwd=tmp_path,
            config={"agent": {"coding_context": "on"}}, model="openai/gpt-5.4",
        )
        brief = mode.system_blocks()[0]
        assert "mode='patch'" in brief
        assert "V4A" in brief
        assert "write_file" in brief  # new files authored, not patched
        # Codex-family harnesses ship apply_patch (V4A) as the ONLY editor and
        # instruct it even for single-file edits — never nudge replace mode.
        assert "single-file" in brief
        assert "mode='replace'" not in brief






# ── profile registry ────────────────────────────────────────────────────────

class TestProfiles:


    def test_coding_profile_shape(self):
        # The coding profile declares the seams other domains read.
        assert cc.CODING_PROFILE.toolset == cc.CODING_TOOLSET
        assert cc.CODING_PROFILE.guidance
        assert cc.CODING_PROFILE.model_hint == "coding"
        # General is inert.
        assert cc.GENERAL_PROFILE.toolset is None
        assert cc.GENERAL_PROFILE.guidance == ""

    def test_skill_demotion_gated_on_focus(self, tmp_path):
        # Names-only demotion is opt-in via focus mode — the default (auto)
        # and forced (on) postures leave the skill index untouched. Under
        # focus, clearly-non-coding categories are demoted (never hidden) and
        # coding-adjacent ones keep full entries (deny-list semantics).
        _git_init(tmp_path)
        for raw in ("auto", "on"):
            mode = cc.resolve_runtime_mode(
                platform="cli", cwd=tmp_path, config={"agent": {"coding_context": raw}}
            )
            assert mode.is_coding is True
            assert mode.compact_skill_categories() == frozenset()
        focus = cc.resolve_runtime_mode(
            platform="cli", cwd=tmp_path,
            config={"agent": {"coding_context": "focus"}},
        )
        assert focus.is_coding is True
        compact = focus.compact_skill_categories()
        assert "social-media" in compact and "smart-home" in compact
        for kept in ("github", "devops", "software-development", "data-science"):
            assert kept not in compact
        # General posture demotes nothing.
        general = cc.resolve_runtime_mode(platform="telegram", cwd=tmp_path, config={})
        assert general.compact_skill_categories() == frozenset()


# ── detection signals ───────────────────────────────────────────────────────

class TestDetection:
    @pytest.mark.parametrize("marker", ["pyproject.toml", "package.json", "go.mod", "AGENTS.md"])
    def test_project_manifest_triggers_without_git(self, tmp_path, marker):
        (tmp_path / marker).write_text("x")
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is True

    def test_marker_in_parent_counts_from_subdir(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("x")
        sub = tmp_path / "src" / "pkg"
        sub.mkdir(parents=True)
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=sub, config=cfg) is True

    def test_bare_dir_is_not_coding(self, tmp_path):
        cfg = {"agent": {"coding_context": "auto"}}
        assert cc.is_coding_context(platform="cli", cwd=tmp_path, config=cfg) is False
