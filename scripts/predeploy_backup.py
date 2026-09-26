#!/usr/bin/env python3
"""Pre-deploy backup of a Hermes install, verified by restoring it (deploy-gate checklist F7).

Backs up, for the root home and EVERY profile home (``profiles/<name>/``, plus homes the credential
registry names and ``--extra-home``):

* every SQLite database (``state.db``, the root ``compaction.db``, plugin stores, ...), copied with
  SQLite's backup API (a consistent snapshot that includes WAL content), never a raw file copy;
* ``skills/``;
* the credential-protocol state: every ``auth.*`` file (``auth.json``, the ``auth.chains*`` registry and
  its preserved copies, rescue files; ``*.lock`` runtime locks excluded) and the ``auth.recovery/``
  records and ``auth.ops/`` journal;
* ``config.yaml`` and ``.env`` (a deploy migrates config; rolling back needs the pre-deploy copy);

and the desktop's data directory (``%APPDATA%\\Hermes``: saved windows, window state, connections,
renderer storage), minus Chromium caches.

It then restores the backup into a scratch directory and checks every file against the manifest
(SHA-256 and size) and every database with ``PRAGMA integrity_check`` and per-table row counts. Exit 0
only when everything verified.

Sources are only read: databases are opened ``mode=ro``, files are read, nothing is created, moved or
locked in them, and ``--out`` must lie outside every source. Stop Hermes (desktop, backend, gateway,
relay) first: a database held busy past the timeout, or a file the desktop keeps open, is reported and
fails the run rather than being skipped. Credential copies are ordinary new files (a store that is a hard
link is copied by content), and the backup holds secrets: keep ``--out`` private.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.backup import _EXCLUDED_BACKUP_ROOT_DIRS, _EXCLUDED_DIRS, _safe_copy_db  # noqa: E402
from hermes_constants import LOCAL_RUNTIME_ROOT_DIRS  # noqa: E402

MANIFEST = "manifest.json"
_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
_SQLITE_MAGIC = b"SQLite format 3\x00"
# ``hermes backup``'s exclusions: dependency/cache trees anywhere; runtime downloads and browser profiles
# at a home's top level. The root's code checkout is reinstalled and its profiles are homes of their own.
_HOME_TOP_EXCLUDED = {*LOCAL_RUNTIME_ROOT_DIRS, *_EXCLUDED_BACKUP_ROOT_DIRS}
_ROOT_TOP_EXCLUDED = {*_HOME_TOP_EXCLUDED, "hermes-agent", "profiles"}
_CREDENTIAL_DIRS = ("auth.recovery", "auth.ops")
_CONFIG_FILES = ("config.yaml", ".env")
# Chromium's regenerable caches inside the Electron userData directory.
_DESKTOP_CACHE_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                       "ShaderCache", "GrShaderCache", "Crashpad", "component_crx_cache"}


# --- sources -------------------------------------------------------------------------------------

def default_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def default_desktop_data() -> Optional[Path]:
    override = os.environ.get("HERMES_DESKTOP_USER_DATA_DIR")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Hermes" if appdata else None


def _registry_homes(root: Path) -> List[Path]:
    """Homes the credential registry names (a store can be registered outside ``profiles/``): the parent of
    every ``.../auth.json`` path string in ``auth.chains.json``. Absent or unparsable registry: none."""
    try:
        data = json.loads((root / "auth.chains.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: List[Path] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node.replace("\\", "/").endswith("/auth.json"):
            found.append(Path(node).parent)

    walk(data)
    return found


def discover_homes(root: Path, extra: List[Path]) -> List[Tuple[str, Path]]:
    """``(label, home)`` for the root, every directory under ``profiles/`` (inclusive: a backup keeps
    tombstoned and marker-less ones too), registry-named homes and ``extra``; each home once."""
    homes: List[Tuple[str, Path]] = [("root", root)]
    profiles = root / "profiles"
    if profiles.is_dir():
        homes += [(f"profiles/{entry.name}", entry) for entry in sorted(profiles.iterdir())
                  if entry.is_dir() and not entry.name.startswith(".")]
    seen = {os.path.normcase(str(home.resolve())) for _, home in homes}
    for index, home in enumerate([*_registry_homes(root), *extra]):
        key = os.path.normcase(str(home.resolve()))
        if key not in seen and home.is_dir():
            seen.add(key)
            homes.append((f"extra/{index}-{home.name}", home))
    return homes


def _walk(base: Path, *, excluded: set, root_excluded: set = frozenset()) -> Iterator[Path]:
    """Regular files under *base*, skipping excluded directory names (and root-only ones at the top).
    Unreadable directories raise: an incomplete backup must not pass as complete."""
    def fail(exc: OSError) -> None:
        raise exc

    for dirpath, dirnames, filenames in os.walk(base, onerror=fail):
        top = Path(dirpath) == base
        dirnames[:] = [d for d in dirnames if d not in excluded and not (top and d in root_excluded)]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                yield path


def _is_sqlite(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC


def home_items(label: str, home: Path) -> Iterator[Tuple[str, Path, str]]:
    """``(kind, source, backup_relpath)`` for one home."""
    root_excluded = _ROOT_TOP_EXCLUDED if label == "root" else _HOME_TOP_EXCLUDED
    for path in _walk(home, excluded=_EXCLUDED_DIRS, root_excluded=root_excluded):
        if path.suffix.lower() in _DB_SUFFIXES and _is_sqlite(path):
            yield "sqlite", path, f"homes/{label}/{path.relative_to(home).as_posix()}"
    skills = home / "skills"
    if skills.is_dir():
        for path in _walk(skills, excluded=_EXCLUDED_DIRS):
            if not (path.suffix.lower() in _DB_SUFFIXES and _is_sqlite(path)):
                yield "skill", path, f"homes/{label}/{path.relative_to(home).as_posix()}"
    for path in sorted(home.glob("auth.*")):
        if path.is_file() and not path.name.endswith(".lock"):
            yield "credential", path, f"homes/{label}/{path.name}"
    for name in _CREDENTIAL_DIRS:
        if (home / name).is_dir():
            for path in _walk(home / name, excluded=set()):
                yield "credential", path, f"homes/{label}/{path.relative_to(home).as_posix()}"
    for name in _CONFIG_FILES:
        if (home / name).is_file():
            yield "config", home / name, f"homes/{label}/{name}"


def desktop_items(data: Path) -> Iterator[Tuple[str, Path, str]]:
    for path in _walk(data, excluded=_DESKTOP_CACHE_DIRS):
        yield "desktop", path, f"desktop/{path.relative_to(data).as_posix()}"


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


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def backup(out: Path, homes: List[Tuple[str, Path]], desktop: Optional[Path]) -> Tuple[dict, List[str]]:
    for _, source in [*homes, *([("desktop", desktop)] if desktop else [])]:
        if _inside(out, source) or _inside(source, out):
            raise SystemExit(f"--out {out} overlaps the source {source}; choose a directory outside the install")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"--out {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        out.chmod(0o700)
    entries, errors = [], []
    sources = [item for label, home in homes for item in home_items(label, home)]
    if desktop is not None and desktop.is_dir():
        sources += list(desktop_items(desktop))
    for kind, source, rel in sources:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        entry = {"kind": kind, "source": str(source), "path": rel}
        if kind == "sqlite":
            if not _safe_copy_db(source, dest, timeout_seconds=30.0):
                errors.append(f"database not copied (busy or unreadable; stop Hermes first): {source}")
                continue
            # The snapshot inherits the source's WAL mode; as a self-contained file it must not grow
            # -wal/-shm sidecars on every open (Hermes switches it back to WAL when it opens it).
            conn = sqlite3.connect(dest)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                conn.close()
            # integrity_check runs once, on the restored copy (verify); byte-identical to this snapshot.
            entry["tables"] = _table_counts(dest)
        else:
            try:
                shutil.copyfile(source, dest)  # content only: never links, never source metadata
            except OSError as exc:
                errors.append(f"file not copied ({exc}; stop the desktop first): {source}")
                continue
        entry.update(size=dest.stat().st_size, sha256=_sha256(dest))
        entries.append(entry)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "homes": [
        {"label": label, "path": str(home)} for label, home in homes],
        "desktop": str(desktop) if desktop else None, "entries": entries}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest, errors


# --- verification (restore into scratch) -----------------------------------------------------------

def verify(out: Path) -> List[str]:
    """Restore every manifest entry into a scratch directory and check it; the list of problems."""
    manifest = json.loads((out / MANIFEST).read_text(encoding="utf-8"))
    problems: List[str] = []
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
                    problems.append(f"restored database does not open ({exc}): {entry['path']}")
                    continue
                if integrity != "ok":
                    problems.append(f"restored database fails integrity_check ({integrity}): {entry['path']}")
                if tables != entry["tables"]:
                    problems.append(f"restored database row counts differ: {entry['path']}")
    return problems


# --- CLI -------------------------------------------------------------------------------------------

def _plan(homes: List[Tuple[str, Path]], desktop: Optional[Path]) -> None:
    print("Would read (paths only; nothing is opened in --plan):")
    for label, home in homes:
        print(f"  [{label}] {home}")
        print(f"      every *.db / *.sqlite(3) under it (SQLite backup API, read-only), skills/, "
              f"auth.* (not *.lock), {', '.join(_CREDENTIAL_DIRS)}/, {', '.join(_CONFIG_FILES)}")
    print(f"  [desktop] {desktop if desktop else '(no APPDATA)'}  minus {', '.join(sorted(_DESKTOP_CACHE_DIRS))}")


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
        problems = verify(args.verify)
        for problem in problems:
            print(f"FAIL {problem}")
        print("VERIFIED" if not problems else f"NOT VERIFIED ({len(problems)} problems)")
        return 0 if not problems else 1

    root = args.root or default_root()
    desktop = args.desktop_data or default_desktop_data()
    if args.plan:
        homes = [("root", root)] + [(f"extra/{i}-{h.name}", h) for i, h in enumerate(args.extra_home)]
        print(f"  (plus every directory under {root / 'profiles'} and homes named in {root / 'auth.chains.json'})")
        _plan(homes, desktop)
        return 0
    homes = discover_homes(root, args.extra_home)
    manifest, errors = backup(args.out, homes, desktop)
    problems = errors + verify(args.out)
    kinds: Dict[str, int] = {}
    for entry in manifest["entries"]:
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    print(f"Backed up {len(manifest['entries'])} files from {len(homes)} homes to {args.out}: "
          + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())))
    for problem in problems:
        print(f"FAIL {problem}")
    print("VERIFIED" if not problems else f"NOT VERIFIED ({len(problems)} problems)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
