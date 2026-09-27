#!/usr/bin/env python3
"""Pre-deploy backup of a Hermes install, verified by restoring it (deploy-gate checklist F7).

Backs up the root home and EVERY profile home (each directory under ``profiles/`` except the ``.locks``
runtime directory, homes the credential registry names, ``--extra-home``):

* every SQLite database (``state.db``, the root ``compaction.db``, plugin stores, ...) as a SQLite
  backup-API snapshot, WAL content included, never a raw database file;
* ``skills/``, ``config.yaml`` and ``.env`` (a deploy migrates config; a rollback needs the old copy);
* the credential-protocol state, located as the protocol locates it: every ``auth.*`` file of a home
  (``*.lock`` runtime locks excluded), ``auth.ops/``, and for every credential store (each home's
  ``auth.json`` and each store the registry names as a chain owner, a row's store or an op's store,
  whatever its file name) the store itself, its ``auth.recovery/`` records and its rescue file, beside
  both the named path and its canonical target;

and the desktop's data directory (``%APPDATA%\\Hermes``: saved windows, window state, connections,
renderer storage), minus Chromium caches.

It then restores the backup into a scratch directory and checks every file against the manifest (SHA-256,
size) and every database with ``PRAGMA integrity_check`` and per-table row counts. A backup verifies only
when it is COMPLETE: anything the inventory could not read or copy (an unreadable directory or registry, a
``.db`` that is not SQLite, a database SQLite rejects, a missing ``--extra-home``) is a recorded failure
that ``--verify`` keeps reporting. Unreadable database bytes are kept as evidence, never as a verified
database.

Sources are only read as files: SQLite never opens a source database (it would update the source's
``-shm`` index even read-only); its main file, WAL and rollback journal are copied into private staging
and snapshotted there, and a source whose size or modification time moves during that copy fails the run.
Stop Hermes (desktop, backend, gateway, relay) first. ``--out`` must be a new directory outside every
source. Credential copies are ordinary new files (a store that is a link or alias is copied by content);
the backup holds secrets: keep ``--out`` private.

    python scripts/predeploy_backup.py --plan                       # list the sources, read nothing
    python scripts/predeploy_backup.py --out D:\\hermes-predeploy   # back up, then verify
    python scripts/predeploy_backup.py --verify D:\\hermes-predeploy
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.backup import _EXCLUDED_BACKUP_ROOT_DIRS, _EXCLUDED_DIRS  # noqa: E402
from hermes_constants import LOCAL_RUNTIME_ROOT_DIRS  # noqa: E402

MANIFEST = "manifest.json"
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_NOT_HERMES_DBS = {"thumbs.db"}  # Windows Explorer's thumbnail cache, not SQLite
_SQLITE_MAGIC = b"SQLite format 3\x00"
_DB_SIDECARS = ("-wal", "-journal")  # copied with the database; -shm is rebuilt from the WAL
# ``hermes backup``'s exclusions: dependency/cache trees anywhere; runtime downloads and browser profiles
# at a home's top level. The root's code checkout is reinstalled and its profiles are homes of their own.
_HOME_TOP_EXCLUDED = {*LOCAL_RUNTIME_ROOT_DIRS, *_EXCLUDED_BACKUP_ROOT_DIRS}
_ROOT_TOP_EXCLUDED = {*_HOME_TOP_EXCLUDED, "hermes-agent", "profiles"}
_PROFILE_LOCKS_DIR = ".locks"  # <root>/profiles/.locks: lifecycle locks, runtime state
# The credential protocol's names (stage-1: hermes_cli/auth_codex_registry.py).
_REGISTRY = "auth.chains.json"
_REGISTRY_VERSION = 1
_CHAIN_STATES = ("active", "fenced", "retired")
_RECORDS_DIR = "auth.recovery"
_OPS_DIR = "auth.ops"
_RESCUE = "auth.codex-rotation-rescue.json"
_CONFIG_FILES = ("config.yaml", ".env")
# Chromium's regenerable caches inside the Electron userData directory.
_DESKTOP_CACHE_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                       "ShaderCache", "GrShaderCache", "Crashpad", "component_crx_cache"}


# --- inventory -------------------------------------------------------------------------------------

def default_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def default_desktop_data() -> Optional[Path]:
    override = os.environ.get("HERMES_DESKTOP_USER_DATA_DIR")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Hermes" if appdata else None


def _key(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _under(path: Path, parent: Path) -> Optional[str]:
    """*path* relative to *parent* (both canonical) as POSIX text, or None."""
    try:
        return Path(os.path.realpath(path)).relative_to(os.path.realpath(parent)).as_posix()
    except ValueError:
        return None


@dataclass
class Item:
    kind: str  # sqlite | file | unrecognized
    source: Path
    rel: str
    aliases: List[str] = field(default_factory=list)


@dataclass
class Inventory:
    homes: List[Tuple[str, Path]] = field(default_factory=list)
    items: Dict[str, Item] = field(default_factory=dict)
    failures: List[str] = field(default_factory=list)

    def locate(self, path: Path, anchor: Path) -> str:
        """Backup path: under the deepest home that contains *path*, else beside its *anchor* directory."""
        for label, home in sorted(self.homes, key=lambda h: -len(os.path.realpath(h[1]))):
            if (rel := _under(path, home)) is not None:
                return f"homes/{label}/{rel}"
        tag = hashlib.sha1(_key(anchor).encode("utf-8")).hexdigest()[:12]
        return f"external/{tag}-{anchor.name}/{_under(path, anchor) or path.name}"

    def add(self, kind: str, path: Path, anchor: Path) -> None:
        key = _key(path)
        if key in self.items:
            if str(path) not in (str(self.items[key].source), *self.items[key].aliases):
                self.items[key].aliases.append(str(path))
            return
        canonical = Path(os.path.realpath(path))
        item = Item(kind, canonical, self.locate(canonical, anchor))
        if str(path) != str(canonical):
            item.aliases.append(str(path))
        self.items[key] = item


def _walk(base: Path, *, excluded: set, top_excluded: set = frozenset()) -> Iterator[Path]:
    """Regular files under *base*, skipping excluded directory names (and top-level ones). An unreadable
    directory raises: an incomplete backup must not pass as complete."""
    def fail(exc: OSError) -> None:
        raise exc

    for dirpath, dirnames, filenames in os.walk(base, onerror=fail):
        top = Path(dirpath) == base
        dirnames[:] = [d for d in dirnames if d not in excluded and not (top and d in top_excluded)]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                yield path


def _is_sqlite(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC


def _validate_registry(data) -> None:
    """The registry structure the credential protocol accepts (stage-1 ``validate_registry``, mirrored until
    that code is on this branch); anything else is unreadable, so the stores it names are unknown."""
    if not isinstance(data, dict) or data.get("version") != _REGISTRY_VERSION:
        raise ValueError("bad version")
    if not isinstance(data.get("ops"), dict) or not isinstance(data.get("chains"), dict):
        raise ValueError("bad sections")
    if not isinstance(data.get("legacy_rescues", {}), dict):
        raise ValueError("bad legacy_rescues")
    seen = {}
    for cid, entry in data["chains"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"chain {cid} is not an object")
        if entry.get("state") not in _CHAIN_STATES:
            raise ValueError(f"chain {cid} has a bad state")
        if not isinstance(entry.get("epoch"), int) or not isinstance(entry.get("owner"), str):
            raise ValueError(f"chain {cid} lacks epoch/owner")
        if not isinstance(entry.get("rows"), list):
            raise ValueError(f"chain {cid} rows")
        for row in entry["rows"]:
            key = (row.get("store"), row.get("row")) if isinstance(row, dict) else (None, None)
            if not all(isinstance(k, str) for k in key):
                raise ValueError(f"chain {cid} has a malformed row")
            if key in seen:
                raise ValueError(f"row {key} is in chains {seen[key]} and {cid}")
            seen[key] = cid
    for oid, op in data["ops"].items():
        stores = op.get("stores", []) if isinstance(op, dict) else None
        if not isinstance(stores, list) or not all(isinstance(s, str) for s in stores):
            raise ValueError(f"op {oid} has malformed stores")


def _registry_paths(root: Path, inv: Inventory) -> Tuple[List[Path], List[Path]]:
    """``(stores, rescue files)`` the credential registry names: chain owners, their rows' stores, unfinished
    ops' stores, recorded legacy rescues and their stores. Absent registry: none. Present but unreadable or
    malformed: a failure (the stores it names cannot be known)."""
    path = root / _REGISTRY
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _validate_registry(data)
    except (OSError, ValueError) as exc:
        inv.failures.append(f"credential registry unreadable ({exc}), so the stores it names are unknown: {path}")
        return [], []
    stores = []
    for chain in data["chains"].values():
        stores += [chain["owner"], *(row["store"] for row in chain["rows"])]
    for op in data["ops"].values():
        stores += op.get("stores", [])
    rescues = data.get("legacy_rescues", {})
    stores += [r["store"] for r in rescues.values() if isinstance(r, dict) and isinstance(r.get("store"), str)]
    return ([Path(s) for s in dict.fromkeys(s for s in stores if s)],
            [Path(r) for r in rescues if isinstance(r, str) and r])


def _add_home(inv: Inventory, label: str, home: Path) -> None:
    top = _ROOT_TOP_EXCLUDED if label == "root" else _HOME_TOP_EXCLUDED
    for path in _walk(home, excluded=_EXCLUDED_DIRS, top_excluded=top):
        if path.suffix.lower() in _DB_SUFFIXES and path.name.lower() not in _NOT_HERMES_DBS:
            if _is_sqlite(path):
                inv.add("sqlite", path, home)
            else:
                inv.add("unrecognized", path, home)
                inv.failures.append(f"not a SQLite database (zeroed or corrupt?), kept as evidence only: {path}")
    if (home / "skills").is_dir():
        for path in _walk(home / "skills", excluded=_EXCLUDED_DIRS):
            if _key(path) not in inv.items:
                inv.add("file", path, home)
    for path in sorted(home.glob("auth.*")):
        if path.is_file() and not path.name.endswith(".lock"):
            inv.add("file", path, home)
    if (home / _OPS_DIR).is_dir():
        for path in _walk(home / _OPS_DIR, excluded=set()):
            inv.add("file", path, home)
    for name in _CONFIG_FILES:
        if (home / name).is_file():
            inv.add("file", home / name, home)


def _add_store(inv: Inventory, store: Path) -> None:
    """A credential store (present or not) with its records and rescue file, beside the named path and its
    canonical target (the protocol keeps records beside the canonical file and looks at every alias)."""
    canonical = Path(os.path.realpath(store))
    for base in dict.fromkeys((store, canonical)):
        if base.is_file():
            inv.add("file", base, base.parent)
        if (rescue := base.parent / _RESCUE).is_file():
            inv.add("file", rescue, base.parent)
        if (records := base.parent / _RECORDS_DIR).is_dir():
            for path in _walk(records, excluded=set()):
                inv.add("file", path, base.parent)


def build_inventory(root: Path, extra: List[Path], desktop: Optional[Path]) -> Inventory:
    inv = Inventory(homes=[("root", root)])
    try:
        profiles = root / "profiles"
        if profiles.is_dir():
            inv.homes += [(f"profiles/{entry.name}", entry) for entry in sorted(profiles.iterdir())
                          if entry.is_dir() and entry.name != _PROFILE_LOCKS_DIR]
        stores, rescues = _registry_paths(root, inv)
        known = {_key(home) for _, home in inv.homes}
        # A registered store's directory is a home of its own when it looks like one.
        registered = [s.parent for s in stores if (s.parent / "config.yaml").is_file() or (s.parent / "state.db").is_file()]
        for index, home in enumerate(dict.fromkeys([*registered, *extra])):
            if not home.is_dir():
                inv.failures.append(f"home not found: {home}")
            elif _key(home) not in known:
                known.add(_key(home))
                inv.homes.append((f"extra/{index}-{home.name}", home))
        for label, home in inv.homes:
            _add_home(inv, label, home)
        for store in [*(home / "auth.json" for _, home in inv.homes), *stores]:
            _add_store(inv, store)
        for rescue in rescues:
            if rescue.is_file():
                inv.add("file", rescue, rescue.parent)
        if desktop is not None and desktop.is_dir():
            # A home may live inside the desktop's data directory (HERMES_DESKTOP_USER_DATA_DIR/hermes-home):
            # the home owns its subtree (databases stay snapshots, its exclusions and sidecars apply).
            for path in _walk(desktop, excluded=_DESKTOP_CACHE_DIRS):
                if not any(_under(path, home) is not None for _, home in inv.homes):
                    inv.items[_key(path)] = Item("file", path, f"desktop/{path.relative_to(desktop).as_posix()}")
    except OSError as exc:
        inv.failures.append(f"inventory incomplete ({exc})")
    return inv


# --- backup ----------------------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_counts(path: Path) -> Dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {}
        for name in names:
            try:
                counts[name] = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.DatabaseError:  # virtual tables whose module is not loaded (e.g. an FTS tokenizer)
                counts[name] = -1
        return counts
    finally:
        conn.close()


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return "; ".join(str(row[0]) for row in conn.execute("PRAGMA integrity_check"))
    finally:
        conn.close()


def _stamps(paths: List[Path]) -> List[Optional[Tuple[int, int]]]:
    out = []
    for path in paths:
        try:
            st = path.stat()
            out.append((st.st_size, st.st_mtime_ns))
        except FileNotFoundError:
            out.append(None)
    return out


def _unlink_db(path: Path) -> None:
    for name in (path.name, *(path.name + s for s in (*_DB_SIDECARS, "-shm"))):
        (path.parent / name).unlink(missing_ok=True)


def _snapshot_db(source: Path, dest: Path, staging: Path) -> Optional[str]:
    """Snapshot *source* into *dest* (rollback-journal mode, self-contained); None, or why it failed. The
    source is only read as files; SQLite recovers its WAL/journal in the private staged copy."""
    group = [source, *(source.with_name(source.name + s) for s in _DB_SIDECARS)]
    before = _stamps(group)
    if before[0] is None:
        return "the database file disappeared after the inventory (stop Hermes first)"
    staged = staging / "snapshot.db"
    try:
        for path, stamp in zip(group, before):
            if stamp is not None:
                shutil.copyfile(path, staged.with_name(staged.name + path.name[len(source.name):]))
        if _stamps(group) != before:
            return "changed while being copied (stop Hermes first)"
        # mode=rw: never let SQLite create an empty database in place of a missing copy.
        src, dst = sqlite3.connect(f"file:{staged}?mode=rw", uri=True), sqlite3.connect(dest)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # a self-contained file: no -wal/-shm on every open
        finally:
            dst.close()
            src.close()
        return None
    except (OSError, sqlite3.DatabaseError) as exc:
        _unlink_db(dest)
        return f"SQLite cannot read it ({exc})"
    finally:
        if staged.exists() and not dest.exists():  # keep the raw bytes of a failed database as evidence
            shutil.copyfile(staged, dest.with_name(dest.name + ".unreadable"))
        _unlink_db(staged)


def _inside(path: Path, parent: Path) -> bool:
    return _under(path, parent) is not None


def backup(out: Path, inv: Inventory, desktop: Optional[Path]) -> dict:
    for source in [*(home for _, home in inv.homes), *([desktop] if desktop else [])]:
        if _inside(out, source) or _inside(source, out):
            raise SystemExit(f"--out {out} overlaps the source {source}; choose a directory outside the install")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"--out {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        out.chmod(0o700)
    entries, failures = [], list(inv.failures)
    with tempfile.TemporaryDirectory(prefix="hermes-backup-staging-") as staging:
        for item in inv.items.values():
            dest = out / item.rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            entry = {"kind": item.kind, "source": str(item.source), "path": item.rel, "aliases": item.aliases}
            if item.kind == "sqlite":
                if (error := _snapshot_db(item.source, dest, Path(staging))) is not None:
                    failures.append(f"database not backed up, {error}: {item.source}")
                    evidence = dest.with_name(dest.name + ".unreadable")
                    if evidence.exists():
                        entries.append({**entry, "kind": "unrecognized", "path": item.rel + ".unreadable",
                                        "size": evidence.stat().st_size, "sha256": _sha256(evidence)})
                    continue
                entry["tables"] = _table_counts(dest)  # integrity_check runs on the restored copy (verify)
            else:
                try:
                    shutil.copyfile(item.source, dest)  # content only: never links, never source metadata
                except OSError as exc:
                    failures.append(f"file not copied ({exc}; stop the desktop first): {item.source}")
                    continue
            entry.update(size=dest.stat().st_size, sha256=_sha256(dest))
            entries.append(entry)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(),
                "homes": [{"label": label, "path": str(home)} for label, home in inv.homes],
                "desktop": str(desktop) if desktop else None, "entries": entries, "failures": failures}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# --- verification (restore into scratch) -----------------------------------------------------------

def verify(out: Path) -> List[str]:
    """Restore every manifest entry into a scratch directory and check it; the list of problems, starting
    with every failure recorded while the backup was taken (an incomplete backup never verifies)."""
    manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    problems = [f"backup incomplete: {failure}" for failure in manifest.get("failures", ["no failure record"])]
    listed = {entry["path"] for entry in manifest["entries"]}
    on_disk = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name != MANIFEST}
    problems += [f"not in the manifest: {rel}" for rel in sorted(on_disk - listed)]
    with tempfile.TemporaryDirectory(prefix="hermes-restore-check-") as scratch:
        for entry in manifest["entries"]:
            src, restored = out / entry["path"], Path(scratch) / entry["path"]
            if not src.is_file():
                problems.append(f"missing from the backup: {entry['path']}")
                continue
            restored.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, restored)
            if restored.stat().st_size != entry["size"] or _sha256(restored) != entry["sha256"]:
                problems.append(f"restored copy differs from the manifest: {entry['path']}")
                continue
            if entry["kind"] == "sqlite":
                try:
                    integrity, tables = _integrity(restored), _table_counts(restored)
                except sqlite3.DatabaseError as exc:
                    problems.append(f"restored database cannot be read ({exc}): {entry['path']}")
                    continue
                if integrity != "ok":
                    problems.append(f"restored database fails integrity_check ({integrity}): {entry['path']}")
                if tables != entry["tables"]:
                    problems.append(f"restored database row counts differ: {entry['path']}")
    return problems


# --- CLI -------------------------------------------------------------------------------------------

def _plan(root: Path, extra: List[Path], desktop: Optional[Path]) -> None:
    print("Would read (paths only; nothing is opened in --plan):")
    for label, home in [("root", root), *((f"extra/{i}-{h.name}", h) for i, h in enumerate(extra))]:
        print(f"  [{label}] {home}")
    print(f"  [profiles] every directory under {root / 'profiles'} except {_PROFILE_LOCKS_DIR}")
    print(f"  [registry] {root / _REGISTRY}: the stores it names, their {_RECORDS_DIR}/ and {_RESCUE}")
    print(f"      per home: every *.db / *.sqlite(3) (copied as files, snapshotted in staging), skills/, "
          f"auth.* (not *.lock), {_OPS_DIR}/, {_RECORDS_DIR}/, {', '.join(_CONFIG_FILES)}")
    print(f"  [desktop] {desktop if desktop else '(no APPDATA)'}  minus {', '.join(sorted(_DESKTOP_CACHE_DIRS))}")


def _report(problems: List[str]) -> int:
    for problem in problems:
        print(f"FAIL {problem}")
    print("VERIFIED" if not problems else f"NOT VERIFIED ({len(problems)} problems)")
    return 0 if not problems else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, help="Hermes root home (default: this machine's)")
    parser.add_argument("--desktop-data", type=Path, help="desktop userData directory (default: %%APPDATA%%\\Hermes)")
    parser.add_argument("--extra-home", type=Path, action="append", default=[],
                        help="a profile home outside <root>/profiles (repeatable)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", type=Path, help="new, empty directory outside the install: back up, then verify")
    mode.add_argument("--verify", type=Path, help="re-verify an existing backup directory")
    mode.add_argument("--plan", action="store_true", help="list the source paths, read nothing")
    args = parser.parse_args(argv)

    if args.verify:
        return _report(verify(args.verify))
    root = args.root or default_root()
    desktop = args.desktop_data or default_desktop_data()
    if args.plan:
        _plan(root, args.extra_home, desktop)
        return 0
    inv = build_inventory(root, args.extra_home, desktop)
    manifest = backup(args.out, inv, desktop)
    kinds: Dict[str, int] = {}
    for entry in manifest["entries"]:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    print(f"Backed up {len(manifest['entries'])} files from {len(inv.homes)} homes to {args.out}: "
          + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())))
    return _report(verify(args.out))


if __name__ == "__main__":
    sys.exit(main())
