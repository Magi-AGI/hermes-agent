"""``hermes auth recovery list|clear``: inspect the Codex chain authority; delete ARCHIVES only.

Archives (committed / abandoned ownership payloads, retired chain records, damaged-registry copies)
are never pruned automatically (decision 3). ``clear`` removes archived files only — never the
registry, a live chain record, or an unfinished op's payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from hermes_cli.auth_codex_registry import (
    REGISTRY_NAME, all_store_paths, ops_dir, read_registry, registry_path)
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
        lines.append(f"  {cid[:8]}  {entry.get('state')}{detail}  owner={entry.get('owner')}  rows=[{rows}]")
    for op_id, op in view.ops.items():
        lines.append(f"  unfinished op {op_id[:8]}: {op.get('kind')} ({op.get('status')})")
    lines.append(f"Archived recovery files: {len(_archive_files())} (`hermes auth recovery clear` removes them)")
    return lines


def clear_archives() -> int:
    removed = 0
    for path in _archive_files():
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed
