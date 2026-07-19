"""`hermes handoff` — triad relay pipe, lean-artifact lint, and decision gates.

Implements the reviewed D1/D3/D4 surface from
``docs/plans/2026-07-17-triad-full-fidelity-relay-mode.md``.

**D1 — the pipe.** ``capture`` writes one assistant message to a file and prints
its absolute path. The point is that Hermes relays the *path*, so the body never
enters Hermes' own context and never gets re-emitted (or summarized) into the
next agent's prompt. The receiving model still pays tokens for whatever it
reads — only the pass-through duplication goes away.

**Byte-identical is the whole feature.** ``capture`` selects the row via
:meth:`SessionDB.get_messages` (for role / id validation) but reads the *bytes*
straight from the raw ``messages.content`` column — see :func:`_raw_content`.
It deliberately does NOT reuse
``hermes sessions export``: export renders, and ``security.redact_secrets``
is bridged to a process-global ``HERMES_REDACT_SECRETS`` that ``agent.redact``
snapshots at import time (``hermes_cli/main.py`` ~:537), so an export-based
capture could emit redacted text while reporting success. A relay that silently
drops content is the exact defect this command exists to remove.

**D3 — lint.** Advisory by default, ``--strict`` (non-zero exit) for reviewer /
CI / pre-dispatch use, where an over-budget artifact is about to eat a
reviewer's context window.

**D4 — decide.** ``decisions.jsonl`` distinguishes proposed / acknowledged /
locked. ``locked`` requires ``--review-event`` naming what was actually reviewed
(the full deck, the executed program) — an agent recommendation or a casual
"sounds good" must not be able to freeze an artifact.

Naming note: ``handoff``, not ``relay`` — ``relay`` already belongs to the
gateway relay connector (``gateway/relay/``, ``Platform.RELAY``,
``GATEWAY_RELAY_*``).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Line budgets (D3). Silent at/below TARGET, advisory above it, violation above HARD.
LINT_TARGET_LINES = 250
LINT_HARD_LINES = 400
# A fenced block longer than this probably wants to be a path handle, not an inline body.
LINT_BLOCK_LINES = 60

DECISION_STATUSES = ("proposed", "acknowledged", "locked")


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _workspace_root() -> Path:
    """Git toplevel if we're in a repo, else cwd.

    Deliberately falls back rather than failing: deck work happens outside git,
    and the artifact dir must still resolve somewhere predictable.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            root = proc.stdout.strip()
            if root:
                return Path(root)
    except Exception:
        pass
    return Path.cwd()


def _load_triad_config() -> Dict[str, Any]:
    """Read the ``triad:`` section from config.yaml (empty dict if absent)."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
    except Exception:
        return {}
    section = cfg.get("triad")
    return section if isinstance(section, dict) else {}


def _resolve_workstream(args: argparse.Namespace, triad_cfg: Dict[str, Any]) -> str:
    """Flag → ``triad.workstream`` config → hard error.

    No invented default: a standalone CLI guessing a workstream writes artifacts
    to a directory nobody is watching, which reads as success and delivers
    nothing.
    """
    explicit = getattr(args, "workstream", None)
    if explicit:
        return str(explicit).strip()
    configured = triad_cfg.get("workstream")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    raise SystemExit(
        "error: no workstream. Pass --workstream <name> or set "
        "triad.workstream in config.yaml (hermes config set triad.workstream <name>)."
    )


def _resolve_artifact_dir(
    args: argparse.Namespace,
    triad_cfg: Dict[str, Any],
    workstream: Optional[str],
) -> Path:
    """``--dir`` → ``triad.handoff_dir`` → workspace-adjacent ``.triad/<workstream>``.

    Workspace-adjacent is the default because the failure mode of a path the
    reviewer cannot open is *silent* — the lane reviews nothing and reports
    confidently. A missing file under the workspace is a loud error instead.

    Deliberate asymmetry: ``--dir`` is the exact target directory (no
    ``<workstream>`` suffix — "put it precisely here"), whereas
    ``triad.handoff_dir`` and the workspace default are *roots* that get the
    workstream appended. A one-shot flag should not surprise the caller by
    nesting; a configured root should keep workstreams from colliding.
    """
    explicit = getattr(args, "dir", None)
    if explicit:
        return Path(explicit).expanduser().resolve()

    configured = triad_cfg.get("handoff_dir")
    if isinstance(configured, str) and configured.strip():
        base = Path(configured.strip()).expanduser().resolve()
    else:
        base = _workspace_root() / ".triad"

    return (base / workstream).resolve() if workstream else base.resolve()


def _open_db():
    from hermes_state import SessionDB
    return SessionDB()


def _resolve_session(db, session_id: Optional[str]) -> str:
    """Explicit id, else the most recently active session — echoed to stderr.

    The echo is the point: an implicit pick that turns out wrong should be
    visible immediately rather than discovered after a reviewer has read the
    wrong artifact.
    """
    if session_id:
        return session_id
    rows = db.list_sessions_rich(limit=1, order_by_last_active=True)
    if not rows:
        raise SystemExit("error: no sessions found; pass --session <id> explicitly.")
    resolved = rows[0].get("id")
    if not resolved:
        raise SystemExit("error: could not resolve a session id; pass --session <id>.")
    print(f"handoff: resolved session {resolved}", file=sys.stderr)
    return str(resolved)


def _raw_content(db_path: Any, message_id: int) -> str:
    """Read ``messages.content`` straight from SQLite, undecoded.

    ``SessionDB.get_messages`` runs every row through ``_decode_content``, which
    turns a stored payload back into a Python object (``hermes_state.py``
    ~:3428). Structured content is persisted with the NUL sentinel
    ``_CONTENT_JSON_PREFIX = "\\x00json:"`` (``:3403``) followed by
    ``json.dumps(content)`` with default separators. Re-serializing the decoded
    object would produce *different bytes*
    than the row holds — different separators, indentation, and no sentinel —
    so a byte-identity promise built on ``get_messages`` would quietly be false
    for structured assistant content.

    Consequence worth knowing: a structured capture therefore contains a
    literal NUL byte, which some editors and downstream tools handle poorly.
    That is the honest cost of capturing what is actually stored; assistant
    rows are plain text in practice, so this path is rare.

    Plain strings are stored and returned unchanged, so this path is identical
    to the decoded one for ordinary text rows; it only matters for structured
    content, where it is the difference between "verbatim" and "close enough".
    A separate read-only connection keeps this out of SessionDB's write lock.
    """
    path = str(db_path)
    conn = None
    try:
        try:
            conn = sqlite3.connect(
                f"file:{Path(path).as_posix()}?mode=ro", uri=True
            )
        except sqlite3.Error:
            # URI mode can be unavailable / awkward on some Windows paths.
            conn = sqlite3.connect(path)
        row = conn.execute(
            "SELECT content FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    finally:
        if conn is not None:
            conn.close()

    if row is None or row[0] is None:
        raise SystemExit(f"error: message {message_id} has no content to capture.")
    content = row[0]
    return content if isinstance(content, str) else str(content)


def _select_message(db, session_id: str, message_id: Optional[int]) -> Dict[str, Any]:
    """Pick the target row: explicit id, else the last assistant row."""
    rows = db.get_messages(session_id)
    if not rows:
        raise SystemExit(f"error: session {session_id} has no messages.")

    if message_id is not None:
        match = next((r for r in rows if r.get("id") == message_id), None)
        if match is None:
            raise SystemExit(
                f"error: message id {message_id} not found in session {session_id}."
            )
        if match.get("role") != "assistant":
            raise SystemExit(
                f"error: message id {message_id} has role "
                f"'{match.get('role')}'; capture takes assistant rows only."
            )
        return match

    for row in reversed(rows):
        if row.get("role") == "assistant" and row.get("content"):
            return row
    raise SystemExit(f"error: no assistant message found in session {session_id}.")


def _write_exact(path: Path, text: str) -> None:
    """Write UTF-8 with newline translation disabled.

    ``newline=""`` keeps CRLF as CRLF on Windows. Any translation here would
    make the artifact differ from the DB row, which is the one property this
    command promises.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _slugify(value: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in value.strip().lower()]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "handoff"


# ---------------------------------------------------------------------------
# capture / list / show
# ---------------------------------------------------------------------------

def cmd_capture(args: argparse.Namespace) -> int:
    triad_cfg = _load_triad_config()
    workstream = _resolve_workstream(args, triad_cfg)
    out_dir = _resolve_artifact_dir(args, triad_cfg, workstream)

    db = _open_db()
    session_id = _resolve_session(db, getattr(args, "session", None))
    # get_messages is used to *select* (role/id validation); the bytes come from
    # the raw column, because get_messages decodes structured content.
    row = _select_message(db, session_id, getattr(args, "message_id", None))
    text = _raw_content(db.db_path, row["id"])

    label = getattr(args, "label", None) or f"msg-{row.get('id')}"
    path = out_dir / f"{_slugify(label)}.md"
    _write_exact(path, text)

    # Exactly one line on stdout: the absolute path. Anything else here would
    # have to be parsed by whoever embeds this in a prompt.
    print(str(path))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    triad_cfg = _load_triad_config()
    workstream = getattr(args, "workstream", None) or triad_cfg.get("workstream") or None
    out_dir = _resolve_artifact_dir(args, triad_cfg, workstream)

    if not out_dir.is_dir():
        print(f"no handoff artifacts under {out_dir}")
        return 0

    files = [p for p in out_dir.glob("*.md") if p.is_file()]
    if not files:
        print(f"no handoff artifacts under {out_dir}")
        return 0

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        stamp = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{stamp}  {p.stat().st_size:>8}  {p}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    if not path.is_file():
        raise SystemExit(f"error: no such artifact: {path}")
    with open(path, "r", encoding="utf-8", newline="") as fh:
        sys.stdout.write(fh.read())
    return 0


# ---------------------------------------------------------------------------
# lint (D3)
# ---------------------------------------------------------------------------

def _lint_document(path: Path) -> tuple[List[str], List[str]]:
    """Return (violations, advisories) for one document."""
    violations: List[str] = []
    advisories: List[str] = []

    with open(path, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    lines = text.splitlines()
    n = len(lines)

    if n > LINT_HARD_LINES:
        violations.append(
            f"{path}: {n} lines exceeds the hard budget of {LINT_HARD_LINES}"
        )
    elif n > LINT_TARGET_LINES:
        advisories.append(
            f"{path}: {n} lines is over the ~{LINT_TARGET_LINES}-line target"
        )

    # Large fenced blocks usually mean an inlined body that should be a path.
    in_fence = False
    fence_start = 0
    for idx, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            if in_fence:
                span = idx - fence_start
                if span > LINT_BLOCK_LINES:
                    violations.append(
                        f"{path}:{fence_start}: embedded block of {span} lines "
                        f"— consider a path handle instead of an inline body"
                    )
                in_fence = False
            else:
                in_fence = True
                fence_start = idx

    # Correction notices are meant to be transient; a lingering one is bloat.
    for idx, line in enumerate(lines, start=1):
        low = line.lower()
        if "correction notice" in low or line.lstrip().startswith("> ### ⚠"):
            advisories.append(
                f"{path}:{idx}: correction notice still present — these are "
                f"transient; fold into the changelog and drop"
            )

    return violations, advisories


def cmd_lint(args: argparse.Namespace) -> int:
    target = getattr(args, "path", None)
    paths: List[Path] = []
    if target:
        p = Path(target).expanduser()
        if p.is_dir():
            paths = sorted(p.rglob("*.md"))
        elif p.is_file():
            paths = [p]
        else:
            raise SystemExit(f"error: no such path: {p}")
    else:
        triad_cfg = _load_triad_config()
        workstream = getattr(args, "workstream", None) or triad_cfg.get("workstream")
        base = _resolve_artifact_dir(args, triad_cfg, workstream)
        paths = sorted(base.rglob("*.md")) if base.is_dir() else []

    if not paths:
        print("handoff lint: nothing to check")
        return 0

    all_violations: List[str] = []
    all_advisories: List[str] = []
    for p in paths:
        try:
            v, a = _lint_document(p)
        except Exception as exc:
            all_advisories.append(f"{p}: unreadable ({exc})")
            continue
        all_violations.extend(v)
        all_advisories.extend(a)

    for msg in all_violations:
        print(f"VIOLATION {msg}")
    for msg in all_advisories:
        print(f"note      {msg}")

    if not all_violations and not all_advisories:
        print(f"handoff lint: {len(paths)} file(s) clean")

    # Advisory by default so a mid-draft document isn't a hard failure; strict
    # is for the moments that actually matter (reviewer, CI, pre-dispatch).
    if getattr(args, "strict", False) and all_violations:
        return 1
    return 0


# ---------------------------------------------------------------------------
# decide (D4)
# ---------------------------------------------------------------------------

def _decisions_path(args: argparse.Namespace, triad_cfg: Dict[str, Any],
                    workstream: str) -> Path:
    return _resolve_artifact_dir(args, triad_cfg, workstream) / "decisions.jsonl"


def _read_decisions(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def cmd_decide(args: argparse.Namespace) -> int:
    status = args.status
    if status not in DECISION_STATUSES:
        raise SystemExit(
            f"error: --status must be one of {', '.join(DECISION_STATUSES)}"
        )

    review_event = getattr(args, "review_event", None)
    if status == "locked" and not (review_event and review_event.strip()):
        raise SystemExit(
            "error: --status locked requires --review-event describing what you "
            "actually reviewed (e.g. \"read the full deck\", \"ran the program "
            "end to end\"). An agent recommendation or a casual approval is not "
            "a lock."
        )

    triad_cfg = _load_triad_config()
    workstream = _resolve_workstream(args, triad_cfg)
    path = _decisions_path(args, triad_cfg, workstream)

    record = {
        "id": args.id,
        "status": status,
        "statement": getattr(args, "statement", None) or "",
        "actor": getattr(args, "actor", None) or "lake",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "review_event": review_event.strip() if (status == "locked" and review_event) else None,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"{record['id']}: {record['status']}")
    return 0


def cmd_decisions(args: argparse.Namespace) -> int:
    """Show current status per decision id (last write wins)."""
    triad_cfg = _load_triad_config()
    workstream = _resolve_workstream(args, triad_cfg)
    path = _decisions_path(args, triad_cfg, workstream)
    records = _read_decisions(path)
    if not records:
        print(f"no decisions recorded at {path}")
        return 0

    latest: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        rid = rec.get("id")
        if rid:
            latest[rid] = rec

    for rid in sorted(latest):
        rec = latest[rid]
        line = f"{rec.get('status','?'):<13} {rid}"
        if rec.get("status") == "locked" and rec.get("review_event"):
            line += f"   [reviewed: {rec['review_event']}]"
        print(line)
    return 0


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workstream", help="Workstream name (default: triad.workstream)")
    p.add_argument("--dir", help="Artifact directory (default: <workspace>/.triad/<workstream>)")


def register_cli(parser: argparse.ArgumentParser) -> None:
    """Wire subcommands onto the ``hermes handoff`` parser."""
    parser.set_defaults(func=cmd_list)  # bare `hermes handoff` → list
    subs = parser.add_subparsers(dest="handoff_command", metavar="COMMAND")

    p_cap = subs.add_parser(
        "capture",
        help="Write one assistant message to a file and print its path",
    )
    p_cap.add_argument("--session", help="Session id (default: most recently active)")
    p_cap.add_argument("--message-id", type=int, dest="message_id",
                       help="SessionDB messages.id row id (assistant rows only)")
    p_cap.add_argument("--last", action="store_true",
                       help="Capture the last assistant message (default)")
    p_cap.add_argument("--label", help="Artifact filename stem")
    _add_common(p_cap)
    p_cap.set_defaults(func=cmd_capture)

    p_list = subs.add_parser("list", help="List handoff artifacts, newest first")
    p_list.add_argument("--session", help=argparse.SUPPRESS)
    _add_common(p_list)
    p_list.set_defaults(func=cmd_list)

    p_show = subs.add_parser("show", help="Print an artifact verbatim")
    p_show.add_argument("path")
    p_show.set_defaults(func=cmd_show)

    p_lint = subs.add_parser("lint", help="Check artifacts against the lean-artifact policy")
    p_lint.add_argument("--path", help="File or directory to lint")
    p_lint.add_argument("--strict", action="store_true",
                        help="Exit non-zero on violations (reviewer/CI/pre-dispatch)")
    _add_common(p_lint)
    p_lint.set_defaults(func=cmd_lint)

    p_dec = subs.add_parser("decide", help="Record a decision status")
    p_dec.add_argument("id")
    p_dec.add_argument("--status", required=True, choices=list(DECISION_STATUSES))
    p_dec.add_argument("--review-event", dest="review_event",
                       help="What was actually reviewed (required for locked)")
    p_dec.add_argument("--statement", help="Decision text")
    p_dec.add_argument("--actor", help="Who recorded this (default: lake)")
    _add_common(p_dec)
    p_dec.set_defaults(func=cmd_decide)

    p_decs = subs.add_parser("decisions", help="Show current status per decision id")
    _add_common(p_decs)
    p_decs.set_defaults(func=cmd_decisions)


__all__ = [
    "register_cli",
    "cmd_capture", "cmd_list", "cmd_show", "cmd_lint", "cmd_decide", "cmd_decisions",
    "LINT_TARGET_LINES", "LINT_HARD_LINES", "DECISION_STATUSES",
]
