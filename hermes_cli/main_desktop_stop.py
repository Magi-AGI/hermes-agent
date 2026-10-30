"""``hermes desktop stop`` — stop a wedged desktop app and the backends it owns (gate H3).

The desktop's own quit path is the normal way out; this command is the escape hatch for a UI too
wedged to quit (Lake, 2026-10-19: "I can't use claude to help me close Hermes every time").

Selection is POSITIVE identity only — live-verified machine-spawn-ledger rows and their recorded
spawner lineage — never image-name or argv matching (the bug class behind ~10 fleet-update
issues). Every kill is FAIL-CLOSED (agy R4-1): a PID is signalled only while its identity is
pinned to the ledger's ``create_time`` through a held process handle; an unverifiable identity
(no recorded create_time, a mismatch, or an access error) is reported and skipped, never killed.
No tree kills (agy R4-2): ``taskkill /T`` walks parent PIDs and would take down break-away
children such as a detached ``gateway`` — instead every target is an exact, individually
verified PID: the desktop mains, their serve/dashboard backends, and the helper rows those
backends registered. A desktop update in flight holds the shared update lock; this command
claims it for the duration and refuses to run while an update owns it (agy R4-2).

Signalling is terminate -> grace -> kill with the outcome VERIFIED (agy R4-3); anything that
survives or could not be verified makes the exit code 1 and is named in the output. Known
uncovered modes (agy R4-4, reported, not guessed at): a desktop in Remote/Cloud/SSH or
attach-first mode, or wedged before spawning any backend, leaves no owned ledger rows — the
command says so instead of pretending success.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

_BACKEND_PURPOSES = frozenset({"serve", "dashboard"})
_NEVER_TOUCH_PURPOSES = frozenset({"gateway"})
_WATCHDOG_GRACE_S = 8.0
_TERM_GRACE_S = 5.0

_NO_ROWS_MSG = (
    "No desktop-owned Hermes processes found: the spawn ledger has no desktop-spawned "
    "serve/dashboard backends on this install.\n"
    "Not covered by this command (no ledger lineage to verify): a desktop in Remote/Cloud/SSH "
    "or attach-first mode, or one wedged before it spawned a backend — close that window from "
    "the OS instead. The gateway, relay dashboard and detached jobs are never touched."
)


@dataclass
class DesktopStopPlan:
    """What a stop run may touch, split by whether identity can be verified."""

    mains: list[tuple[int, float]] = field(default_factory=list)
    unverified_mains: list[int] = field(default_factory=list)
    backends: list[dict] = field(default_factory=list)
    helpers: list[dict] = field(default_factory=list)


def desktop_stop_plan(entries: list[dict]) -> DesktopStopPlan:
    """Select ONLY desktop-owned rows from live-verified ledger entries.

    A backend qualifies when its purpose is serve/dashboard AND a spawner was recorded (the
    Electron parent-identity env); the mains are the distinct recorded spawners — but a main
    whose ``spawner_create`` was never recorded cannot be told from a recycled PID and lands in
    ``unverified_mains`` (reported, never killed; agy R4-1). Helper rows registered BY a selected
    backend (kernel/MCP children) are included by the same exact-PID standard. A gateway row
    never qualifies in any role, and a dashboard with no spawner (the relay) stays untouched.
    """
    plan = DesktopStopPlan()
    seen: set[int] = set()
    for entry in entries:
        if entry.get("purpose") not in _BACKEND_PURPOSES:
            continue
        spawner = entry.get("spawner_pid")
        if not isinstance(spawner, int) or spawner <= 0:
            continue
        plan.backends.append(entry)
        if spawner in seen:
            continue
        seen.add(spawner)
        create = entry.get("spawner_create")
        if isinstance(create, (int, float)) and create > 0:
            plan.mains.append((spawner, float(create)))
        else:
            plan.unverified_mains.append(spawner)
    backend_pids = {int(e["pid"]) for e in plan.backends}
    for entry in entries:
        if entry.get("purpose") in _BACKEND_PURPOSES or entry.get("purpose") in _NEVER_TOUCH_PURPOSES:
            continue
        if entry.get("spawner_pid") in backend_pids:
            plan.helpers.append(entry)
    return plan


def _parent_linkage(main_pid: int, backend_rows: list[dict]) -> Optional[float]:
    """Positive identity for a main with no recorded start time (agy R5-4: every POSIX spawn).

    A LIVE, ledger-verified backend whose CURRENT parent pid equals the recorded spawner proves
    that parent is the process that spawned it — a recycled PID cannot re-acquire a living
    child, and a dead parent reparents the child away. Returns the verified parent's OWN
    create_time, captured while the linkage held, so the later kill is pinned against THIS
    incarnation (a kill-time self-comparison would accept a recycled PID — agy round 6,
    R5-4a). ``None`` = unverified; any access failure reads as unverified.
    """
    try:
        import psutil
    except Exception:
        return None
    for row in backend_rows:
        try:
            create = row.get("create_time")
            if create is None:
                # A row without a recorded start time cannot verify anything (fail closed).
                continue
            parent_create = psutil.Process(main_pid).create_time()
            proc = psutil.Process(int(row["pid"]))
            if abs(proc.create_time() - float(create)) >= 1.0:
                continue
            # Windows keeps a child's ORIGINAL parent pid after that parent exits, so a bare
            # ppid match also accepts a newcomer on the recycled pid (Codex HS-C1). The real
            # spawner must PREDATE the child it spawned — the same ordering check psutil's own
            # Process.parent() applies — and the child must still name it as parent AFTER we
            # took the parent's create_time, so that create_time belongs to the spawning
            # incarnation.
            if proc.ppid() == main_pid and parent_create < float(create):
                return parent_create
        except Exception:
            continue
    return None


def _stop_pinned(pid: int, create_time: Optional[float], *,
                 grace_seconds: float = _TERM_GRACE_S,
                 sleep: Callable[[float], None] = time.sleep) -> str:
    """Terminate -> grace -> kill ONE process whose identity stays pinned for the whole exchange.

    Returns ``"stopped"``, ``"gone"`` (already dead), ``"survived"`` (outlived SIGKILL within the
    grace), or ``"unverifiable"`` (identity could not be pinned to ``create_time`` — nothing was
    signalled). The psutil handle is taken ONCE and its own cached create_time compared against
    the ledger's, so a PID recycled between check and signal reads as a different process.
    """
    try:
        import psutil
    except Exception:
        return "unverifiable"
    try:
        proc = psutil.Process(pid)
        if create_time is None or abs(proc.create_time() - float(create_time)) >= 1.0:
            return "unverifiable"
        proc.terminate()
        try:
            proc.wait(timeout=max(0.1, grace_seconds))
            return "stopped"
        except psutil.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=max(0.1, grace_seconds))
                return "stopped"
            except psutil.TimeoutExpired:
                return "survived"
    except psutil.NoSuchProcess:
        return "gone"
    except Exception:
        return "unverifiable"


def cmd_desktop_stop(
    *,
    entries_fn: Optional[Callable[[], list[dict]]] = None,
    stop_pinned: Optional[Callable[..., str]] = None,
    parent_linkage: Optional[Callable[[int, list[dict]], Optional[float]]] = None,
    update_lock_factory: Optional[Callable[[], object]] = None,
    grace_seconds: float = _WATCHDOG_GRACE_S,
    sleep: Callable[[float], None] = time.sleep,
    out=None,
) -> int:
    """Stop the desktop mains and every backend/helper they own. Returns a shell exit code:
    0 = everything owned was stopped; 1 = something was skipped as unverifiable or nothing
    was owned at all (stopping nothing is not success), or a survivor remained, or
    an update holds the lock."""
    from hermes_cli.process_identity import ledger_entries
    from hermes_cli.update_lock import UpdateLock, describe_holder

    entries_fn = entries_fn or ledger_entries
    stop_pinned = stop_pinned or _stop_pinned
    parent_linkage = parent_linkage or _parent_linkage
    out = out or sys.stdout

    # Hold the shared update lock for the whole stop: an in-flight desktop update's handoff
    # would relaunch what this command stops, and killing its staging mid-swap corrupts the
    # install (agy R4-2). An update in progress wins; the user retries after it finishes.
    # The marker lives at the MACHINE root — the path the updater actually holds — never a
    # profile home a sticky active_profile would resolve (agy R5-5).
    if update_lock_factory is not None:
        lock = update_lock_factory()
    else:
        from hermes_constants import get_default_hermes_root
        from hermes_cli.update_lock import MARKER_NAME
        lock = UpdateLock(path=get_default_hermes_root() / MARKER_NAME)
    if not lock.acquire():
        holder = getattr(lock, "holder", None)
        print("A Hermes update is in progress; refusing to stop the desktop under it.", file=out)
        if holder is not None:
            print(describe_holder(holder), file=out)
        return 1
    try:
        plan = desktop_stop_plan(entries_fn())
        if not plan.mains and not plan.unverified_mains and not plan.backends:
            print(_NO_ROWS_MSG, file=out)
            # Stopping nothing is not success: the command exists to stop a wedged desktop,
            # and scripts branch on this (agy R5-6).
            return 1
        problems = 0
        dead_mains: set[int] = set()
        # A main whose start time was never recorded (every POSIX spawn, agy R5-4) gets a
        # second chance: live parent linkage from one of its ledger-verified backends.
        mains = list(plan.mains)
        still_unverified: list[int] = []
        for pid in plan.unverified_mains:
            owned = [e for e in plan.backends if e.get("spawner_pid") == pid]
            linked_create = parent_linkage(pid, owned)
            if linked_create is not None:
                mains.append((pid, float(linked_create)))
            else:
                still_unverified.append(pid)
        # Mains first: a live main respawns its backends. Electron's children (renderers/GPU)
        # exit when the browser process dies (their mojo pipes break); they are not in the
        # ledger and are never signalled directly.
        for pid, create in mains:
            outcome = stop_pinned(pid, create, sleep=sleep)
            print(f"desktop main {pid}: {outcome}", file=out)
            if outcome in ("survived", "unverifiable"):
                problems += 1
            else:
                dead_mains.add(pid)
        skip_backend_pids: set[int] = set()
        # Dependents are touched only under a main that is provably gone: a surviving or
        # kill-time-unverifiable main is still there to respawn them (Codex HS-C2), exactly
        # like a never-verified one.
        live_main_pids = ({pid for pid, _ in mains} - dead_mains) | set(still_unverified)
        for pid in still_unverified:
            print(f"desktop main {pid}: SKIPPED — its identity could not be verified (no "
                  f"recorded start time and no live backend names it as parent). Close that "
                  f"window from the OS. Its backends are left alone: killing them under a live "
                  f"main only triggers a respawn.", file=out)
            problems += 1
        for pid in sorted(live_main_pids):
            skip_backend_pids |= {int(e["pid"]) for e in plan.backends
                                  if e.get("spawner_pid") == pid}
        # The backends' parent-death watchdog should now self-exit; give it the grace it polls on,
        # re-reading live-verified rows rather than trusting the pre-kill snapshot.
        deadline = time.monotonic() + max(0.0, grace_seconds)
        targets = [e for e in plan.backends + plan.helpers
                   if int(e["pid"]) not in skip_backend_pids
                   # A helper of a backend we left alive must not be killed out from under it
                   # (agy round 6, R5-4b).
                   and e.get("spawner_pid") not in skip_backend_pids]
        remaining = {int(e["pid"]) for e in targets}
        pending = list(targets)
        while pending and time.monotonic() < deadline:
            sleep(0.5)
            live = {int(e["pid"]): e for e in entries_fn() if int(e.get("pid") or 0) in remaining}
            pending = [e for e in pending if int(e["pid"]) in live]
        for entry in pending:
            pid = int(entry["pid"])
            outcome = stop_pinned(pid, entry.get("create_time"), sleep=sleep)
            print(f"{entry.get('purpose')} {pid} (profile={entry.get('profile') or '-'}): "
                  f"did not self-exit; {outcome}", file=out)
            problems += outcome in ("survived", "unverifiable")
        summary = (f"Done: {len(mains)} desktop main(s), {len(targets)} backend(s)/helper(s) "
                   f"addressed; {len(targets) - len(pending)} self-exited in the grace window. "
                   f"Gateway, relay dashboard and detached jobs untouched.")
        if problems:
            summary += f" {problems} process(es) were skipped or survived — see above."
        print(summary, file=out)
        return 1 if problems else 0
    finally:
        lock.release()
