"""``hermes auth recovery list|clear``: inspect the Codex chain authority; delete ARCHIVES only.

Archives (committed / abandoned ownership payloads, retired chain records, damaged-registry copies)
are never pruned automatically (decision 3). ``clear`` removes archived files only — never the
registry, a live chain record, or an unfinished op's payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from hermes_cli.auth_codex_registry import (
    RECORDS_DIR_NAME, REGISTRY_NAME, all_store_paths, migration_lock, ops_dir, read_registry, registry_path)
from hermes_cli.auth_codex_records import records_dir


def _archive_files() -> List[Path]:
    files = list((ops_dir() / "archive").glob("*.json")) if (ops_dir() / "archive").is_dir() else []
    for store in all_store_paths():
        archive = records_dir(store) / "archive"
        if archive.is_dir():
            files.extend(archive.glob("*.json"))
    files.extend(registry_path().parent.glob(f"{REGISTRY_NAME}.corrupt.*"))
    return sorted(set(files))


def list_recovery() -> List[str]:
    view = read_registry()
    lines = [f"Codex login registry: {view.status}" + (f" ({view.error})" if view.error else "")]
    for cid, entry in view.chains.items():
        fence = entry.get("fence") or {}
        detail = f" — {fence.get('reason')}" if fence.get("reason") else ""
        rows = ", ".join(r["row"] for r in entry.get("rows", ()))
        lines.append(f"  {cid[:12]}  {entry.get('state')}{detail}  owner={entry.get('owner')}  rows=[{rows}]")
    for op_id, op in view.ops.items():
        hint = (f" — `hermes auth recovery resolve {op_id[:8]}` releases it"
                if op.get("status") == "unrecoverable" else "")
        lines.append(f"  unfinished op {op_id[:8]}: {op.get('kind')} ({op.get('status')}){hint}")
    lines.append(f"Archived recovery files: {len(_archive_files())} (`hermes auth recovery clear` removes them)")
    return lines


def _index_archived_record(path: Path) -> bool:
    """Fold an archived chain record into the retired-token index before its file goes; False
    when the file cannot be read (it is then KEPT: its evidence would otherwise be lost)."""
    from hermes_cli.auth_codex_evidence import material_of, read_index, retain
    from hermes_cli.auth_codex_records import RecordView
    from hermes_cli.auth_credential_io import read_bytes_retrying
    try:
        data = json.loads(read_bytes_retrying(path).decode("utf-8"))
        cid = data["chain_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    status, index = read_index()
    if status != "ok" or cid not in index["chains"]:
        retain({cid: material_of(RecordView("ok", data), reason="archived")})
    return True


def clear_archives() -> int:
    """Delete archived files. Archived chain records are indexed first (the retired-token index
    itself is never an archive and is never removed)."""
    removed = 0
    with migration_lock():
        for path in _archive_files():
            if path.parent.name == "archive" and path.parent.parent.name == RECORDS_DIR_NAME:
                if not _index_archived_record(path):
                    continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
