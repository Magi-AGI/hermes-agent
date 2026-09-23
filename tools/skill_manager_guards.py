"""Write/delete guards for ``skill_manage``. Every guard returns ``None`` when the
operation may proceed, else a refusal (error dict or message). Origin-owned state
(``_find_skill``, ``_skills_dir``) is reached lazily via ``tools.skill_manager_tool``
so test patches keep working."""

import contextvars as _ctxvars
import logging
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("tools.skill_manager_tool")


def _refusal(message: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _is_background_review() -> bool:
    """True inside the autonomous curator review fork; False on any lookup failure."""
    try:
        from tools.skill_provenance import is_background_review
        return bool(is_background_review())
    except Exception:
        return False


def _resolved_str(path: Path) -> str:
    with suppress(Exception):
        return str(path.resolve())
    return str(path)


class _BackgroundReviewReadMarks:
    """Read marks shared by copied tool contexts within one review run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: set[str] = set()

    def add(self, path: str) -> None:
        with self._lock:
            self._paths.add(path)

    def contains(self, path: str) -> bool:
        with self._lock:
            return path in self._paths


_background_review_read_paths: "_ctxvars.ContextVar[Optional[_BackgroundReviewReadMarks]]" = (
    _ctxvars.ContextVar("background_review_read_paths", default=None))


def mark_background_review_skill_read(path: Path) -> None:
    """Record that the active background-review fork has read a skill file. The fork must not
    patch content it only inferred from the transcript: skill_view/read_file call this, and
    the write guards require the mark."""
    if not _is_background_review():
        return
    if (marks := _background_review_read_paths.get()) is None:
        _background_review_read_paths.set(marks := _BackgroundReviewReadMarks())
    marks.add(_resolved_str(path))


def _background_review_has_read(path: Path) -> bool:
    marks = _background_review_read_paths.get()
    return marks is not None and marks.contains(_resolved_str(path))


def _reset_background_review_read_marks() -> None:
    """Start a fresh, isolated read set for the current review context."""
    _background_review_read_paths.set(_BackgroundReviewReadMarks())


def _resolved_roots(skill_path: Path):
    """``(resolved skill_path, [(root, resolved_root), ...])`` over every resolvable skills root."""
    from agent.skill_utils import get_all_skills_dirs
    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path
    roots = []
    for root in get_all_skills_dirs():
        with suppress(OSError):
            roots.append((root, root.resolve()))
    return resolved, roots


def _containing_skills_root(skill_path: Path) -> Path:
    """Skills root (local or external_dirs) containing ``skill_path``; local dir if none match."""
    from tools import skill_manager_tool as _smt
    resolved, roots = _resolved_roots(skill_path)
    return next((root for root, r in roots if resolved.is_relative_to(r)), _smt._skills_dir())


def _is_path_redirect(path: Path) -> bool:
    """Symlink or (Windows 3.12+) junction — either lets a poisoned tree redirect rmtree outside."""
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _validate_delete_target(skill_dir: Path) -> Optional[str]:
    """Last-line guard before rmtree: even a poisoned tree must never delete (1) a path outside
    every known skills root, (2) a skills root itself, (3) a symlink/junction (rmtree follows it).

    ``_find_skill`` already restricts ``skill_dir`` to a real ``SKILL.md`` parent discovered by walking the
    skills roots, so the agent cannot inject an arbitrary path the way Kilo Code's HTTP endpoint could
    (their issue 11227: a built-in-skill sentinel resolved to the server cwd and a recursive delete wiped
    the user's entire working directory). This is the matching defense-in-depth for our agent-facing
    ``skill_manage`` delete path: even if discovery or a poisoned tree hands us a bad directory, never
    recursively delete See #11227.
    """
    if _is_path_redirect(skill_dir):
        return (f"Refusing to delete '{skill_dir}': the skill directory is a "
                f"symlink/junction. Remove the link target manually if intended.")
    try:
        skill_dir.resolve()
    except OSError as exc:
        return f"Refusing to delete '{skill_dir}': could not resolve path ({exc})."
    resolved, roots = _resolved_roots(skill_dir)
    for _root, root in roots:
        if resolved == root:
            return (f"Refusing to delete '{skill_dir}': resolves to the skills root "
                    f"itself, which would remove every installed skill.")
        if resolved.is_relative_to(root):
            return None
    return f"Refusing to delete '{skill_dir}': path does not resolve inside any known skills root."


def _is_pinned(name: str, what: str) -> Optional[bool]:
    """skill_usage pinned flag; None (logged at debug) when the record is unreadable."""
    try:
        from tools import skill_usage
        return bool(skill_usage.get_record(name).get("pinned"))
    except Exception:
        logger.debug("%s lookup failed for %s", what, name, exc_info=True)
        return None


def _pinned_guard(name: str) -> Optional[str]:
    """Refusal message if *name* is pinned or essential, else None. Pin only guards DELETION;
    patches/edits stay allowed. ESSENTIAL_SKILLS are permanently pinned (the system prompt
    references them). Best-effort: an unreadable sidecar lets the delete through."""
    try:
        from agent.skill_utils import ESSENTIAL_SKILLS
        if name in ESSENTIAL_SKILLS:
            return (
                f"Skill '{name}' is essential to Hermes (the agent's own "
                f"operating manual referenced by the system prompt) and "
                f"cannot be deleted. Patches and edits are still allowed.")
    except Exception:
        logger.debug("essential-guard lookup failed for %s", name, exc_info=True)
    if _is_pinned(name, "pinned-guard"):
        return (
            f"Skill '{name}' is pinned and cannot be deleted by skill_manage. Ask the user to "
            f"run `hermes curator unpin {name}` if they want to delete it. Patches and edits "
            f"are allowed on pinned skills; only deletion is blocked.")
    return None


def _background_review_write_guard(
    name: str, skill_dir: Path, action: str) -> Optional[Dict[str, Any]]:
    """Refuse autonomous curator writes to anything but curator-owned sediment. The review fork
    has no user in the loop, so it is also blocked on pinned/external/bundled/hub skills."""
    if not _is_background_review():
        return None
    refuse = f"Refusing background curator {action} for"
    if _is_pinned(name, "pinned skill guard"):
        return _refusal(
            f"{refuse} pinned skill '{name}': pinned skills "
            f"are off-limits to autonomous maintenance. Ask the user to run `hermes curator "
            f"unpin {name}` if they want it changed.")
    try:
        from agent.skill_utils import is_external_skill_path
        if is_external_skill_path(skill_dir):
            return _refusal(
                f"{refuse} skill '{name}': the skill lives in skills.external_dirs, which are "
                f"externally owned and read-only to autonomous curation.")
    except Exception:
        logger.debug("external skill guard lookup failed for %s", name, exc_info=True)
    try:
        from tools import skill_usage
        for predicate, label in (
            (skill_usage.is_protected_builtin, "protected built-in"),
            (skill_usage.is_hub_installed, "hub-installed"),
            (skill_usage.is_bundled, "bundled")):
            if predicate(name):
                return _refusal(f"{refuse} {label} skill '{name}'.")
        # Not curator-managed (no `created_by: "agent"`) => user-owned. A MISSING
        # record and an explicit `created_by: null` must resolve IDENTICALLY (keying
        # on presence made the policy depend on the guard's own side effect: the
        # first write created a null record, the next identical write was refused).
        usage_rec = skill_usage.load_usage().get(name)
        # Skills that are not curator-managed are off-limits to autonomous curation. This prevents the LLM
        # consolidation pass from mutating skills the user owns (manually authored, URL-installed, or
        # created by a foreground `skill_manage(create)` at the user's request), which lack the `created_by:
        # "agent"` marker. Keying on `isinstance(usage_rec, dict)` made the policy depend on the guard's own
        # side effect: a local skill with no telemetry record passed, the successful write called
        # bump_patch() which created a `created_by: null` record, and the very same write was refused from
        # then on. "Allowed exactly once" is not a policy — it is a race with our own bookkeeping. Fail
        # closed for both shapes; `hermes curator adopt <name>` is the supported way in. See #67140.
        if not skill_usage._is_curator_managed_record(usage_rec):
            _detail = (f"created_by={usage_rec.get('created_by')!r}" if isinstance(usage_rec, dict)
                       else "no usage record")
            return _refusal(
                f"{refuse} skill '{name}': the skill is not "
                f"curator-managed ({_detail}). User-owned skills are off-limits to autonomous "
                f"curation. Run `hermes curator adopt {name}` to opt it in.")
    except Exception:
        logger.warning("owned skill guard lookup failed for %s", name, exc_info=True)
        return _refusal(
            f"{refuse} skill '{name}': agent ownership could not "
            f"be verified because the provenance record is unavailable or unreadable.")
    return None


def _background_review_read_before_write_guard(
    name: str, target: Path, action: str, file_label: str) -> Optional[Dict[str, Any]]:
    """Require review forks to load the exact target before mutating it."""
    if not _is_background_review() or _background_review_has_read(target):
        return None
    return _refusal(
        f"Refusing background curator {action} for skill '{name}': the current {file_label} "
        f"content has not been loaded in this review turn. Call skill_view(name) for SKILL.md, or "
        f"skill_view(name, file_path=...) for a supporting file, then retry the write using the "
        f"content just returned.",
        _read_before_write_required=True)


def _background_review_preflight(action: str, name: str) -> Optional[Dict[str, Any]]:
    if action not in {"edit", "patch", "delete", "write_file", "remove_file"}:
        return None
    from tools import skill_manager_tool as _smt
    existing = _smt._find_skill(name)
    return _background_review_write_guard(name, existing["path"], action) if existing else None


_MUTATING_ACTIONS = frozenset({"create", "edit", "patch", "delete", "write_file", "remove_file"})


def _curator_dry_run_mutation_guard(action: str) -> Optional[Dict[str, Any]]:
    """Mechanically refuse EVERY mutating skill_manage call during a curator preview pass —
    checked before any other dispatch logic, covering `create` too (the narrower
    ``_background_review_preflight`` above does not). The prompt banner tells the model not to
    call these; this is what actually stops it if it tries anyway."""
    from tools.skill_provenance import is_curator_dry_run
    if action not in _MUTATING_ACTIONS or not is_curator_dry_run():
        return None
    return _refusal(
        f"Refusing '{action}' during a curator dry-run preview: this pass is report-only and "
        f"cannot mutate the skill library. Describe the change you would make in the summary "
        f"instead of calling skill_manage with a mutating action.")


def _skill_dir_size_and_ref_count(skill_dir: Path) -> tuple[int, int]:
    """(SKILL.md bytes, references/ file count) for the ceiling check. Missing/unreadable pieces
    count as 0 rather than raising — a measurement failure must not itself block a write. Counts
    recursively: write_file allows nested paths under references/ (e.g. references/sub/x.md), and
    a direct-children-only count would silently exempt anything nested from the ceiling."""
    size_bytes = 0
    with suppress(Exception):
        size_bytes = (skill_dir / "SKILL.md").stat().st_size
    ref_count = 0
    with suppress(Exception):
        ref_count = sum(1 for p in (skill_dir / "references").rglob("*") if p.is_file())
    return size_bytes, ref_count


def _is_new_reference_file(skill_dir: Path, target: Path) -> bool:
    """True when *target* is a not-yet-existing file anywhere under skill_dir/references/ —
    checked against the full relative path, not just the direct parent's name, so a nested
    write_file (references/sub/x.md) is recognized too."""
    with suppress(Exception):
        parts = target.relative_to(skill_dir).parts
        return bool(parts) and parts[0] == "references"
    return False


def _background_review_size_ceiling_guard(
    name: str, skill_dir: Path, target: Path, action: str, content: str,
    original: Optional[str]) -> Optional[Dict[str, Any]]:
    """Block the ROUTINE self-improvement pass (not the curator's own consolidation fork — see
    tools.skill_provenance.is_routine_background_review) from growing an already-oversized skill
    further. Only fires on genuinely additive writes: a brand-new references/ file, or a
    patch/write_file to an existing file that makes it bigger. Shrinking edits and deletions are
    always allowed, even over the ceiling — that's the direction this guard wants to encourage.
    Magi: foreground sessions are bound too (a fix may land, growth may not)."""
    from tools.skill_provenance import is_foreground, is_size_ceiling_bound
    if not is_size_ceiling_bound():
        return None
    is_new_file = original is None
    # Bytes, matching the ceiling's st_size units (character counts diverge on multi-byte text).
    is_growth = is_new_file or (isinstance(content, str) and
                                len(content.encode("utf-8")) > len((original or "").encode("utf-8")))
    if not is_growth:
        return None
    try:
        from agent import curator as _curator
    except Exception:
        logger.debug("curator import failed for size-ceiling guard on %s", name, exc_info=True)
        return None
    size_bytes, ref_count = _skill_dir_size_and_ref_count(skill_dir)
    # A brand-new references/ file (nested or not) adds one to the count actually written to disk
    # after this call succeeds; check against the post-write count so the file that WOULD tip it
    # over is itself refused, not just the one after.
    adds_reference = is_new_file and _is_new_reference_file(skill_dir, target)
    effective_ref_count = ref_count + 1 if adds_reference else ref_count
    max_kb, max_refs = _curator.get_max_skill_size_kb(), _curator.get_max_reference_files()
    # Each dimension binds only the write that grows it: an oversized SKILL.md must not freeze fixes
    # to a small reference file, and a crowded references/ must not freeze edits to existing files.
    # Size is the file's POST-write size (content is the whole new file), so an under-ceiling
    # SKILL.md cannot leap past the ceiling in one write, and no file can grow past it.
    written_bytes = len(content.encode("utf-8")) if isinstance(content, str) else 0
    over_size = written_bytes > max_kb * 1024
    over_refs = adds_reference and effective_ref_count > max_refs
    if not (over_size or over_refs):
        return None
    with suppress(Exception):
        _curator.flag_consolidation_recommended(name, size_bytes=size_bytes, ref_count=effective_ref_count)
    reason = ", ".join(
        r for r, cond in ((f"{target.name} would be {written_bytes / 1024:.1f}KB "
                           f"(ceiling {max_kb}KB)", over_size),
                          (f"references/ has {effective_ref_count} files (ceiling {max_refs})", over_refs)) if cond)
    if is_foreground():
        return _refusal(
            f"Refusing {action} for skill '{name}': {reason}, and this write would grow it further. "
            f"Over-ceiling skills may be corrected or condensed but not enlarged: make the edit "
            f"replace or shorten existing text so the file does not get bigger, or fold the new "
            f"file into an existing reference. Do not work around this with other file tools; if the "
            f"addition is essential, tell the user the skill needs consolidation.")
    return _refusal(
        f"Refusing background review {action} for skill '{name}': {reason}, and this write "
        f"would grow it further. This skill needs consolidation, not another addition — a "
        f"preview will be generated on the next scheduled curator pass "
        f"(`hermes curator run --dry-run --consolidate` to check now — plain `--dry-run` stays "
        f"prune-only unless curator.consolidate: true is set), and `hermes curator run --consolidate` "
        f"applies it. If the lesson is genuinely new and load-bearing, state it plainly in this "
        f"turn's summary instead of writing it to the skill.")


def _curator_consolidation_delete_guard(
    name: str, absorbed_into: Optional[str]) -> Optional[Dict[str, Any]]:
    """Fail closed on unverified deletes during the curator consolidation pass. The fork's only
    legitimate delete is a consolidation declared via ``absorbed_into=<umbrella>`` (existence
    validated in ``_delete_skill``); the deterministic inactivity prune never calls skill_manage,
    so a bare delete here can only be the LLM pass pruning without evidence.

    A delete with no forwarding target — ``absorbed_into`` omitted (``None``) or empty (``""``) — is the
    fail-open behavior reported in #29912: the consolidation pass archived whole clusters of active skills
    with zero verified consolidations (``consolidated_this_run == 0``), leaving active automations pointing
    at names that no longer resolve. Refuse it; keep the skill active.
    """
    if not _is_background_review() or (isinstance(absorbed_into, str) and absorbed_into.strip()):
        return None
    return _refusal(
        f"Refusing background curator delete of skill '{name}': the consolidation pass may only "
        f"archive a skill it has absorbed into an umbrella. Pass absorbed_into=<umbrella> (the "
        f"umbrella must already exist) to record a verified consolidation. Pruning a skill with no "
        f"forwarding target is not permitted here — the deterministic inactivity prune handles "
        f"staleness archival separately. Keeping '{name}' active.",
        _fail_closed=True)


def _is_org_mirror(skill_path: Path) -> bool:
    from agent.skill_utils import is_org_mirror_path
    from tools import skill_manager_tool as _smt
    return is_org_mirror_path(skill_path, _smt._skills_dir())


def _maybe_auto_propose_org_edit(name: str, skill_path: Path) -> Optional[str]:
    """Submit an org-skill edit upstream when `sync.org_auto_propose` is on. Returns a note for
    the tool result or None; never raises (the edit is saved locally and can be proposed later)."""
    try:
        from tools import skills_sync_client as ssc
        if not _is_org_mirror(skill_path):
            return None
        if not ssc.sync_org_auto_propose():
            return (
                f"This skill is shared by your organisation. Your edit is "
                f"saved locally and will not be overwritten by org updates. "
                f"Run `hermes sync propose {name}` to share it back.")
        from tools.skills_sync_client_org import propose_skill
        result = propose_skill(name)
        if result.get("proposal_pending"):
            return (
                f"Auto-proposed to your organisation as proposal "
                f"#{result.get('proposal_id')} (pending admin review).")
        return "Auto-proposed to your organisation (merged into the shared set)."
    except Exception as e:
        logger.debug("auto-propose skipped for %s: %s", name, e)
        return (
            f"Edit saved locally. Could not submit it to your organisation "
            f"right now — run `hermes sync propose {name}` to retry.")


def _org_mirror_write_guard(name: str, skill_path: Path, action: str) -> Optional[Dict[str, Any]]:
    """Org-shared skills are EDITABLE IN PLACE — this only blocks deletion. Edits land in the
    mirror, survive the next org pull (baseline sidecar in skills_sync_client) and reach the org
    via `hermes sync propose`. Deletion stays refused: the mirror is a view of org HEAD, so a
    local delete just comes back, and removing for everyone is an admin action."""
    if action not in {"delete", "remove_file"}:
        return None
    try:
        if _is_org_mirror(skill_path):
            return _refusal(
                f"Cannot {action} '{name}' locally: it is shared by your organisation, so a local "
                f"delete would just come back on the next sync. Ask an org admin to remove it for "
                f"everyone. (Editing it IS allowed — your changes are kept and can be proposed "
                f"back with `hermes sync propose {name}`.)")
    except Exception:
        logger.debug("org mirror guard lookup failed for %s", name, exc_info=True)
    return None
