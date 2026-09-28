"""``hermes auth rollback-prepare [--check]``: prepare an install for a rollback to a build WITHOUT
the Codex chain authority (deploy gate F8). The end state and why each rule exists:
``auth_codex_rollback_plan``.

Restoring an old ``auth.json`` is never a rollback: it brings back pairs the new build already
consumed. :func:`prepare_rollback` runs WITH THE NEW BUILD before the switch, under the migration
lock:

1. removes an earlier receipt (an interrupted run never looks complete);
2. takes a STRICT inventory of every home (a denied listing, an unreadable or unparseable
   ``auth.json`` refuses); an unreadable registry refuses; a registry that was damaged or removed
   after use needs ``--other-homes-checked``;
3. runs the ordinary dispatcher pass and REFUSES while an ownership op is unfinished;
4. settles every active chain through the real refresh transaction with the network disabled;
5. records the token hashes of every row it will remove in the retired-token index, then removes
   them as ownership ops (a login losing every row is retired like ``hermes auth remove``; a
   login keeping one row only drops the others; id-less rows directly);
6. switches Codex OFF for the older build in every home other than the root that keeps no login
   of its own, and drops a token-less root provider section;
7. re-reads everything under the store locks and writes ``<root>/auth.rollback-receipt.json``
   LAST, only when the result is safe (which includes: no importable Codex CLI pair).

Every write is the new build's own protocol (dispatcher, refresh transaction, ownership op, strict
writer), so a crash leaves a state the new build recovers by itself and a re-run completes it.
``--check`` (:func:`check_rollback`) re-verifies the live state with plain, side-effect-free reads.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from hermes_cli import auth_codex_rows as rows_mod
from hermes_cli.auth_codex_registry import (
    PROVIDER, SINGLETON, STATE_ACTIVE, CodexAuthorityRefused, RegistryView, all_store_paths, migration_lock,
    pair_fp, read_registry, root_home, store_locks, store_path_of, thread_holds_store_lock, token_sha, utc_now)
from hermes_cli.auth_codex_rollback_plan import (
    CODEX_OFF_SOURCE, Assessment, InventoryIncomplete, assess, codex_cli_verdict, codex_off_row, home_paths,
    load_homes, row_material)
from hermes_cli.auth_credential_io import (
    encode_json, path_memo, require_committed, store_key, unlink_retrying, write_credential_file)

logger = logging.getLogger("hermes_cli.auth")

RECEIPT_NAME = "auth.rollback-receipt.json"
RECEIPT_VERSION = 2


def receipt_path() -> Path:
    return root_home() / RECEIPT_NAME


def _assessment(*, other_homes_checked: bool) -> Assessment:
    view = read_registry()
    try:
        homes = load_homes(home_paths(view))
    except InventoryIncomplete as exc:
        out = Assessment(view.status, refusal=str(exc))
    else:
        out = assess(view, homes, other_homes_checked=other_homes_checked)
    out.codex_cli = codex_cli_verdict()
    return out


def check_rollback(*, other_homes_checked: bool = False) -> Assessment:
    """Read-only: no lock, no dispatch, no write (the stores are parsed from raw bytes)."""
    with path_memo():
        return _assessment(other_homes_checked=other_homes_checked)


# ── preparation steps ─────────────────────────────────────────────────────────────────────────

def _never_send(*_a, **_k):
    """The settle pass is never forced and never finds a token worth refreshing; reaching the token
    endpoint would be a bug. ``httpx.ConnectError`` is on the pre-send allowlist (decision 4), so
    even then nothing is fenced and nothing is sent."""
    from hermes_cli.auth_constants import httpx
    raise httpx.ConnectError("rollback-prepare never contacts the token endpoint")


def _settle_active_chains(report: Dict[str, Any]) -> None:
    """RULE-S2 reconcile of every active chain through the real refresh transaction, network off."""
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


def _retain_removed(pairs: List[Tuple[str, str]], kept_fps: set) -> None:
    """Every removed pair stays recognisable on roll-forward (import refused, registration fenced):
    its hashes go to the retired-token index BEFORE it is deleted (caller holds the migration lock)."""
    from hermes_cli.auth_codex_evidence import retain
    material = {}
    for access, refresh in pairs:
        fp = pair_fp(access, refresh)
        if fp in kept_fps or not (access or refresh):
            continue  # the pair a kept row still holds is the live chain's, already in its record
        material[f"rollback-removed:{fp[:24]}"] = {
            "access": {s for s in (token_sha(access),) if s}, "refresh": {s for s in (token_sha(refresh),) if s},
            "fps": {fp}, "complete": True, "reason": "rollback-removed"}
    retain(material)


def _strip_rows_directly(path: Path, rows: List[str]) -> None:
    """Rows no registry op can name (id-less, or no readable registry): removed under the store lock."""
    from hermes_cli import auth as auth_mod
    store = auth_mod._load_auth_store(path)
    indexes = {int(r.split(":", 1)[1]) for r in rows if r.startswith("pool-index:")}
    ids = {rows_mod.pool_id(r) for r in rows if r.startswith("pool:")}
    entries = rows_mod.pool_entries(store)
    if entries is not None:
        entries[:] = [e for n, e in enumerate(entries)
                      if n not in indexes and not (isinstance(e, dict) and e.get("id") in ids)]
        if not entries:
            store["credential_pool"].pop(PROVIDER, None)
    if SINGLETON in rows and isinstance(store.get("providers"), dict):
        store["providers"].pop(PROVIDER, None)
    auth_mod._save_auth_store(store, target_path=path)


def _trim_payload(view: RegistryView, key: str, store: Dict[str, Any], cid: str, rows: List[str]):
    """Drop *rows* from an ACTIVE login that keeps another row in this home: the login stays active
    with fewer rows (epoch + 1, its record's epoch rewritten) — never retired."""
    from hermes_cli.auth_codex_ops import Payload
    entry = view.chain(cid)
    epoch = entry["epoch"] + 1
    payload = Payload(kind="rollback-prepare", scope_chains=[cid], scope_stores=[key])
    payload.chains[cid] = {**entry, "epoch": epoch,
                           "rows": [r for r in entry["rows"] if not (r["store"] == key and r["row"] in rows)]}
    payload.records.append({"action": "set_epoch", "owner": entry["owner"], "chain_id": cid, "epoch": epoch})
    payload.edits[key] = [{"op": "remove_row", "row": r, "chain_id": cid, "fp": rows_mod.row_fp(store, r)}
                          for r in rows]
    return payload


def _remove_rows(report: Dict[str, Any], *, other_homes_checked: bool) -> None:
    """Remove every row the assessment does not keep, evidence recorded first."""
    from hermes_cli.auth_codex_authority import _run_with_all_stores
    from hermes_cli.auth_codex_ops import run_op
    from hermes_cli.auth_codex_plans import load_snapshots, plan_removal

    paths = home_paths(read_registry())

    def build(view, _snaps):
        homes = load_homes(paths)
        plan = assess(read_registry(), homes, other_homes_checked=other_homes_checked)
        if plan.refusal:
            raise InventoryIncomplete(plan.refusal)
        kept_fps = {rows_mod.row_fp(homes[v.store].store, v.row) for v in plan.kept}
        kept_chains = {(v.store, v.chain_id) for v in plan.kept}
        by_home: Dict[str, List[Any]] = {}
        for v in plan.to_remove:
            by_home.setdefault(v.store, []).append(v)
        _retain_removed([row_material(homes[k].store, v.row) for k, vs in by_home.items() for v in vs], kept_fps)
        for key in sorted(by_home):
            direct = [v.row for v in by_home[key] if v.row.startswith("pool-index:") or view is None]
            named = [v for v in by_home[key] if v.row not in direct]
            trims: Dict[str, List[str]] = {}
            for v in named:
                if (key, v.chain_id) in kept_chains:
                    trims.setdefault(v.chain_id, []).append(v.row)
            for cid, rows in sorted(trims.items()):
                current = read_registry()
                run_op(_trim_payload(current, key, load_homes([homes[key].path])[key].store, cid, rows), current)
            whole = [v.row for v in named if (key, v.chain_id) not in kept_chains]
            if whole:
                current = read_registry()
                payload = plan_removal(current, load_snapshots([h.path for h in homes.values() if h.path.exists()]),
                                       key, whole)
                payload.kind = "rollback-prepare"
                run_op(payload, current)
            if direct:
                _strip_rows_directly(homes[key].path, direct)
        report["removed"].extend({"store": v.store, "row": v.row, "reason": v.verdict, "chain_id": v.chain_id}
                                 for v in plan.to_remove)

    existing = [p for p in paths if p.exists()]
    if read_registry().ok:
        standard = {store_key(q) for q in all_store_paths()}
        _run_with_all_stores(build, extra_paths=[p for p in existing if store_key(p) not in standard])
    else:  # the registry is gone after use and the operator confirmed the other homes: every row goes
        with store_locks(existing):
            build(None, {})


def _apply_fixes(report: Dict[str, Any], *, other_homes_checked: bool) -> None:
    """Codex OFF for the older build in every home other than the root that keeps no login of its
    own (an empty provider section + one inert pool row); a token-less root section is dropped."""
    from hermes_cli import auth as auth_mod
    paths = home_paths(read_registry())
    plan = assess(read_registry(), load_homes(paths), other_homes_checked=other_homes_checked)
    kept = {(v.store, v.row) for v in plan.kept}
    for key, fix in plan.fixes:
        path = next(p for p in paths if store_key(p) == key)
        with store_locks([path]):
            store = auth_mod._load_auth_store(path)
            providers = store.setdefault("providers", {})
            if fix == "drop-tokenless":
                providers.pop(PROVIDER, None)
            else:
                if (key, SINGLETON) not in kept:
                    providers[PROVIDER] = {}
                entries = rows_mod.pool_entries(store, create=True)
                has_kept_pool = any(k == key and r != SINGLETON for k, r in kept)
                if not has_kept_pool and not any(isinstance(e, dict) and e.get("source") == CODEX_OFF_SOURCE
                                                 for e in entries):
                    entries.append(codex_off_row())
            auth_mod._save_auth_store(store, target_path=path)
        report["fixed"].append({"store": key, "fix": fix})


def _final_assessment(*, other_homes_checked: bool) -> Assessment:
    """The state as the older build will find it: every home under its lock."""
    paths = home_paths(read_registry())
    with store_locks([p for p in paths if p.exists()]):
        return _assessment(other_homes_checked=other_homes_checked)


def _write_receipt(result: Assessment, report: Dict[str, Any]) -> Path:
    body = {"version": RECEIPT_VERSION, "prepared_at": utc_now(), "registry": result.registry,
            "untouched": result.untouched,
            "kept": [{"store": v.store, "row": v.row, "chain_id": v.chain_id} for v in result.kept],
            "removed": report["removed"], "fixed": report["fixed"],
            "fenced_while_settling": report["fenced_while_settling"],
            "codex_off_for_the_older_build": result.codex_off, "codex_cli": result.codex_cli,
            "rule": "Never restore an auth.json backup or snapshot as a rollback; sign in again instead."}
    require_committed(write_credential_file(receipt_path(), encode_json(body)), "the rollback receipt")
    return receipt_path()


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


def prepare_rollback(*, other_homes_checked: bool = False) -> Tuple[Assessment, Dict[str, Any]]:
    """``hermes auth rollback-prepare``. Returns the final (locked) assessment and what was done; the
    receipt exists afterwards only when ``assessment.safe``. Idempotent."""
    if thread_holds_store_lock():
        raise RuntimeError("rollback-prepare must not run inside an auth-store lock")
    report: Dict[str, Any] = {"removed": [], "fixed": [], "fenced_while_settling": [], "dispatch": None}
    with migration_lock():
        try:
            unlink_retrying(receipt_path())  # a crashed or outdated run never looks complete
        except FileNotFoundError:
            pass
        with path_memo():
            first = _assessment(other_homes_checked=other_homes_checked)
        if first.refusal:
            raise InventoryIncomplete(first.refusal)
        if not first.untouched:
            if read_registry().ok:
                from hermes_cli.auth_codex_authority import dispatch_locked
                with path_memo():
                    report["dispatch"] = dispatch_locked()
                view = read_registry()
                if view.ops:
                    raise CodexAuthorityRefused(_blocking_message(list(view.ops.items())))
                if any(e.get("lifecycle") for e in view.chains.values()):
                    raise CodexAuthorityRefused("A Codex login is excluded by a profile move that has no op; "
                                                "run `hermes auth migrate`.")
                with path_memo():
                    _settle_active_chains(report)
            with path_memo():
                _remove_rows(report, other_homes_checked=other_homes_checked)
            with path_memo():
                _apply_fixes(report, other_homes_checked=other_homes_checked)
        with path_memo():
            final = _final_assessment(other_homes_checked=other_homes_checked)
        if final.safe:
            report["receipt"] = str(_write_receipt(final, report))
    return final, report


# ── ``hermes auth rollback-prepare [--check] [--other-homes-checked]`` ───────────────────────

_CLI_TEXT = {
    "absent": "none",
    "ignored": "present, not importable by the older build (no pair / expired)",
    "importable": ("holds a login the older build would IMPORT into every home whose own login is missing or "
                   "rejected; several homes importing one pair replay it. Move it aside for the rollback "
                   "(`codex logout`, or rename the file), then run this again"),
    "unreadable": "could not be read ({detail}); fix access to it, then run this again",
}


def _describe(result: Assessment, *, done: bool) -> List[str]:
    lines = [f"Codex login registry: {result.registry}"]
    if result.untouched:
        lines.append("  this build never managed Codex logins here: nothing to change")
    if result.kept:
        lines.append(f"  kept {len(result.kept)} Codex row(s); the older build continues them:")
        lines += [f"    {v.store}  {v.row}  (login {str(v.chain_id or '')[:12]})" for v in result.kept]
    if result.to_remove:
        verb = "still holds" if done else "would remove"
        lines.append(f"  {verb} {len(result.to_remove)} Codex row(s) the older build must not use:")
        lines += [f"    {v.store}  {v.row}  ({v.verdict})" for v in result.to_remove]
    if result.fixes:
        lines.append(f"  {'still needs' if done else 'would switch'} Codex OFF for the older build in:")
        lines += [f"    {store}  ({fix})" for store, fix in result.fixes]
    if result.codex_off:
        lines.append("  Codex is OFF for the older build in these homes (no login of their own; the older build "
                     "would otherwise borrow the default profile's, unsafely):")
        lines += [f"    {store}" for store in result.codex_off]
    cli = result.codex_cli
    text = _CLI_TEXT.get(cli.get("status"), str(cli.get("status")))
    lines.append(f"  Codex CLI login {cli.get('path')}: " + text.format(detail=cli.get("detail", "")))
    if result.refusal:
        lines.append("  " + result.refusal)
    return lines


def rollback_prepare_command(args) -> None:
    from hermes_cli.auth_constants import AuthError
    confirmed = bool(getattr(args, "other_homes_checked", False))
    if getattr(args, "check", False):
        result = check_rollback(other_homes_checked=confirmed)
        for line in _describe(result, done=False):
            print(line)
        if result.blocking_ops:
            print(_blocking_message(result.blocking_ops))
        if result.safe:
            print("SAFE TO ROLL BACK: nothing to prepare.")
            return
        raise SystemExit("NOT READY: run `hermes auth rollback-prepare` (with this build) first.")
    try:
        result, report = prepare_rollback(other_homes_checked=confirmed)
    except AuthError as exc:
        raise SystemExit(str(exc)) from exc
    if report["removed"]:
        print(f"Removed {len(report['removed'])} Codex row(s) the older build must not use:")
        for item in report["removed"]:
            print(f"    {item['store']}  {item['row']}  ({item['reason']})")
    for line in _describe(result, done=True):
        print(line)
    if not result.safe:
        raise SystemExit("NOT SAFE TO ROLL BACK YET (see above). No receipt was written.")
    print(f"SAFE TO ROLL BACK. Receipt: {report['receipt']}")
    print("Stop every Hermes process before switching builds, never restore an auth.json backup or snapshot, "
          "and re-run `hermes auth rollback-prepare --check` right before the switch.")
