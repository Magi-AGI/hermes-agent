"""The execution-discipline guidance must not turn reversible writes into approval gates.

Magi fork policy (Lake, 2026-10-30): the lead agent proceeds on reversible work inside the
stated scope and reports it; confirmation is reserved for irreversible or outward-facing steps.
The upstream sentence "if the next step has side effects (file writes, commands, API calls),
confirm scope before executing" is injected into the system prompt of every GPT/Codex/Grok and
open-weight session (``agent.execution_guidance: auto``) and outranks any skill that says
otherwise, so the contract is pinned here on the text itself.
"""
import re

from agent.prompt_builder import OPENAI_MODEL_EXECUTION_GUIDANCE


def _safety_line() -> str:
    lines = [l for l in OPENAI_MODEL_EXECUTION_GUIDANCE.splitlines() if l.lstrip().startswith("- Safety:")]
    assert len(lines) == 1, lines
    return lines[0]


def test_reversible_writes_are_not_a_confirmation_trigger():
    safety = _safety_line()
    assert "confirm scope before executing" not in safety
    assert not re.search(r"file writes, commands, API calls", safety)


def test_confirmation_is_reserved_for_irreversible_or_outward_facing_steps():
    safety = _safety_line()
    assert "irreversible" in safety and "outward" in safety
    assert "proceed" in safety  # reversible, in-scope writes proceed and are reported
