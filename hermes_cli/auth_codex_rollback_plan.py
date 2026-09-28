"""What an install must look like before a rollback to a build WITHOUT the Codex chain authority.

Deploy gate F8. The older build (``1b8cc8c1b5`` and before) reads every ``auth.json`` directly and
ignores the registry, fences, chain records and the retired-token index. Its own rules decide what
is safe to leave behind, and three of them are hazards on their own:

* **Aliases.** Its pool syncs ``device_code`` AND ``manual:device_code`` rows FROM the singleton
  before refreshing them, but writes only a refreshed ``device_code`` row back: a kept manual alias
  (or any second row of one login) is refreshed and then replayed through the singleton.
* **Borrowing.** A home other than the root with no Codex state of its own reads the root's
  (``_load_provider_state`` / ``read_credential_pool`` fall back to it). Its singleton path writes a
  refreshed pair into the BORROWING home and leaves the root holding the consumed one; its fork
  heal (``heal_forked_single_use_oauth_grants``) merges a profile login of the same account (or of
  an unknown one) into the root's and so turns that profile into a borrower.
* **Codex CLI import.** A missing, token-less or rejected login makes it import
  ``$CODEX_HOME/auth.json`` (default ``~/.codex``); several homes import the same pair.

So the end state this module plans (and verifies) is:

1. every Codex row that is not an owner row of an ACTIVE login holding its recorded current pair
   with no attempt in flight is removed (its token hashes kept in the retired-token index first);
2. per home, a login keeps its singleton plus at most ONE ``device_code`` row (a login without a
   singleton keeps one row), and a home that keeps a singleton keeps no other
   ``device_code`` / ``manual:device_code`` row;
3. a profile login the older build's heal would merge into the root's is removed;
4. every home other than the root holds its own login or has Codex switched OFF for the older
   build: an EMPTY provider section (read as "no login" — no root fallback, no Codex CLI import,
   ignored by the heal) and one inert, key-less pool row (no pool fallback, never leased); the
   root holds no token-less provider section (a Codex CLI import trigger);
5. no importable Codex CLI pair exists;
6. the inventory is complete: every home is listed and read strictly, the registry is readable,
   and a registry that was ever damaged (or is gone after use) is accepted only with the
   operator's confirmation that no other Hermes home under this root holds a Codex login.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_cli import auth_codex_rows as rows_mod
from hermes_cli.auth_codex_records import read_record
from hermes_cli.auth_codex_registry import (
    OPS_DIR_NAME, PROVIDER, RECORDS_DIR_NAME, REGISTRY_NAME, SINGLETON, STATE_ACTIVE, CodexAuthorityRefused,
    RegistryView, active_store_path, profiles_root, root_home, root_store_path, store_path_of)
from hermes_cli.auth_credential_io import isdir_strict, lexists_strict, listdir_strict, read_witnessed_bytes, store_key

KEEP = "keep"
UNREGISTERED_NO_REGISTRY = "not registered yet (the preparation registers it by its token material first)"
DEVICE_CODE, MANUAL_DEVICE_CODE = "device_code", "manual:device_code"
CODEX_OFF_SOURCE = "manual:rollback-codex-off"
CODEX_OFF_ID = "rbkoff"


def codex_off_row() -> Dict[str, Any]:
    """Inert pool row: an API-key row with no key is never leased by the older build (and never
    refreshed); its presence stops the pool falling back to the root's rows."""
    return {"id": CODEX_OFF_ID, "label": "Codex is off here for the older Hermes build: sign in to use it",
            "auth_type": "api_key", "source": CODEX_OFF_SOURCE, "access_token": "", "priority": 0,
            "request_count": 0}


class InventoryIncomplete(CodexAuthorityRefused):
    """The preparation cannot prove it sees every Codex login the older build could read."""


@dataclass
class Home:
    key: str                  # store key of its auth.json
    path: Path                # its auth.json (may not exist)
    store: Dict[str, Any]     # parsed content ({} when absent)
    is_root: bool


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
    fixes: List[Tuple[str, str]] = field(default_factory=list)   # (store key, "codex-off" | "drop-tokenless")
    blocking_ops: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    codex_cli: Dict[str, Any] = field(default_factory=dict)
    refusal: Optional[str] = None
    codex_off: List[str] = field(default_factory=list)       # homes (by store key) left with no login

    @property
    def to_remove(self) -> List[RowVerdict]:
        return [v for v in self.rows if v.verdict != KEEP]

    @property
    def kept(self) -> List[RowVerdict]:
        return [v for v in self.rows if v.verdict == KEEP]

    @property
    def safe(self) -> bool:
        return (self.refusal is None and not self.blocking_ops and not self.to_remove and not self.fixes
                and self.codex_cli.get("status") in ("absent", "ignored"))


# ── inventory (strict: anything that cannot be inspected refuses) ─────────────────────────────

def read_store_strict(path: Path) -> Dict[str, Any]:
    """Parse *path* WITHOUT side effects ({} when absent). Unreadable or unparseable raises
    :class:`InventoryIncomplete` — never "no login" (the normal loader keeps a ``.corrupt`` copy and
    returns an empty store)."""
    try:
        raw, _sha = read_witnessed_bytes(path)
    except OSError as exc:
        raise InventoryIncomplete(f"Hermes can't read {path} ({exc}); fix access to it, then run this again.") from None
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise InventoryIncomplete(
            f"{path} is not valid JSON, so Hermes can't tell which Codex login it holds. Repair or move it "
            "(the older build would read it as empty), then run this again.")
    return data


def _strict(what: str, fn, *args):
    try:
        return fn(*args)
    except OSError as exc:
        raise InventoryIncomplete(f"Hermes can't inspect {what} ({exc}); fix access to it, then run this again.") from None


def home_paths(view: RegistryView) -> List[Path]:
    """Every auth.json the older build could read for this root: the root, the active home, EVERY
    directory under ``profiles/`` (listed strictly, plus the profile enumeration itself, whose
    failure refuses), and every home the registry names (a registered home outside ``profiles/``)."""
    from hermes_cli.profiles import _iter_named_profile_dirs
    paths = [root_store_path(), active_store_path()]
    directory = profiles_root()
    for name in _strict(f"the profiles directory {directory}", listdir_strict, directory):
        entry = directory / name
        if not name.startswith(".") and _strict(f"profile {entry}", isdir_strict, entry):
            paths.append(entry / "auth.json")
    try:
        paths.extend(Path(d) / "auth.json" for d in _iter_named_profile_dirs())
    except OSError as exc:
        raise InventoryIncomplete(f"Hermes can't list the profiles ({exc}); fix access, then run this again.") from None
    named = {e.get("owner") for e in view.chains.values()} | {
        r.get("store") for e in view.chains.values() for r in e.get("rows", ())}
    for op in view.ops.values():
        named |= set(op.get("stores") or ())
    for key in sorted(k for k in named if k):
        path = store_path_of(key)
        if _strict(f"the registered home {path.parent}", lexists_strict, path.parent):
            paths.append(path)
    seen, out = set(), []
    for p in paths:
        k = store_key(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def load_homes(paths: List[Path]) -> Dict[str, Home]:
    root = store_key(root_store_path())
    return {store_key(p): Home(store_key(p), p, read_store_strict(p), store_key(p) == root) for p in paths}


def protocol_traces(paths: List[Path]) -> bool:
    """The new build governed Codex here (records, ops, the retired index, a damaged registry copy)."""
    from hermes_cli.auth_codex_evidence import retired_index_path
    root = root_home()
    candidates = [root / OPS_DIR_NAME, retired_index_path()] + [Path(p).parent / RECORDS_DIR_NAME for p in paths]
    return any(_strict(str(c), lexists_strict, c) for c in candidates) or registry_was_damaged()


def registry_was_damaged() -> bool:
    import fnmatch
    root = root_home()
    return any(fnmatch.fnmatch(n, f"{REGISTRY_NAME}.corrupt.*") for n in _strict(str(root), listdir_strict, root))


# ── rows ──────────────────────────────────────────────────────────────────────────────────────

def material_rows(store: Dict[str, Any]) -> List[str]:
    """Every Codex row holding ANY token material; id-less pool rows as ``pool-index:<n>``."""
    out = []
    if any(rows_mod.row_tokens(store, SINGLETON)):
        out.append(SINGLETON)
    for n, entry in enumerate(rows_mod.pool_entries(store) or ()):
        if isinstance(entry, dict) and (str(entry.get("access_token") or "").strip()
                                        or str(entry.get("refresh_token") or "").strip()):
            out.append(rows_mod.pool_row(str(entry["id"])) if entry.get("id") else f"pool-index:{n}")
    return out


def row_material(store: Dict[str, Any], row: str) -> Tuple[str, str]:
    if row.startswith("pool-index:"):
        entries = [e for e in rows_mod.pool_entries(store) or ()]
        entry = entries[int(row.split(":", 1)[1])]
        return str(entry.get("access_token") or "").strip(), str(entry.get("refresh_token") or "").strip()
    return rows_mod.row_tokens(store, row)


def _source(store: Dict[str, Any], row: str) -> Optional[str]:
    return None if row == SINGLETON else (rows_mod.get_row(store, row) or {}).get("source")


def basic_verdict(view: RegistryView, key: str, store: Dict[str, Any], row: str,
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


def _one_row_per_login(home: Home, verdicts: List[RowVerdict]) -> List[RowVerdict]:
    """Rule 2: what the older build's pool keeps in step with its singleton."""
    store = home.store
    kept = [v for v in verdicts if v.verdict == KEEP]
    singleton = next((v for v in kept if v.row == SINGLETON), None)
    chosen = set()
    if singleton is not None:
        chosen.add(SINGLETON)
        aliases = sorted(v.row for v in kept if v.chain_id == singleton.chain_id and v.row != SINGLETON
                         and _source(store, v.row) == DEVICE_CODE)
        if aliases:
            chosen.add(aliases[0])
    for cid in sorted({v.chain_id for v in kept if singleton is None or v.chain_id != singleton.chain_id}):
        rows = [v.row for v in kept if v.chain_id == cid]
        if singleton is not None:
            rows = [r for r in rows if _source(store, r) not in (DEVICE_CODE, MANUAL_DEVICE_CODE)]
        rows.sort(key=lambda r: (_source(store, r) == DEVICE_CODE, r))
        if rows:
            chosen.add(rows[0])
    out = []
    for v in verdicts:
        if v.verdict == KEEP and v.row not in chosen:
            why = ("an alias the older build syncs from the singleton but never writes back"
                   if singleton is not None and _source(store, v.row) in (DEVICE_CODE, MANUAL_DEVICE_CODE)
                   else "a second row of one login")
            v = RowVerdict(v.store, v.row, why, v.chain_id)
        out.append(v)
    return out


def _identity(entry: Dict[str, Any]) -> Optional[str]:
    """The account identity the older build's fork heal compares (``_oauth_identity``)."""
    from hermes_cli.auth_constants import _decode_jwt_claims
    for token in (entry.get("access_token"), entry.get("id_token")):
        claims = _decode_jwt_claims(token) if token else {}
        if not claims:
            continue
        nested = claims.get("https://api.openai.com/auth")
        account = nested.get("chatgpt_account_id") if isinstance(nested, dict) else None
        for value in (account, claims.get("sub"), claims.get("email")):
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _flat(store: Dict[str, Any], row: str) -> Dict[str, Any]:
    if row == SINGLETON:
        state = rows_mod.singleton_state(store) or {}
        tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
        return {"id": None, **tokens}
    return dict(rows_mod.get_row(store, row) or {})


def heal_would_merge(profile_rows: List[Dict[str, Any]], root_rows: List[Dict[str, Any]]) -> bool:
    """Conservative superset of the older build's heal: it leaves a profile login alone only when
    both sides carry a KNOWN account identity, they differ, and no pool id or token is shared."""
    if not profile_rows or not root_rows:
        return False
    root_ids = {r.get("id") for r in root_rows if r.get("id")}
    root_idents = [_identity(r) for r in root_rows]
    root_tokens = {t for r in root_rows for t in (r.get("access_token"), r.get("refresh_token")) if t}
    for p in profile_rows:
        ident = _identity(p)
        if (p.get("id") and p["id"] in root_ids) or ident is None or any(r is None or r == ident for r in root_idents):
            return True
        if {p.get("access_token"), p.get("refresh_token")} & root_tokens:
            return True
    return False


def _section(store: Dict[str, Any]) -> Any:
    providers = store.get("providers")
    return providers.get(PROVIDER) if isinstance(providers, dict) else None


def needs_codex_off(home: Home, kept_rows: List[str]) -> bool:
    """Rule 4 for a home other than the root, judged on the state AFTER its removals."""
    singleton_kept = SINGLETON in kept_rows
    pool_kept = any(r != SINGLETON for r in kept_rows)
    section = _section(home.store)
    section_ok = singleton_kept or (section == {} and not any(rows_mod.row_tokens(home.store, SINGLETON)))
    off_row = any(isinstance(e, dict) and e.get("source") == CODEX_OFF_SOURCE for e in rows_mod.pool_entries(home.store) or ())
    return not (section_ok and (pool_kept or off_row))


def root_has_tokenless_section(home: Home) -> bool:
    section = _section(home.store)
    return isinstance(section, dict) and bool(section) and not any(rows_mod.row_tokens(home.store, SINGLETON))


# ── the assessment ────────────────────────────────────────────────────────────────────────────

def assess(view: RegistryView, homes: Dict[str, Home], *, other_homes_checked: bool) -> Assessment:
    """Classify every row and home. Records are read here: the caller holds the owner store locks
    (or is the read-only ``--check``, which takes none)."""
    out = Assessment(view.status)
    paths = [h.path for h in homes.values()]
    if view.status == "unreadable":
        out.refusal = ("The Codex login registry is damaged, so Hermes can't know every Hermes home that held "
                       "a Codex login. Run `hermes auth migrate --rebuild`, then run this again.")
        return out
    traces = protocol_traces(paths)
    if view.status == "absent" and not traces:
        unregistered = [RowVerdict(h.key, r, UNREGISTERED_NO_REGISTRY) for h in homes.values()
                        for r in material_rows(h.store)]
        if unregistered:
            # Never governed, but Codex material exists: nothing identifies which rows are one login.
            # The preparation first registers them by their token material (the bootstrap migration:
            # identical pairs across every home are one login, ambiguous ones are paused), then
            # applies the same keep rules; until then nothing is certified.
            out.rows = unregistered
            return out
    if ((view.status == "absent" and traces) or registry_was_damaged()) and not other_homes_checked:
        out.refusal = ("The Codex login registry was damaged or removed after this build used it, so it may not list "
                       "every Hermes home that held a Codex login (a HERMES_HOME outside profiles/ is found only "
                       "through it). Remove the Codex login from any such home yourself, then run this again with "
                       "--other-homes-checked.")
        return out
    out.blocking_ops = list(view.ops.items())
    records = {cid: read_record(store_path_of(e["owner"]), cid) for cid, e in view.chains.items()
               if e.get("state") == STATE_ACTIVE} if view.ok else {}
    by_home: Dict[str, List[RowVerdict]] = {}
    for key, home in homes.items():
        verdicts = [basic_verdict(view, key, home.store, r, records) if view.ok
                    else RowVerdict(key, r, "authority registry absent") for r in material_rows(home.store)]
        by_home[key] = _one_row_per_login(home, verdicts)
    root = next((h for h in homes.values() if h.is_root), None)
    root_rows = [_flat(root.store, v.row) for v in by_home.get(root.key, []) if v.verdict == KEEP] if root else []
    for key, home in homes.items():
        if home.is_root:
            continue
        kept = [v for v in by_home[key] if v.verdict == KEEP]
        if heal_would_merge([_flat(home.store, v.row) for v in kept], root_rows):
            by_home[key] = [RowVerdict(v.store, v.row, "the older build would merge this login into the default "
                                                     "profile's (same or unknown account)", v.chain_id)
                            if v.verdict == KEEP else v for v in by_home[key]]
    for key, home in homes.items():
        out.rows.extend(by_home[key])
        kept_rows = [v.row for v in by_home[key] if v.verdict == KEEP]
        if home.is_root:
            if root_has_tokenless_section(home):
                out.fixes.append((key, "drop-tokenless"))
            continue
        if needs_codex_off(home, kept_rows):
            out.fixes.append((key, "codex-off"))
        if not kept_rows:
            out.codex_off.append(key)
    return out


# ── the Codex CLI login the older build imports ───────────────────────────────────────────────

def codex_cli_auth_path() -> Path:
    """The file the older build's ``_import_codex_cli_tokens`` reads (same resolution)."""
    return Path(os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")).expanduser() / "auth.json"


def codex_cli_verdict() -> Dict[str, Any]:
    """``absent`` / ``ignored`` (no pair, or an access token already expired — it only gets older):
    the older build imports nothing. ``importable``: it WOULD import this pair into every home whose
    login is missing, token-less or rejected — blocking. ``unreadable``: blocking."""
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
    if _codex_access_token_is_expiring(str(tokens["access_token"]).strip(), 0):
        return {**out, "status": "ignored"}
    return {**out, "status": "importable"}
