"""Skill write-origin provenance: a ContextVar separating background-review skill writes from foreground
user-directed writes (the curator only curates skills the self-improvement review fork created; skills a user
asked for belong to the user). run_agent.py binds the origin before each tool loop, mirroring
AIAgent._memory_write_origin: ``token = set_current_write_origin(...)`` / ``reset_current_write_origin(token)``."""

import contextvars

_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar("skill_write_origin", default="foreground")
BACKGROUND_REVIEW = "background_review"  # sentinel used by run_agent._spawn_background_review (routine per-session pass)
CURATOR_CONSOLIDATION_DRY_RUN = "curator_consolidation_dry_run"  # curator.py preview fork (report-only)
CURATOR_CONSOLIDATION = "curator_consolidation"  # curator.py live-apply fork (hermes curator run --consolidate)

# Every autonomous (no-user-in-the-loop) actor. All three get the existing pinned/external/
# bundled/hub/user-owned and read-before-write guards. They are NOT interchangeable beyond
# that: only BACKGROUND_REVIEW is subject to the size-ceiling guard (the curator's own
# consolidation pass is the intended REMEDY for an oversized skill, not another contributor to
# the problem), and only CURATOR_CONSOLIDATION_DRY_RUN is subject to the blanket mutation guard.
_AUTONOMOUS_ORIGINS = frozenset({BACKGROUND_REVIEW, CURATOR_CONSOLIDATION_DRY_RUN, CURATOR_CONSOLIDATION})


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """"foreground" for any regular agent (CLI, gateway, cron, subagent); one of the autonomous
    sentinels above for a review/curator fork."""
    return _write_origin.get()


def is_background_review() -> bool:
    """True for ANY autonomous actor (routine self-improvement pass or either curator fork) —
    the existing pinned/external/bundled/hub/user-owned/read-before-write guards apply to all of
    them alike. Use the narrower predicates below when a guard must distinguish which one."""
    return get_current_write_origin() in _AUTONOMOUS_ORIGINS


def is_routine_background_review() -> bool:
    """True only for the routine per-session self-improvement pass — the actor the size-ceiling
    guard exists to restrain."""
    return get_current_write_origin() == BACKGROUND_REVIEW


# Attendedness is orthogonal to origin: an explicit ``/refine`` fork IS a background review (every
# curator / skill-ledger / approval guard keyed on ``is_background_review()`` must still apply), but a
# user asked for it, so the unattended-only memory delete gate (#105921) does not.
_review_attended: contextvars.ContextVar[bool] = contextvars.ContextVar("review_attended", default=False)


def set_review_attended(attended: bool) -> contextvars.Token[bool]:
    return _review_attended.set(bool(attended))


def reset_review_attended(token: contextvars.Token[bool]) -> None:
    _review_attended.reset(token)


def is_unattended_review() -> bool:
    return is_background_review() and not _review_attended.get()


def is_curator_dry_run() -> bool:
    """True only inside a curator preview (report-only) fork — every mutating skill_manage call
    must be mechanically refused here, not merely discouraged by the prompt."""
    return get_current_write_origin() == CURATOR_CONSOLIDATION_DRY_RUN
