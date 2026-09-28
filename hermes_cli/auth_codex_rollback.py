"""Preparing an install for a rollback to a build WITHOUT the Codex chain authority (deploy gate F8).

A build older than the credential recovery protocol reads and refreshes every Codex row in every
``auth.json`` directly: it ignores the registry, its fences, the owner-local chain records and the
retired-token index. Left as the new build leaves them, those rows can hold material the old build
must never present again:

* a chain fenced ``indeterminate`` / ``rejected`` keeps its PREDECESSOR pair in the rows (the
  refresh may have consumed it at the server);
* a copy of a chain outside its owner store is never updated by a refresh (only the owner's rows
  are), so after one rotation it is a consumed pair;
* a chain the protocol retired, paused (``legacy-rescue``, ``unexpected-token``,
  ``unverified-history``, ...) or cannot verify holds material nobody can vouch for;
* an attempt the protocol has not settled yet (a ``pending`` successor not yet in the rows) leaves
  the rows one rotation behind.

Restoring an old ``auth.json`` is never a rollback: it brings back pairs the new build already
consumed. :func:`prepare_rollback` (``hermes auth rollback-prepare``) runs WITH THE NEW BUILD
before the switch and leaves only material the old build may safely continue:

1. under the migration lock, the ordinary dispatcher pass (roll decided ops forward, archive
   undecided payloads, fence legacy rescues); any op still unfinished afterwards (lifecycle-stuck,
   unrecoverable) REFUSES the preparation, naming the command that resolves it;
2. every active chain is settled through the real refresh transaction with the network disabled
   (pending successors adopted / completed, an intent found at start fenced ``indeterminate``);
3. every Codex row that is not "an owner row of an ACTIVE chain holding exactly the pair its record
   says is current, with no attempt in flight" is REMOVED as an ownership op (the ``hermes auth
   remove`` machinery: the chain is retired, its record archived and its material kept in the
   retired-token index), so the old build finds no Codex login there and asks for a sign-in;
4. the Codex CLI login the old build would import (``$CODEX_HOME`` / ``~/.codex``) is checked
   against Hermes's evidence: material Hermes already used (or cannot check) blocks the receipt;
5. a receipt ``<root>/auth.rollback-receipt.json`` is written LAST, only when the result is safe.

Non-Codex providers are never touched. Every write is the new build's own protocol (dispatcher,
refresh transaction, removal op, strict writer), so a crash at any point leaves a state the new
build recovers by itself; the receipt is removed first and written last, so a crashed preparation
never looks complete. ``--check`` (:func:`check_rollback`) re-verifies the live state read-only
right before the switch.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_cli import auth_codex_rows as rows_mod
from hermes_cli.auth_codex_records import read_record
from hermes_cli.auth_codex_registry import (
    OPS_DIR_NAME, RECORDS_DIR_NAME, REGISTRY_NAME, SINGLETON, STATE_ACTIVE, CodexAuthorityRefused, RegistryView,
    all_store_paths, migration_lock, pair_fp, read_registry, root_home, store_locks, store_path_of,
    thread_holds_store_lock, utc_now)
from hermes_cli.auth_credential_io import (
    encode_json, path_memo, require_committed, store_key, unlink_retrying, write_credential_file)

logger = logging.getLogger("hermes_cli.auth")

RECEIPT_NAME = "auth.rollback-receipt.json"
RECEIPT_VERSION = 1

KEEP = "keep"
# CLI-login verdicts that block the receipt: the old build imports that file whenever its own
# Codex login is missing or rejected.
_CLI_UNSAFE = frozenset({"known", "unverifiable", "unreadable"})


def receipt_path() -> Path:
    return root_home() / RECEIPT_NAME


@dataclass(frozen=True)
class RowVerdict:
    store: str
    row: str
    verdict: str                  # KEEP, or why the row must go
    chain_id: Optional[str] = None


@dataclass
class Assessment:
    registry: str                                   # ok | absent | unreadable
    rows: List[RowVerdict] = field(default_factory=list)
    blocking_ops: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    protocol_traces: bool = False
    codex_cli: Dict[str, Any] = field(default_factory=dict)
    # Profiles with no Codex login of their own while root keeps one: the old build makes them
    # BORROW root's, and its borrow path forks the chain (root is left holding a consumed pair) —
    # an old-build hazard the preparation cannot remove; reported so each can sign in on its own.
    borrowers: List[str] = field(default_factory=list)

    @property
    def to_remove(self) -> List[RowVerdict]:
        return [v for v in self.rows if v.verdict != KEEP]

    @property
    def kept(self) -> List[RowVerdict]:
        return [v for v in self.rows if v.verdict == KEEP]

    @property
    def safe(self) -> bool:
        return (not self.blocking_ops and not self.to_remove
                and self.codex_cli.get("status") not in _CLI_UNSAFE)


# ── classification ────────────────────────────────────────────────────────────────────────────

def material_rows(store: Dict[str, Any]) -> List[str]:
    """Every Codex row that holds ANY token material (an access token alone is still presented).
    Id-less pool rows are listed as ``pool-index:<n>`` (they cannot be registered or removed by id)."""
    out = []
    if any(rows_mod.row_tokens(store, SINGLETON)):
        out.append(SINGLETON)
    for n, entry in enumerate(rows_mod.pool_entries(store) or ()):
        if not isinstance(entry, dict):
            continue
        if not (str(entry.get("access_token") or "").strip() or str(entry.get("refresh_token") or "").strip()):
            continue
        out.append(rows_mod.pool_row(str(entry["id"])) if entry.get("id") else f"pool-index:{n}")
    return out


def row_verdict(view: RegistryView, key: str, store: Dict[str, Any], row: str,
                records: Dict[str, Any]) -> RowVerdict:
    """KEEP only an owner row of an ACTIVE chain (no lifecycle exclusion) whose record reads, is of
    the chain's epoch, has no attempt in flight, and names exactly this row's pair as current."""
    if row.startswith("pool-index:"):
        return RowVerdict(key, row, "unregistered (no row id)")
    found = view.chain_for_row(key, row)
    if found is None:
        return RowVerdict(key, row, "unregistered")
    cid, entry = found
    if entry.get("state") != STATE_ACTIVE:
        reason = (entry.get("fence") or {}).get("reason")
        return RowVerdict(key, row, f"{entry.get('state')}" + (f" ({reason})" if reason else ""), cid)
    if entry.get("lifecycle"):
        return RowVerdict(key, row, "profile move in progress", cid)
    if entry.get("owner") != key:
        return RowVerdict(key, row, "copy outside the owner store", cid)
    record = records.get(cid)
    if record is None or record.status != "ok":
        return RowVerdict(key, row, f"record {getattr(record, 'status', 'missing')}", cid)
    if record.data.get("epoch") != entry.get("epoch"):
        return RowVerdict(key, row, "record from an older ownership", cid)
    if record.data.get("attempt"):
        return RowVerdict(key, row, f"refresh attempt {record.data['attempt'].get('state')}", cid)
    if rows_mod.row_fp(store, row) != record.data.get("current_fp"):
        return RowVerdict(key, row, "not the chain's current pair", cid)
    return RowVerdict(key, row, KEEP, cid)


def _protocol_traces(paths: Iterable[Path]) -> bool:
    """The new build has governed Codex here before (records, ops, the retired index, a preserved
    damaged registry): with the registry itself absent, no row can be vouched for."""
    from hermes_cli.auth_codex_evidence import retired_index_path
    root = root_home()
    candidates = [root / OPS_DIR_NAME, retired_index_path()] + [Path(p).parent / RECORDS_DIR_NAME for p in paths]
    if any(os.path.lexists(c) for c in candidates):
        return True
    return any(root.glob(f"{REGISTRY_NAME}.corrupt.*"))


def scan_paths(view: RegistryView) -> List[Path]:
    """Every store the old build could read: root, the active store, every profile, and every store
    the registry names (an owner or row store outside ``profiles/``)."""
    paths = list(all_store_paths())
    seen = {store_key(p) for p in paths}
    named = {e.get("owner") for e in view.chains.values()} | {
        r.get("store") for e in view.chains.values() for r in e.get("rows", ())}
    for key in sorted(k for k in named if k):
        path = store_path_of(key)
        if store_key(path) not in seen and path.exists():
            seen.add(store_key(path))
            paths.append(path)
    return paths


def assess(view: RegistryView, snaps: Dict[str, Any]) -> Assessment:
    """Classify every Codex row of *snaps* (``{key: Snapshot}``). Records are read here: the caller
    holds the owner store locks (or is the read-only ``--check``, which takes none)."""
    out = Assessment(view.status)
    if view.ok:
        out.blocking_ops = list(view.ops.items())
        records = {cid: read_record(store_path_of(e["owner"]), cid) for cid, e in view.chains.items()
                   if e.get("state") == STATE_ACTIVE}
    else:
        out.protocol_traces = view.status == "unreadable" or _protocol_traces(s.path for s in snaps.values())
    for key, snap in snaps.items():
        for row in material_rows(snap.store):
            if view.ok:
                out.rows.append(row_verdict(view, key, snap.store, row, records))
            elif out.protocol_traces:
                out.rows.append(RowVerdict(key, row, f"authority registry {view.status}"))
            else:
                out.rows.append(RowVerdict(key, row, KEEP))  # never governed by the new build
    return out


# ── the Codex CLI login the old build imports ─────────────────────────────────────────────────

def codex_cli_auth_path() -> Path:
    """The file the old build's ``_import_codex_cli_tokens`` reads (same resolution)."""
    return Path(os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")).expanduser() / "auth.json"


def codex_cli_verdict(view: RegistryView, kept_fps: Iterable[str]) -> Dict[str, Any]:
    """Would the old build import token material Hermes has already used?

    ``absent`` / ``ignored`` (the old build would not import it: no pair, or an access token that
    is already expired, which only gets older) / ``current`` (it IS a kept chain's current pair:
    shared with the CLI, as before this build) / ``unknown`` (never held by Hermes) are not
    blocking. ``known`` (any token Hermes's history or retired index lists), ``unverifiable``
    (Hermes's evidence is incomplete) and ``unreadable`` block the receipt."""
    from hermes_cli.auth_codex import _codex_access_token_is_expiring
    path = codex_cli_auth_path()
    out: Dict[str, Any] = {"path": str(path)}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (FileNotFoundError, NotADirectoryError):
        return {**out, "status": "absent"}
    except OSError as exc:
        return {**out, "status": "unreadable", "detail": str(exc)}
    try:
        tokens = json.loads(raw).get("tokens")
    except (ValueError, AttributeError):
        return {**out, "status": "ignored"}
    if not (isinstance(tokens, dict) and tokens.get("access_token") and tokens.get("refresh_token")):
        return {**out, "status": "ignored"}
    access, refresh = str(tokens["access_token"]).strip(), str(tokens["refresh_token"]).strip()
    if _codex_access_token_is_expiring(access, 0):
        return {**out, "status": "ignored"}
    if pair_fp(access, refresh) in set(kept_fps):
        return {**out, "status": "current"}
    if view.status == "absent" and not _protocol_traces(all_store_paths()):
        return {**out, "status": "unknown"}  # the new build never governed (so never used) a Codex token here
    from hermes_cli.auth_codex_evidence import known_material
    evidence = known_material(view)
    if evidence.knows(access, refresh):
        return {**out, "status": "known"}
    if not evidence.complete:
        return {**out, "status": "unverifiable", "detail": "; ".join(evidence.gaps[:3])}
    return {**out, "status": "unknown"}


# ── read-only check ───────────────────────────────────────────────────────────────────────────

def _plain_snapshots(paths: Iterable[Path]) -> Dict[str, Any]:
    from hermes_cli.auth_codex_plans import load_snapshots
    return load_snapshots([p for p in paths if Path(p).exists()])


def _kept_fps(assessment: Assessment, snaps: Dict[str, Any]) -> List[str]:
    return [rows_mod.row_fp(snaps[v.store].store, v.row) for v in assessment.kept if v.store in snaps]


def root_login_borrowers(assessment: Assessment, snaps: Dict[str, Any]) -> List[str]:
    """Named profiles without Codex material of their own while root keeps a Codex login."""
    from hermes_cli.auth_codex_registry import is_root_store, profiles_root
    if not any(is_root_store(v.store) for v in assessment.kept):
        return []
    directory = profiles_root()
    names = sorted(p.name for p in directory.iterdir() if p.is_dir() and not p.name.startswith(".")) \
        if directory.is_dir() else []
    out = []
    for name in names:
        path = directory / name / "auth.json"
        snap = snaps.get(store_key(path)) if path.exists() else None
        if snap is None or not material_rows(snap.store):
            out.append(name)
    return out


def _finish(result: Assessment, view: RegistryView, snaps: Dict[str, Any]) -> Assessment:
    result.codex_cli = codex_cli_verdict(view, _kept_fps(result, snaps))
    result.borrowers = root_login_borrowers(result, snaps)
    return result


def check_rollback() -> Assessment:
    """``hermes auth rollback-prepare --check``: plain reads only (no lock, no dispatch, no write).
    Safe = nothing to remove, no unfinished op, and no blocking Codex CLI login."""
    with path_memo():
        view = read_registry()
        snaps = _plain_snapshots(scan_paths(view))
        result = _finish(assess(view, snaps), view, snaps)
    return result


# ── preparation ───────────────────────────────────────────────────────────────────────────────

def _never_send(*_a, **_k):
    """The settle pass is never forced and never finds a token worth refreshing; reaching the token
    endpoint would be a bug. ``httpx.ConnectError`` is on the pre-send allowlist (decision 4), so
    even then nothing is fenced and nothing is sent."""
    from hermes_cli.auth_constants import httpx
    raise httpx.ConnectError("rollback-prepare never contacts the token endpoint")


def _settle_active_chains(report: Dict[str, Any]) -> None:
    """RULE-S2 reconcile of every active chain through the real refresh transaction, network off:
    a pending successor lands in the rows (adopt / complete); an intent found at start or any other
    mismatch becomes the fence the transaction requests (and is then removed below)."""
    from hermes_cli import auth as auth_mod
    from hermes_cli.auth_codex_refresh_txn import refresh_row
    from hermes_cli.auth_constants import AUTH_LOCK_TIMEOUT_SECONDS, AuthError
    view = read_registry()
    for cid, entry in sorted(view.chains.items()):
        if entry.get("state") != STATE_ACTIVE or entry.get("lifecycle"):
            continue
        owner = store_path_of(entry["owner"])
        owner_rows = [r["row"] for r in entry.get("rows", ()) if r.get("store") == entry["owner"]]
        if not owner_rows or not owner.exists():
            continue
        started = rows_mod.row_tokens(auth_mod._load_auth_store(owner), owner_rows[0])
        if not any(started):
            continue
        try:
            refresh_row(owner, owner_rows[0], started, force=False, needs_refresh=lambda _access: False,
                        timeout_seconds=float(AUTH_LOCK_TIMEOUT_SECONDS), refresh_fn=_never_send)
        except AuthError as exc:
            if getattr(exc, "retryable", False):
                raise CodexAuthorityRefused(
                    "Another Hermes process is using this Codex login. Stop every Hermes process "
                    f"(desktop, gateway, CLI sessions), then run the preparation again ({exc}).") from None
            report["fenced_while_settling"].append({"chain_id": cid, "code": exc.code})


def _remove_rows(report: Dict[str, Any]) -> None:
    """Remove every row the assessment does not keep, one ownership op per store (RULE-K1: the
    ``hermes auth remove`` machinery — retire / archive / index — so the new build agrees)."""
    from hermes_cli.auth_codex_authority import _run_with_all_stores
    from hermes_cli.auth_codex_ops import run_op
    from hermes_cli.auth_codex_plans import load_snapshots, plan_removal

    def build(view, snaps):
        if view is None:
            raise CodexAuthorityRefused("The Codex login registry became unreadable during the preparation.")
        paths = [s.path for s in snaps.values()]
        by_store: Dict[str, List[RowVerdict]] = {}
        for verdict in assess(view, snaps).to_remove:
            by_store.setdefault(verdict.store, []).append(verdict)
        for key in sorted(by_store):
            with_id = [v.row for v in by_store[key] if not v.row.startswith("pool-index:")]
            if with_id:
                # Each store's removal is its own op, planned from the registry and stores as the
                # previous one left them (a chain retired there is already retired here).
                view = read_registry()
                payload = plan_removal(view, load_snapshots(paths), key, with_id)
                payload.kind = "rollback-prepare"
                run_op(payload, view)
            if len(with_id) != len(by_store[key]):
                _strip_idless(snaps[key].path)
            report["removed"].extend({"store": v.store, "row": v.row, "reason": v.verdict,
                                      "chain_id": v.chain_id} for v in by_store[key])
    known = {store_key(q) for q in all_store_paths()}
    _run_with_all_stores(build, extra_paths=[p for p in scan_paths(read_registry()) if store_key(p) not in known])


def _strip_idless(path: Path) -> None:
    """Id-less Codex pool rows are never registered (bootstrap assigns ids): they are removed like
    any unregistered row, directly (caller holds the store lock)."""
    from hermes_cli import auth as auth_mod
    store = auth_mod._load_auth_store(path)
    entries = rows_mod.pool_entries(store) or []
    kept = [e for e in entries if not (isinstance(e, dict) and not e.get("id") and (
        str(e.get("access_token") or "").strip() or str(e.get("refresh_token") or "").strip()))]
    if len(kept) != len(entries):
        entries[:] = kept
        if not kept:
            store["credential_pool"].pop(rows_mod.PROVIDER, None)
        auth_mod._save_auth_store(store, target_path=path)


def _strip_all_codex(paths: Iterable[Path], report: Dict[str, Any], reason: str) -> None:
    """No readable registry although the new build governed Codex here: nothing can be vouched for,
    so every Codex row goes (fail closed). A direct edit under each store's lock — there is no
    registry to record an op in, and none is needed: no chain can be refreshed without one."""
    from hermes_cli import auth as auth_mod
    for path in paths:
        if not Path(path).exists():
            continue
        with store_locks([path]):
            store = auth_mod._load_auth_store(path)
            removed = material_rows(store)
            if not removed:
                continue
            if SINGLETON in removed:
                store["providers"].pop(rows_mod.PROVIDER, None)
            entries = rows_mod.pool_entries(store)
            if entries is not None:
                entries[:] = [e for e in entries if not (isinstance(e, dict) and (
                    str(e.get("access_token") or "").strip() or str(e.get("refresh_token") or "").strip()))]
                if not entries:
                    store["credential_pool"].pop(rows_mod.PROVIDER, None)
            auth_mod._save_auth_store(store, target_path=path)
            report["removed"].extend({"store": store_key(path), "row": r, "reason": reason} for r in removed)


def _blocking_message(ops: List[Tuple[str, Dict[str, Any]]]) -> str:
    lines = []
    for op_id, op in ops:
        short = op_id[:8]
        if op.get("status") == "unrecoverable":
            hint = f"`hermes auth recovery resolve {short}`"
        else:
            hint = f"`hermes auth recover --op {short}` (or `hermes auth recover --op {short} --abandon`)"
        lines.append(f"  {short}: {op.get('kind')} ({op.get('status')}) — finish it with {hint}")
    return ("A Codex login update is unfinished, so Hermes can't prepare the rollback yet. No Codex login "
            "was removed.\n" + "\n".join(lines) + "\nThen run `hermes auth rollback-prepare` again.")


def _write_receipt(result: Assessment, report: Dict[str, Any]) -> Path:
    body = {"version": RECEIPT_VERSION, "prepared_at": utc_now(), "registry": result.registry,
            "kept": [{"store": v.store, "row": v.row, "chain_id": v.chain_id} for v in result.kept],
            "removed": report["removed"], "fenced_while_settling": report["fenced_while_settling"],
            "codex_cli": result.codex_cli, "profiles_that_would_borrow_root": result.borrowers,
            "rule": "Never restore an auth.json backup or snapshot as a rollback; sign in again instead."}
    require_committed(write_credential_file(receipt_path(), encode_json(body)), "the rollback receipt")
    return receipt_path()


def prepare_rollback() -> Tuple[Assessment, Dict[str, Any]]:
    """``hermes auth rollback-prepare``. Returns the final (locked) assessment and what was done;
    the receipt exists afterwards only when ``assessment.safe``. Idempotent: running it again after
    a crash, or after it completed, converges on the same state."""
    if thread_holds_store_lock():
        raise RuntimeError("rollback-prepare must not run inside an auth-store lock")
    report: Dict[str, Any] = {"removed": [], "fenced_while_settling": [], "dispatch": None}
    with migration_lock():
        try:
            unlink_retrying(receipt_path())  # a crashed or outdated run never looks complete
        except FileNotFoundError:
            pass
        view = read_registry()
        if view.ok:
            from hermes_cli.auth_codex_authority import dispatch_locked
            with path_memo():
                report["dispatch"] = dispatch_locked()
            view = read_registry()
            stuck = list(view.ops.items())
            excluded = [cid for cid, e in view.chains.items() if e.get("lifecycle")]
            if stuck or excluded:
                raise CodexAuthorityRefused(_blocking_message(stuck) if stuck else (
                    "A Codex login is excluded by a profile move that has no op; run `hermes auth migrate`."))
            with path_memo():
                _settle_active_chains(report)
            with path_memo():
                _remove_rows(report)
        else:
            with path_memo():
                paths = scan_paths(view)
                snaps = _plain_snapshots(paths)
                if view.status == "unreadable" or _protocol_traces(paths):
                    if view.status == "unreadable":
                        from hermes_cli.auth_codex_registry import preserve_unreadable_registry
                        preserve_unreadable_registry()
                    _strip_all_codex([s.path for s in snaps.values()], report, f"authority registry {view.status}")
        with path_memo():
            final = _final_assessment()
        if final.safe:
            report["receipt"] = str(_write_receipt(final, report))
    return final, report


def _final_assessment() -> Assessment:
    """The state as the old build will find it: every store's rows under every store lock (records
    are read only under their owner's lock), then the Codex CLI check against the evidence."""
    from hermes_cli.auth_codex_plans import load_snapshots
    view = read_registry()
    paths = [p for p in scan_paths(view) if p.exists()]
    with store_locks(paths):
        snaps = load_snapshots(paths)
        result = _finish(assess(view, snaps), view, snaps)
    return result


# ── ``hermes auth rollback-prepare [--check]`` ────────────────────────────────────────────────

_CLI_TEXT = {
    "absent": "none", "ignored": "present, not importable by the old build (no pair / expired)",
    "current": "holds the same login Hermes keeps (shared with the Codex CLI, as before this build)",
    "unknown": "a login Hermes never held",
    "known": "holds a token Hermes ALREADY USED — the old build would import it when its own login is "
             "missing or rejected. Sign the Codex CLI in again (`codex login`), then re-run",
    "unverifiable": "Hermes can't check it against its login history ({detail}). Sign the Codex CLI in "
                    "again (`codex login`), then re-run",
    "unreadable": "could not be read ({detail}); fix access to it, then re-run",
}


def _describe(result: Assessment, *, done: bool) -> List[str]:
    lines = [f"Codex login registry: {result.registry}"]
    if result.kept:
        lines.append(f"  kept {len(result.kept)} Codex row(s); the old build continues them:")
        lines += [f"    {v.store}  {v.row}  (login {str(v.chain_id or '')[:12]})" for v in result.kept]
    if result.to_remove:
        verb = "still holds" if done else "would remove"
        lines.append(f"  {verb} {len(result.to_remove)} Codex row(s) the old build must not use:")
        lines += [f"    {v.store}  {v.row}  ({v.verdict})" for v in result.to_remove]
    cli = result.codex_cli
    text = _CLI_TEXT.get(cli.get("status"), str(cli.get("status")))
    lines.append(f"  Codex CLI login {cli.get('path')}: " + text.format(detail=cli.get("detail", "")))
    if result.borrowers:
        lines.append(
            "  WARNING — under the old build these profiles have no Codex login of their own and would "
            "BORROW root's, which the old build does unsafely (a borrowed refresh leaves root holding a used "
            "token). After the switch, sign in to Codex separately in each one you use Codex from "
            "(`hermes -p <name> auth`), or use Codex only from the default profile: " + ", ".join(result.borrowers))
    return lines


def rollback_prepare_command(args) -> None:
    from hermes_cli.auth_constants import AuthError
    if getattr(args, "check", False):
        result = check_rollback()
        for line in _describe(result, done=False):
            print(line)
        if result.blocking_ops:
            print(_blocking_message(result.blocking_ops))
        if result.safe:
            print("SAFE TO ROLL BACK: nothing to prepare.")
            return
        raise SystemExit("NOT READY: run `hermes auth rollback-prepare` (with this build) first.")
    try:
        result, report = prepare_rollback()
    except AuthError as exc:
        raise SystemExit(str(exc)) from exc
    if report["removed"]:
        print(f"Removed {len(report['removed'])} Codex row(s) the old build must not use "
              "(it will ask you to sign in to Codex there):")
        for item in report["removed"]:
            print(f"    {item['store']}  {item['row']}  ({item['reason']})")
    for line in _describe(result, done=True):
        print(line)
    if not result.safe:
        raise SystemExit("NOT SAFE TO ROLL BACK YET (see above). No receipt was written.")
    print(f"SAFE TO ROLL BACK. Receipt: {report['receipt']}")
    print("Stop every Hermes process before switching builds, and never restore an auth.json backup or "
          "snapshot: sign in to Codex again instead. `hermes auth rollback-prepare --check` re-verifies "
          "right before the switch.")
