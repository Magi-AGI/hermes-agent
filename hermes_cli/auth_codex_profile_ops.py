"""Codex ownership ops of profile lifecycle and transfers (design rev 9 §4 RULE-P1..P4, RULE-M3;
credential recovery protocol stage 2).

* **Profile rename** (a lifecycle op, RULE-P2): migration lock -> the lifecycle locks of every
  profile involved (sorted) -> store locks for the snapshot; PAYLOAD (source / destination
  directories, every affected chain's entry BEFORE the op) -> DECISION (chain owners and row stores
  rewritten to the new store, epochs +1, ``lifecycle = {op_id}``; ``state`` / ``fence`` untouched)
  -> quiesce (store locks released; the caller closes its in-directory handles) -> phase
  ``moving`` -> the directory move -> phase ``moved`` -> APPLY (chain records' epochs at their new
  place) -> COMMIT (``lifecycle`` cleared, op removed). A move that fails leaves the op
  ``lifecycle-stuck`` (terminal for automatic dispatch, RULE-P3); ``hermes auth recover --op``
  resumes it, ``--abandon`` compensates it (RULE-P4: PAYLOAD-2, then ONE atomic DECISION-2 inside the
  same op entry with fixed epochs, APPLY-2, COMMIT-2). A rename interrupted BEFORE its move (the
  caller's quiesce failed) is abandoned the same way at once, so no later dispatch moves a directory
  the user was told was not renamed.
* **Profile delete**: an ordinary op (nothing moves): the profile's rows leave their chains, chains
  it owns are retired (records archived, token material kept in the retired-token index first),
  chains owned elsewhere lose those rows at a new epoch. The caller removes the directory while
  still holding the migration lock and the profile's lifecycle lock, so no stale writer recreates it.
* **Transfer** (RULE-M3): a chain, its owner rows and its record (attempt, fence, full history) move
  to another store; ``state`` / ``fence`` are copied unchanged, only ``owner`` changes, epoch +1.
* A store whose ``auth.json`` does not live inside the moved directory (a link to root's, a
  junction profile) changes no store key: renaming such a profile moves no Codex state.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from hermes_cli import auth_codex_rows as rows_mod
from hermes_cli.auth_codex_lifecycle import (
    ABANDON, FORWARD, continuation, lifecycle_locks, lock_name, op_direction, profile_of_store)
from hermes_cli.auth_codex_ops import (
    ABANDON_SUFFIX, Payload, _read_payload, apply_payload, commit_op,
    decide_op, refuse_conflicts)
from hermes_cli.auth_codex_registry import (
    OP_RUNNING, OP_STUCK, OP_UNRECOVERABLE, PROVIDER, SINGLETON, STATE_RETIRED, CodexAuthorityRefused,
    CodexRegistryUnreadable, RegistryView, migration_lock, ops_dir, read_registry,
    store_locks, store_path_of, thread_holds_store_lock, utc_now, write_registry)
from hermes_cli.auth_credential_io import (
    LockAcquisitionTimeout, canonical_path, encode_json, invalidate_path_memo, path_memo, read_witnessed_bytes,
    require_committed, sha256_hex, store_key, write_credential_file)

logger = logging.getLogger("hermes_cli.auth")

RENAME, DELETE, TRANSFER = "profile-rename", "profile-delete", "transfer"
LIFECYCLE_KINDS = frozenset({RENAME})
PHASE_MOVING, PHASE_MOVED, PHASE_ABANDONING = "moving", "moved", "abandoning"
# A dispatcher pass waits at most this long for a profile's lifecycle lock (a writer inside the
# profile holds it briefly); busy means the op stays for the next pass.
RECOVERY_LOCK_BUDGET_SECONDS = 5.0


class CodexProfileMoveStuck(CodexAuthorityRefused):
    """The profile directory could not be moved: the op is ``lifecycle-stuck`` (RULE-P3)."""

    def __init__(self, op_id: str, profile: str, detail: str) -> None:
        super().__init__(
            f"Hermes could not move the profile '{profile}' ({detail}); its Codex login is paused until "
            f"that is resolved. Close whatever holds the profile open (a running gateway, an editor, a "
            f"terminal in that folder), then run `hermes auth recover --op {op_id[:8]}` to finish the "
            f"move, or `hermes auth recover --op {op_id[:8]} --abandon` to cancel it.")
        self.op_id = op_id


# ── Where a store lives ──────────────────────────────────────────────────────────────────────

def _abs(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def store_key_after_move(old_dir: Path, new_dir: Path) -> Optional[Tuple[str, str]]:
    """``(old key, new key)`` of the store that moving the directory entry *old_dir* to *new_dir*
    relocates, or None when it relocates none: the entry is a link (symlink / junction — the data
    stays where it is), or ``auth.json`` resolves outside the directory (a link to root's store)."""
    old_abs = _abs(old_dir)
    real_dir = canonical_path(old_abs)
    if os.path.normcase(str(real_dir)) != os.path.normcase(str(canonical_path(old_abs.parent) / old_abs.name)):
        return None
    old_key = store_key(old_abs / "auth.json")
    rel = os.path.relpath(old_key, os.path.normcase(str(real_dir)))
    if os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    new_abs = _abs(new_dir)
    new_key = os.path.normcase(os.path.join(str(canonical_path(new_abs.parent)), new_abs.name, rel))
    return old_key, new_key


def _exists(path: Path) -> Optional[bool]:
    """True / False, or None when it cannot be told (fail closed: never treated as absent)."""
    try:
        os.lstat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def directory_location(source: Path, dest: Path) -> str:
    """``source`` | ``dest`` (exactly one exists) | ``both`` | ``neither`` | ``unknown``."""
    src, dst = _exists(source), _exists(dest)
    if src is None or dst is None:
        return "unknown"
    return {(True, False): "source", (False, True): "dest", (True, True): "both"}.get((src, dst), "neither")


def _chains_touching_store(view: RegistryView, key: str) -> Dict[str, Dict[str, Any]]:
    return {cid: entry for cid, entry in view.chains.items()
            if entry.get("owner") == key or any(r.get("store") == key for r in entry.get("rows", ()))}


def _profiles_of_keys(keys: Iterable[str]) -> List[str]:
    out = set()
    for key in keys:
        name = profile_of_store(Path(key))
        if name:
            out.add(name)
    return sorted(out)


def _stores_of(chains: Dict[str, Dict[str, Any]]) -> List[str]:
    keys = set()
    for entry in chains.values():
        keys.add(entry["owner"])
        keys.update(r["store"] for r in entry.get("rows", ()))
    return sorted(keys)


# ── Op entry helpers ─────────────────────────────────────────────────────────────────────────

def _update_op(op_id: str, **fields: Any) -> None:
    """One registry write changing fields of ``ops[op_id]`` (caller holds the migration lock)."""
    view = read_registry()
    if not view.ok or op_id not in view.ops:
        raise CodexAuthorityRefused(f"Codex login update {op_id[:8]} is no longer unfinished.")
    data = json.loads(json.dumps(view.data))
    data["ops"][op_id].update(fields)
    write_registry(data)


def _mark_stuck(op_id: str, detail: str) -> None:
    _update_op(op_id, status=OP_STUCK, stuck_reason=detail[:300], stuck_at=utc_now())
    logger.warning("Codex profile op %s is lifecycle-stuck: %s", op_id, detail)


def _holds_codex_material(store: Path) -> bool:
    """The store has a Codex row or a legacy rescue file beside it (anything that cannot be READ
    counts as present: the dispatcher then decides, failing closed)."""
    from hermes_cli.auth import _load_auth_store
    from hermes_cli.auth_codex_registry import legacy_rescue_path
    if _exists(legacy_rescue_path(store)) is not False:
        return True
    present = _exists(store)
    if present is None:
        return True
    try:
        return bool(rows_mod.codex_rows(_load_auth_store(store))) if present else False
    except OSError:
        return True


def _prepared_view(action: str, store: Path) -> RegistryView:
    """Under the migration lock: the dispatcher pass (roll forward unfinished ops, bootstrap an ABSENT
    registry when Codex material exists, fence + archive new legacy rescues), then the registry. With
    NO registry and no Codex material in *store* there is nothing to govern: no dispatch, no writes.
    An UNREADABLE registry refuses (who owns what cannot be known, RULE-G3)."""
    from hermes_cli.auth_codex_authority import dispatch_locked
    view = read_registry()
    if view.status == "absent" and not _holds_codex_material(store):
        return view
    if view.status == "unreadable" or dispatch_locked() == "unreadable":
        raise CodexAuthorityRefused(
            f"The Codex login registry is damaged, so {action} can't tell which Codex logins it would "
            "change. Run `hermes auth migrate --rebuild` (every existing Codex login stays paused until "
            "you sign in again), then try again.")
    return read_registry()


# ── Rename (RULE-P2) ─────────────────────────────────────────────────────────────────────────

def plan_rename(view: RegistryView, old_key: str, new_key: str, reserved: List[str], lock_profiles: List[str],
                old_dir: Path, new_dir: Path, affected: Dict[str, Dict[str, Any]]) -> Payload:
    """*reserved*: the source and destination names (RULE-G4); *lock_profiles*: every profile whose
    lifecycle lock the op and its recovery take up front, sorted (RULE-L1), incl. other stores of the
    moving chains."""
    payload = Payload(kind=RENAME, scope_chains=sorted(affected), scope_stores=sorted({old_key, new_key}),
                      scope_profiles=reserved)

    def moved(key: str) -> str:
        return new_key if key == old_key else key

    for cid, entry in affected.items():
        final = {**entry, "owner": moved(entry["owner"]),
                 "rows": [{**r, "store": moved(r["store"])} for r in entry.get("rows", ())]}
        if entry.get("state") != STATE_RETIRED:  # a retired chain's leftover rows only follow the path
            final.update(epoch=entry["epoch"] + 1, lifecycle={"op_id": payload.op_id})
            payload.records.append({"action": "set_epoch", "owner": final["owner"], "chain_id": cid,
                                    "epoch": final["epoch"]})
        payload.chains[cid] = final
    payload.extra = {"source_dir": str(_abs(old_dir)), "dest_dir": str(_abs(new_dir)),
                     "source_key": old_key, "dest_key": new_key, "profiles": lock_profiles,
                     "before": json.loads(json.dumps(affected))}
    return payload


class ProfileRename:
    """Handed to the profile rename: ``move(fn)`` runs the directory move inside the op."""

    def __init__(self, op_id: Optional[str], body: Optional[Dict[str, Any]], old_dir: Path, new_dir: Path,
                 profile: str) -> None:
        self.op_id, self.body = op_id, body
        self.old_dir, self.new_dir, self.profile = old_dir, new_dir, profile
        self.moved = False

    def move(self, fn: Callable[[], Any]) -> None:
        if self.op_id is None:
            fn()
            invalidate_path_memo()
            self.moved = True
            return
        _update_op(self.op_id, phase=PHASE_MOVING)
        invalidate_path_memo()
        try:
            _move_with_budget(fn)
        except BaseException as exc:
            invalidate_path_memo()
            if directory_location(self.old_dir, self.new_dir) != "dest":
                _mark_stuck(self.op_id, f"{type(exc).__name__}: {exc}")
                self.moved = True  # nothing more to undo here: the op is stuck, not abandoned
                if isinstance(exc, Exception):
                    raise CodexProfileMoveStuck(self.op_id, self.profile, str(exc)) from exc
                raise
        invalidate_path_memo()
        self.moved = True
        _finish_forward(self.op_id, self.body)


def _move_with_budget(fn: Callable[[], Any]) -> None:
    """The directory move. A Windows sharing violation from another process's momentary handle (a
    lock-free reader of auth.json opens it for microseconds) is retried within the strict writer's
    contended-rename budget (6 attempts, jittered 20..100 ms); anything else, or contention that
    outlasts the budget, fails at once and the op becomes lifecycle-stuck (RULE-P2)."""
    from hermes_cli.auth_credential_io import retry_contended
    retry_contended(fn)


def _finish_forward(op_id: str, body: Dict[str, Any]) -> None:
    """The directory is at its destination: phase ``moved``, APPLY, COMMIT (idempotent)."""
    _update_op(op_id, phase=PHASE_MOVED)
    apply_payload(body)
    commit_op(op_id)


@contextmanager
def profile_rename(old_name: str, new_name: str, old_dir: Path, new_dir: Path) -> Iterator[ProfileRename]:
    """The Codex side of ``hermes profile rename``: yields a :class:`ProfileRename` whose ``move`` the
    caller runs after closing its in-directory handles. Everything happens under the migration lock
    and the lifecycle locks of the source, destination and every other profile a moving chain
    touches; conflicting unfinished ops refuse it (RULE-G4)."""
    if thread_holds_store_lock():
        raise RuntimeError("a profile rename must not run inside an auth-store or lifecycle lock")
    action = f"renaming the profile '{old_name}'"
    with migration_lock(), path_memo():
        view = _prepared_view(action, Path(old_dir) / "auth.json")
        keys = store_key_after_move(old_dir, new_dir)
        affected = _chains_touching_store(view, keys[0]) if keys else {}
        profiles = sorted({lock_name(old_name), lock_name(new_name), *_profiles_of_keys(_stores_of(affected))})
        with lifecycle_locks(profiles):
            if _exists(new_dir) is not False:
                raise FileExistsError(f"Profile '{new_name}' already exists at {new_dir}.")
            refuse_conflicts(view, sorted(affected), list(keys or ()), [old_name, new_name], action)
            live = {cid: e for cid, e in affected.items() if e.get("state") != STATE_RETIRED}
            if not affected:
                handle = ProfileRename(None, None, old_dir, new_dir, old_name)
                yield handle
                return
            with store_locks([store_path_of(k) for k in _stores_of(affected) if k != keys[0]] + [old_dir / "auth.json"]):
                payload = plan_rename(view, keys[0], keys[1], sorted({lock_name(old_name), lock_name(new_name)}),
                                      profiles, old_dir, new_dir, affected)
                body = decide_op(payload, view)
            logger.info("Codex: renaming profile %s -> %s moves %d login(s) (op %s)",
                        old_name, new_name, len(live), payload.op_id)
            handle = ProfileRename(payload.op_id, body, old_dir, new_dir, old_name)
            with continuation(payload.op_id, FORWARD):
                try:
                    yield handle
                except BaseException as exc:
                    if not handle.moved and not _abandon_unmoved(payload.op_id, old_name) and isinstance(exc, Exception):
                        raise CodexProfileMoveStuck(payload.op_id, old_name, f"interrupted before the move: {exc}") from exc
                    raise
                if not handle.moved and not _abandon_unmoved(payload.op_id, old_name):
                    raise CodexProfileMoveStuck(payload.op_id, old_name, "the rename stopped before the move")


def _abandon_unmoved(op_id: str, profile: str) -> bool:
    """The rename stopped before its move: compensate now (RULE-P4) so no later dispatch moves the
    directory. If even that fails the op is left stuck (terminal, named to the user): False."""
    try:
        _abandon_locked(op_id)
        return True
    except Exception as exc:
        logger.warning("Codex rename op %s could not be cancelled; left for `hermes auth recover`", op_id,
                       exc_info=True)
        try:
            if op_id in read_registry().ops:
                _mark_stuck(op_id, f"the rename stopped before the move and could not be cancelled: {exc}")
        except Exception:
            logger.warning("Codex rename op %s could not be marked stuck", op_id, exc_info=True)
        return False


# ── Recovery (M1 for lifecycle ops), resume, abandonment (RULE-P4) ───────────────────────────

def recover_lifecycle_op(op_id: str, op: Dict[str, Any], body: Dict[str, Any], *,
                         lock_budget: float = RECOVERY_LOCK_BUDGET_SECONDS) -> str:
    """Roll a running lifecycle op to its end in its recorded direction (caller holds the migration
    lock): ``rolled_forward`` | ``abandoned`` | ``stuck`` | ``unrecoverable`` | ``busy``."""
    profiles = (body.get("extra") or {}).get("profiles") or op.get("profiles") or []
    try:
        with lifecycle_locks(profiles, lock_budget):
            direction = op_direction(op)
            with continuation(op_id, direction):
                if direction == ABANDON:
                    return _complete_abandon(op_id, read_registry().ops[op_id])
                return _roll_forward(op_id, read_registry().ops[op_id], body)
    except LockAcquisitionTimeout:
        return "busy"


def _roll_forward(op_id: str, op: Dict[str, Any], body: Dict[str, Any]) -> str:
    extra = body.get("extra") or {}
    source, dest = Path(extra["source_dir"]), Path(extra["dest_dir"])
    location = directory_location(source, dest)
    if location == "source" and op.get("phase") != PHASE_MOVED:
        _update_op(op_id, phase=PHASE_MOVING)
        invalidate_path_memo()
        try:
            _move_with_budget(lambda: os.rename(source, dest))
        except OSError as exc:
            invalidate_path_memo()
            if directory_location(source, dest) != "dest":
                _mark_stuck(op_id, f"{type(exc).__name__}: {exc}")
                return "stuck"
        invalidate_path_memo()
    elif location != "dest":
        _mark_stuck(op_id, f"the profile directory is not where the move expects it ({location})")
        return "stuck"
    _finish_forward(op_id, body)
    return "rolled_forward"


def _abandon_path(op_id: str) -> Path:
    return ops_dir() / f"{op_id}{ABANDON_SUFFIX}.json"


def _abandon_locked(op_id: str) -> str:
    """RULE-P4 (caller holds the migration lock). Precondition, checked under the lifecycle locks:
    the op runs forward and its directory has NOT moved."""
    view = read_registry()
    op = view.ops.get(op_id) if view.ok else None
    if op is None:
        raise CodexAuthorityRefused(f"Codex login update {op_id[:8]} is not unfinished.")
    if op.get("kind") not in LIFECYCLE_KINDS:
        raise CodexAuthorityRefused(f"Codex login update {op_id[:8]} ({op.get('kind')}) can't be abandoned; "
                                    f"`hermes auth recover --op {op_id[:8]}` completes it.")
    body = _read_payload(op_id, op)
    if body is None:
        raise CodexAuthorityRefused(
            f"The record of Codex login update {op_id[:8]} can't be read, so it can't be cancelled; "
            f"`hermes auth recovery resolve {op_id[:8]}` releases it (its logins stay paused).")
    extra = body.get("extra") or {}
    with lifecycle_locks(extra.get("profiles") or op.get("profiles") or []):
        if op_direction(op) == ABANDON:  # DECISION-2 already made: only APPLY-2 / COMMIT-2 remain
            with continuation(op_id, ABANDON):
                return _complete_abandon(op_id, op)
        with continuation(op_id, FORWARD):
            location = directory_location(Path(extra["source_dir"]), Path(extra["dest_dir"]))
            if location != "source":
                raise CodexAuthorityRefused(
                    f"The profile directory has already moved ({location}), so the rename can't be cancelled; "
                    f"`hermes auth recover --op {op_id[:8]}` finishes it.")
            current = view.chains
            before = extra.get("before") or {}
            epochs = {cid: current[cid]["epoch"] + 1 for cid, entry in before.items()
                      if cid in current and entry.get("state") != STATE_RETIRED}
            compensation = {
                "op_id": op_id, "kind": f"{op.get('kind')}:abandon", "created_at": utc_now(),
                "before": before, "abandon_epochs": epochs,
                "records": [{"action": "set_epoch", "owner": before[cid]["owner"], "chain_id": cid,
                             "epoch": epoch} for cid, epoch in sorted(epochs.items())]}
            raw = encode_json(compensation)
            # 1. PAYLOAD-2
            require_committed(write_credential_file(_abandon_path(op_id), raw), f"compensation payload {op_id}")
            # 2. DECISION-2: ONE registry write — chains back to the source bindings at the fixed
            # epochs (state / fence as they are now; lifecycle still set) and the same op entry
            # turned runnable in the abandon direction.
            data = json.loads(json.dumps(read_registry().data))
            for cid, entry in before.items():
                if cid not in data["chains"]:
                    continue
                now = data["chains"][cid]
                restored = {**entry, "state": now.get("state"), "fence": now.get("fence")}
                if cid in epochs:
                    restored.update(epoch=epochs[cid], lifecycle={"op_id": op_id})
                data["chains"][cid] = restored
            data["ops"][op_id].update(
                direction=ABANDON, status=OP_RUNNING, phase=PHASE_ABANDONING,
                abandon_payload_path=_abandon_path(op_id).name, abandon_payload_sha=sha256_hex(raw),
                abandon_epochs=epochs)
            data["ops"][op_id].pop("stuck_reason", None)
            write_registry(data)
        with continuation(op_id, ABANDON):
            return _complete_abandon(op_id, read_registry().ops[op_id])


def _complete_abandon(op_id: str, op: Dict[str, Any]) -> str:
    """APPLY-2 + COMMIT-2 from the RECORDED compensation (never a new one, never new epochs)."""
    try:
        raw, sha = read_witnessed_bytes(_abandon_path(op_id))
        compensation = json.loads(raw.decode("utf-8")) if raw is not None and sha == op.get("abandon_payload_sha") else None
    except Exception:
        compensation = None
    if compensation is None or compensation.get("abandon_epochs") != op.get("abandon_epochs"):
        from hermes_cli.auth_codex_ops import _fence_unrecoverable
        if op.get("status") != OP_UNRECOVERABLE:
            _fence_unrecoverable(op_id, op)
        return "unrecoverable"
    apply_payload({"edits": {}, "records": compensation["records"]})
    commit_op(op_id, extra_archives=[_abandon_path(op_id)])
    return "abandoned"


def _find_op(view: RegistryView, prefix: str) -> str:
    matches = [k for k in view.ops if prefix and k.startswith(prefix.strip())]
    if len(matches) != 1:
        raise CodexAuthorityRefused(
            f"No single unfinished Codex login update matches {prefix!r}; `hermes auth recovery list` shows them.")
    return matches[0]


def recover_op(prefix: str, *, abandon: bool = False) -> str:
    """``hermes auth recover --op <id> [--abandon]``: resume one unfinished op (a ``lifecycle-stuck``
    one included) from its recorded phase and direction, or abandon a lifecycle op whose directory
    has not moved (RULE-P3 / P4)."""
    if thread_holds_store_lock():
        raise RuntimeError("recovery must not run inside an auth-store or lifecycle lock")
    with migration_lock(), path_memo():
        view = read_registry()
        if not view.ok:
            raise CodexRegistryUnreadable() if view.status == "unreadable" else CodexAuthorityRefused(
                "There is no unfinished Codex login update.")
        op_id = _find_op(view, prefix)
        op = view.ops[op_id]
        if op.get("status") == OP_UNRECOVERABLE:
            raise CodexAuthorityRefused(
                f"The record of Codex login update {op_id[:8]} can't be read; `hermes auth recovery resolve "
                f"{op_id[:8]}` completes it if it reads again, or releases it (its logins stay paused).")
        if abandon:
            return _abandon_locked(op_id)
        if op.get("status") == OP_STUCK:
            _update_op(op_id, status=OP_RUNNING)
            op = read_registry().ops[op_id]
        from hermes_cli.auth_codex_ops import roll_forward_one
        outcome = roll_forward_one(op_id, op)
        if outcome == "stuck":
            entry = read_registry().ops.get(op_id) or {}
            raise CodexProfileMoveStuck(op_id, ", ".join(entry.get("profiles") or ()),
                                        entry.get("stuck_reason") or "the move failed again")
        if outcome == "busy":
            raise CodexAuthorityRefused(f"Another Hermes process is using that profile; try "
                                        f"`hermes auth recover --op {op_id[:8]}` again in a moment.")
        return outcome


# ── Delete ───────────────────────────────────────────────────────────────────────────────────

def plan_delete(view: RegistryView, snaps: Dict[str, Any], key: str, profile: str) -> Payload:
    """Every row in the deleted store leaves its chain; chains the store owns are retired (record
    archived after its material is indexed); chains owned elsewhere keep their other rows at a new
    epoch (RULE-A3)."""
    from hermes_cli.auth_codex_plans import _chain_row_material, plan_removal
    from hermes_cli.auth_codex_ops import retire_entry
    registered = sorted({r["row"] for e in view.chains.values() for r in e.get("rows", ()) if r["store"] == key})
    payload = plan_removal(view, snaps, key, registered) if key in snaps else Payload(
        kind=DELETE, scope_chains=[], scope_stores=[key])
    payload.kind = DELETE
    payload.scope_profiles = [profile]
    for cid, entry in view.chains.items():  # an owned chain without rows here is retired too
        if entry.get("owner") == key and entry.get("state") != STATE_RETIRED and cid not in payload.chains:
            payload.chains[cid] = retire_entry(entry, [r for r in entry["rows"] if r["store"] != key])
            payload.records.append({"action": "archive", "owner": key, "chain_id": cid, "reason": "profile-deleted",
                                    "rows": _chain_row_material(entry, snaps)})
            payload.scope_chains.append(cid)
    for action in payload.records:
        if action.get("action") == "archive":
            action["reason"] = "profile-deleted"
    for cid, entry in view.chains.items():  # a retired chain's leftover rows here go with the store
        if entry.get("state") == STATE_RETIRED and cid not in payload.chains and any(
                r["store"] == key for r in entry.get("rows", ())):
            payload.chains[cid] = {**entry, "rows": [r for r in entry["rows"] if r["store"] != key]}
    return payload


@contextmanager
def profile_delete(name: str, profile_dir: Path) -> Iterator[Dict[str, Any]]:
    """The Codex side of ``hermes profile delete``, run BEFORE the directory is removed and held
    (migration lock + the profile's lifecycle lock) until the caller finished removing it."""
    if thread_holds_store_lock():
        raise RuntimeError("a profile delete must not run inside an auth-store or lifecycle lock")
    action = f"deleting the profile '{name}'"
    with migration_lock(), path_memo():
        view = _prepared_view(action, Path(profile_dir) / "auth.json")
        key = store_key(Path(profile_dir) / "auth.json")
        affected = _chains_touching_store(view, key)
        profiles = sorted({lock_name(name), *_profiles_of_keys(_stores_of(affected))})
        with lifecycle_locks(profiles):
            refuse_conflicts(view, sorted(affected), [key], [name], action)
            report: Dict[str, Any] = {"retired": [], "released": []}
            if affected:
                from hermes_cli.auth_codex_plans import load_snapshots
                paths = [store_path_of(k) for k in _stores_of(affected)]
                present = [p for p in paths if _exists(Path(p).parent) is True]
                with store_locks(present):
                    payload = plan_delete(view, load_snapshots(present), key, name)
                    body = decide_op(payload, view)
                    apply_payload(body)
                    commit_op(payload.op_id)
                for cid, entry in payload.chains.items():
                    was_live = view.chains[cid].get("state") != STATE_RETIRED
                    if was_live and entry.get("state") == STATE_RETIRED:
                        report["retired"].append(cid)
                    elif was_live:
                        report["released"].append(cid)
            yield report


# ── Transfer (RULE-M3) ───────────────────────────────────────────────────────────────────────

def plan_transfer(view: RegistryView, snaps: Dict[str, Any], cid: str, dest: str) -> Payload:
    from hermes_cli.auth_codex_records import read_record
    entry = view.chain(cid)
    if entry is None or entry.get("state") == STATE_RETIRED:
        raise CodexAuthorityRefused("That Codex login is retired or unknown; there is nothing to move.")
    source = entry["owner"]
    if source == dest:
        raise CodexAuthorityRefused("That Codex login already belongs there.")
    src, dst = snaps.get(source), snaps.get(dest)
    if src is None or dst is None:
        raise CodexAuthorityRefused("The Codex login's store or the destination can't be read right now.")
    _refuse_aliased_ends(store_path_of(source), store_path_of(dest))
    record = read_record(store_path_of(source), cid)
    if record.status != "ok":
        raise CodexAuthorityRefused(
            "This Codex login's refresh record can't be read, so it can't be moved safely; sign in again "
            "in the destination profile instead.")
    epoch = entry["epoch"] + 1
    payload = Payload(kind=TRANSFER, scope_chains=[cid], scope_stores=sorted({source, dest}),
                      scope_profiles=sorted({src.profile, dst.profile}), apply_order=[dest, source])
    moved_rows: List[Dict[str, str]] = []
    kept_rows = [r for r in entry.get("rows", ()) if r["store"] != source]
    taken = {e.get("id") for e in rows_mod.pool_entries(dst.store) or () if isinstance(e, dict)}
    current: Optional[Tuple[str, str]] = None
    for r in entry.get("rows", ()):
        if r["store"] != source:
            continue
        row = r["row"]
        data = rows_mod.get_row(src.store, row)
        if data is None:
            continue
        access, refresh = rows_mod.row_tokens(src.store, row)
        current = current or (access, refresh)
        target = row
        existing = rows_mod.get_row(dst.store, row)
        if row == SINGLETON:
            if existing is not None and rows_mod.row_tokens(dst.store, SINGLETON)[1] and \
                    (view.chain_for_row(dest, SINGLETON) or (None,))[0] != cid:
                raise CodexAuthorityRefused(
                    "The destination already has its own Codex login; sign it out there first "
                    "(`hermes auth logout openai-codex` in that profile), then move this one.")
        elif existing is not None and (view.chain_for_row(dest, row) or (None,))[0] != cid:
            from hermes_cli.auth_codex_plans import _new_pool_id
            new_id = _new_pool_id(dst.store)
            while new_id in taken:
                new_id = _new_pool_id(dst.store)
            taken.add(new_id)
            target = rows_mod.pool_row(new_id)
        metadata = {k: v for k, v in data.items() if k not in ("tokens", "access_token", "refresh_token", "chain_id", "id")}
        if {"store": dest, "row": target} not in kept_rows:
            moved_rows.append({"store": dest, "row": target})
        payload.edits.setdefault(dest, []).append({
            "op": "write_row", "row": target, "chain_id": cid, "access_token": access, "refresh_token": refresh,
            "owned": {}, "defaults": metadata, "reset_failure": False})
        payload.edits.setdefault(source, []).append({
            "op": "remove_row", "row": row, "chain_id": cid, "fp": rows_mod.row_fp(src.store, row)})
    if current is None:
        raise CodexAuthorityRefused("That Codex login has no credential rows to move.")
    written = {e["row"] for e in payload.edits[dest]}
    for r in kept_rows:  # the destination's own copies of this chain become owner rows: current pair
        if r["store"] == dest and r["row"] not in written:
            payload.edits[dest].append({"op": "write_row", "row": r["row"], "chain_id": cid,
                                        "access_token": current[0], "refresh_token": current[1],
                                        "owned": {}, "defaults": {}, "reset_failure": False})
    sources = sorted({(e.get("defaults") or {}).get("source") for e in payload.edits[dest]} - {None})
    if any(r["row"] == SINGLETON for r in moved_rows) or "device_code" in sources:
        payload.edits[dest].append({"op": "unsuppress", "sources": ["device_code"]})
    payload.edits[dest].append({"op": "set_active_provider", "value": PROVIDER, "only_if_unset": True})
    payload.chains[cid] = {**entry, "owner": dest, "epoch": epoch, "rows": kept_rows + moved_rows}
    payload.records.append({"action": "import_moved", "owner": dest, "chain_id": cid, "epoch": epoch,
                            "record": record.data})
    payload.records.append({"action": "drop_moved", "owner": source, "chain_id": cid, "epoch": epoch,
                            "moved_to": dest})
    payload.result = {"chain_id": cid, "from": source, "to": dest, "rows": [r["row"] for r in moved_rows],
                      "state": entry.get("state"), "fence": (entry.get("fence") or {}).get("reason")}
    return payload


def _refuse_aliased_ends(source: Path, dest: Path) -> None:
    """Two ends that are ONE file under two canonical names (a bind-mounted directory, a hard link):
    writing the destination and then emptying the source would empty the destination too. Refused
    before anything is decided (a check that cannot be made refuses as well)."""
    try:
        same = os.path.exists(source) and os.path.exists(dest) and os.path.samefile(source, dest)
    except OSError:
        same = True
    if same:
        raise CodexAuthorityRefused(
            "The source and destination are the same credential file under two names; nothing was moved.")


def transfer_chain(dest_home: Path, chain: Optional[str] = None) -> Dict[str, Any]:
    """``hermes auth codex --transfer-to <profile>``: move a Codex login (the active store's own, or
    ``chain``) with its record to *dest_home*'s store. Fenced logins stay fenced (RULE-M3)."""
    from hermes_cli.auth_codex_authority import _run_with_all_stores
    from hermes_cli.auth_codex_registry import active_store_path
    dest_path = Path(dest_home) / "auth.json"
    dest = store_key(dest_path)
    here = store_key(active_store_path())

    def build(view, snaps):
        if view is None:
            raise CodexRegistryUnreadable()
        if chain:
            matches = [cid for cid in view.chains if cid.startswith(chain.strip())]
            if len(matches) != 1:
                raise CodexAuthorityRefused(f"No single Codex login matches {chain!r}; `hermes auth recovery list` shows them.")
            cid = matches[0]
        else:
            owned = sorted(cid for cid, e in view.chains.items()
                           if e.get("owner") == here and e.get("state") != STATE_RETIRED)
            if len(owned) != 1:
                raise CodexAuthorityRefused(
                    "This profile has " + ("no Codex login of its own" if not owned else "several Codex logins")
                    + "; choose one with `--chain <id>` (`hermes auth recovery list` shows them).")
            cid = owned[0]
        payload = plan_transfer(view, snaps, cid, dest)
        from hermes_cli.auth_codex_ops import run_op
        run_op(payload, view)
        return payload.result

    return _run_with_all_stores(build, extra_paths=[dest_path])
