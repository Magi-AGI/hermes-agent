"""Acceptance tests for `hermes handoff` (D1 pipe, D3 lint, D4 decision gates).

Maps to §2.6, §3.4, and §4.4 of
``docs/plans/2026-07-17-triad-full-fidelity-relay-mode.md``.

These exercise the real functions against a real SQLite session store in a temp
``HERMES_HOME`` — the capture path's whole promise is byte-identity with the DB
row, which a mocked store cannot demonstrate.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture()
def db(hermes_home, tmp_path, monkeypatch):
    """An isolated SessionDB, injected into the code under test.

    ``SessionDB()`` with no argument falls back to module-level
    ``DEFAULT_DB_PATH``, which is resolved at import time — so redirecting
    ``HERMES_HOME`` in a fixture does NOT move it, and the command under test
    would open (and write to) the real session store. Both the fixture and
    ``relaypipe._open_db`` therefore have to point at the same explicit temp
    path, or the test writes one DB and the command reads another.
    """
    from hermes_state import SessionDB
    from hermes_cli import relaypipe

    session_db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(relaypipe, "_open_db", lambda: session_db)
    return session_db


def _mk_session(db, session_id: str, rows):
    """Create a session and append (role, content) rows in order."""
    db.create_session(session_id, "cli")
    for role, content in rows:
        db.append_message(session_id, role, content)


def _args(**kw):
    ns = argparse.Namespace()
    defaults = dict(
        session=None, message_id=None, last=False, label=None,
        workstream="ws", dir=None, strict=False, path=None,
        status=None, review_event=None, statement=None, actor=None, id=None,
    )
    defaults.update(kw)
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


def _read_exact(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# §2.6 — capture
# ---------------------------------------------------------------------------

def test_capture_last_is_byte_identical_to_db_row(db, tmp_path, capsys):
    """§2.6 #1 — the artifact matches the stored row exactly."""
    from hermes_cli import relaypipe

    body = "Line one\n\nLine two with **markdown** and a `backtick`.\n"
    _mk_session(db, "s1", [("user", "hi"), ("assistant", body)])

    out_dir = tmp_path / "arts"
    rc = relaypipe.cmd_capture(_args(session="s1", dir=str(out_dir), label="cap"))
    assert rc == 0

    printed = capsys.readouterr().out.strip()
    assert _read_exact(Path(printed)) == body

    stored = [m for m in db.get_messages("s1") if m["role"] == "assistant"][-1]
    assert _read_exact(Path(printed)) == stored["content"]


def test_capture_unaffected_by_redact_secrets(db, tmp_path, monkeypatch, capsys):
    """§2.6 #1 — capture must not run through the redaction/render path."""
    from hermes_cli import relaypipe

    monkeypatch.setenv("HERMES_REDACT_SECRETS", "1")
    body = "token: sk-ant-fake-000111222333\nkeep me verbatim\n"
    _mk_session(db, "s1", [("assistant", body)])

    relaypipe.cmd_capture(_args(session="s1", dir=str(tmp_path / "a"), label="c"))
    printed = capsys.readouterr().out.strip()
    assert _read_exact(Path(printed)) == body


def test_capture_preserves_crlf_whitespace_and_unicode(db, tmp_path, capsys):
    """§2.6 #2 — no newline translation, no reflow, no trailing-space stripping."""
    from hermes_cli import relaypipe

    body = "alpha\r\nbeta   \n\tgamma — ünïcode ✓\n\n\n"
    _mk_session(db, "s1", [("assistant", body)])

    relaypipe.cmd_capture(_args(session="s1", dir=str(tmp_path / "a"), label="c"))
    printed = capsys.readouterr().out.strip()
    assert _read_exact(Path(printed)) == body


def test_capture_structured_content_is_raw_db_bytes(db, tmp_path, capsys):
    """Codex CP019 — structured content must be the stored bytes, not a re-serialization.

    SessionDB persists list/dict content behind the NUL sentinel
    ``_CONTENT_JSON_PREFIX = "\\x00json:"`` and decodes it back on read, so
    capturing via ``get_messages`` and re-dumping would emit different bytes
    than the row holds. Capture reads the raw column instead, which keeps the
    byte-identity claim true for every assistant row rather than only for plain
    strings.
    """
    import sqlite3
    from hermes_cli import relaypipe

    structured = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    _mk_session(db, "s1", [("assistant", structured)])

    relaypipe.cmd_capture(_args(session="s1", dir=str(tmp_path / "a"), label="c"))
    written = _read_exact(Path(capsys.readouterr().out.strip()))

    conn = sqlite3.connect(str(db.db_path))
    try:
        stored = conn.execute(
            "SELECT content FROM messages WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    assert written == stored
    # Explicit about WHY the bytes differ: the stored form carries the NUL
    # sentinel, so a decoded-then-re-dumped artifact could never match.
    assert written.startswith("\x00json:")
    # And specifically NOT the decoded/re-serialized form the old code produced.
    assert written != json.dumps(structured, ensure_ascii=False, indent=2)


def test_capture_message_id_selects_row(db, tmp_path, capsys):
    """§2.6 #3 — explicit row id selection."""
    from hermes_cli import relaypipe

    _mk_session(db, "s1", [
        ("assistant", "first answer"),
        ("user", "follow up"),
        ("assistant", "second answer"),
    ])
    rows = db.get_messages("s1")
    first_assistant = [r for r in rows if r["role"] == "assistant"][0]

    relaypipe.cmd_capture(_args(
        session="s1", message_id=first_assistant["id"],
        dir=str(tmp_path / "a"), label="c",
    ))
    printed = capsys.readouterr().out.strip()
    assert _read_exact(Path(printed)) == "first answer"


def test_capture_rejects_non_assistant_row(db, tmp_path):
    """§2.6 #3 — user rows are not capture targets."""
    from hermes_cli import relaypipe

    _mk_session(db, "s1", [("user", "a question"), ("assistant", "an answer")])
    user_row = [r for r in db.get_messages("s1") if r["role"] == "user"][0]

    with pytest.raises(SystemExit) as exc:
        relaypipe.cmd_capture(_args(
            session="s1", message_id=user_row["id"], dir=str(tmp_path / "a"),
        ))
    assert "assistant rows only" in str(exc.value)


def test_capture_rejects_out_of_range_message_id(db, tmp_path):
    """§2.6 #3 — unknown id is a hard error, not a silent fallback."""
    from hermes_cli import relaypipe

    _mk_session(db, "s1", [("assistant", "x")])
    with pytest.raises(SystemExit) as exc:
        relaypipe.cmd_capture(_args(
            session="s1", message_id=999999, dir=str(tmp_path / "a"),
        ))
    assert "not found" in str(exc.value)


def test_session_defaults_to_most_recent_and_echoes_to_stderr(db, tmp_path, capsys):
    """§2.6 #4 — implicit session resolution must be visible, never silent."""
    from hermes_cli import relaypipe

    _mk_session(db, "older", [("assistant", "old body")])
    _mk_session(db, "newer", [("assistant", "new body")])

    relaypipe.cmd_capture(_args(session=None, dir=str(tmp_path / "a"), label="c"))
    captured = capsys.readouterr()

    assert "resolved session" in captured.err
    assert _read_exact(Path(captured.out.strip())) == "new body"


def test_workstream_required_when_unset(db, tmp_path, monkeypatch):
    """§2.6 #4 — no invented workstream slug."""
    from hermes_cli import relaypipe

    monkeypatch.setattr(relaypipe, "_load_triad_config", lambda: {})
    _mk_session(db, "s1", [("assistant", "x")])

    with pytest.raises(SystemExit) as exc:
        relaypipe.cmd_capture(_args(session="s1", workstream=None, dir=None))
    assert "no workstream" in str(exc.value)


def test_capture_prints_exactly_one_line(db, tmp_path, capsys):
    """§2.6 #5 — stdout is embeddable in a prompt without parsing."""
    from hermes_cli import relaypipe

    _mk_session(db, "s1", [("assistant", "body")])
    relaypipe.cmd_capture(_args(session="s1", dir=str(tmp_path / "a"), label="c"))

    out_lines = [l for l in capsys.readouterr().out.split("\n") if l.strip()]
    assert len(out_lines) == 1
    assert Path(out_lines[0]).is_absolute()


# --- §2.6 #7: Hermes must not inline the body -------------------------------

def _relay_message(path: str) -> str:
    """The message shape a relay is supposed to emit: a pointer, not a body."""
    return f"Read the full source at:\n  {path}\nReview it directly."


def test_relay_message_contains_no_64_char_span_of_body(db, tmp_path, capsys):
    """§2.6 #7 — the primary no-inline assertion."""
    from hermes_cli import relaypipe

    body = "".join(
        f"Distinctive paragraph number {i} that is definitely long enough "
        f"to exceed the sixty-four character window used by this check.\n"
        for i in range(5)
    )
    _mk_session(db, "s1", [("assistant", body)])
    relaypipe.cmd_capture(_args(session="s1", dir=str(tmp_path / "a"), label="c"))
    path = capsys.readouterr().out.strip()

    msg = _relay_message(path)
    assert path in msg
    spans = {body[i:i + 64] for i in range(0, max(1, len(body) - 64))}
    assert not any(s in msg for s in spans)


def test_no_inline_short_artifact_variant(db, tmp_path, capsys):
    """§2.6 #7(a) — the 64-char rule is vacuous below 64 chars, so bound length."""
    from hermes_cli import relaypipe

    body = "tiny answer\n"  # < 64 chars: substring rule proves nothing here
    assert len(body) < 64
    _mk_session(db, "s1", [("assistant", body)])
    relaypipe.cmd_capture(_args(session="s1", dir=str(tmp_path / "a"), label="c"))
    path = capsys.readouterr().out.strip()

    msg = _relay_message(path)
    # The message must stay within a small envelope over the path itself,
    # which a body-inlining implementation could not satisfy.
    assert len(msg) - len(path) < 120
    assert body.strip() not in msg


def test_no_inline_repetitive_artifact_variant(db, tmp_path, capsys):
    """§2.6 #7(b) — repetitive bodies can dodge a single 64-char window."""
    from hermes_cli import relaypipe

    body = "BOILERPLATE LINE\n" * 200
    _mk_session(db, "s1", [("assistant", body)])
    relaypipe.cmd_capture(_args(session="s1", dir=str(tmp_path / "a"), label="c"))
    path = capsys.readouterr().out.strip()

    msg = _relay_message(path)
    # Not merely "shorter than the body" — it must share no distinctive span.
    assert "BOILERPLATE LINE" not in msg
    assert len(msg) < len(body)


# --- §2.6 #8: artifact dir resolution ---------------------------------------

def test_artifact_dir_defaults_to_workspace_triad(tmp_path, monkeypatch):
    """§2.6 #8 — workspace-adjacent .triad/<workstream> with no override."""
    from hermes_cli import relaypipe

    monkeypatch.setattr(relaypipe, "_workspace_root", lambda: tmp_path)
    resolved = relaypipe._resolve_artifact_dir(_args(dir=None), {}, "ws")
    assert resolved == (tmp_path / ".triad" / "ws").resolve()


def test_config_handoff_dir_overrides_workspace(tmp_path, monkeypatch):
    """§2.6 #8 — triad.handoff_dir beats the workspace default."""
    from hermes_cli import relaypipe

    monkeypatch.setattr(relaypipe, "_workspace_root", lambda: tmp_path / "ws_root")
    cfg = {"handoff_dir": str(tmp_path / "configured")}
    resolved = relaypipe._resolve_artifact_dir(_args(dir=None), cfg, "ws")
    assert resolved == (tmp_path / "configured" / "ws").resolve()


def test_dir_flag_overrides_everything(tmp_path, monkeypatch):
    """§2.6 #8 — --dir is absolute authority."""
    from hermes_cli import relaypipe

    monkeypatch.setattr(relaypipe, "_workspace_root", lambda: tmp_path / "ws_root")
    cfg = {"handoff_dir": str(tmp_path / "configured")}
    resolved = relaypipe._resolve_artifact_dir(
        _args(dir=str(tmp_path / "explicit")), cfg, "ws"
    )
    assert resolved == (tmp_path / "explicit").resolve()


# ---------------------------------------------------------------------------
# §3.4 — lint
# ---------------------------------------------------------------------------

def test_lint_silent_at_target_and_warns_over_hard_budget(tmp_path, capsys):
    """§3.4 #1 — silent at 250 lines, violation above 400."""
    from hermes_cli import relaypipe

    ok = tmp_path / "ok.md"
    ok.write_text("x\n" * relaypipe.LINT_TARGET_LINES, encoding="utf-8")
    v, a = relaypipe._lint_document(ok)
    assert v == [] and a == []

    big = tmp_path / "big.md"
    big.write_text("x\n" * (relaypipe.LINT_HARD_LINES + 10), encoding="utf-8")
    v, a = relaypipe._lint_document(big)
    assert any("hard budget" in m for m in v)


def test_lint_flags_large_embedded_block(tmp_path):
    """§3.4 #2 — an inlined body that should have been a handle."""
    from hermes_cli import relaypipe

    doc = tmp_path / "packet.md"
    doc.write_text(
        "# Packet\n\n```\n" + ("embedded source line\n" * 80) + "```\n",
        encoding="utf-8",
    )
    v, _ = relaypipe._lint_document(doc)
    assert any("embedded block" in m for m in v)


def test_lint_strict_exits_nonzero_plain_does_not(tmp_path, capsys):
    """§3.4 #3 — advisory locally, enforcing at dispatch."""
    from hermes_cli import relaypipe

    doc = tmp_path / "big.md"
    doc.write_text("x\n" * (relaypipe.LINT_HARD_LINES + 10), encoding="utf-8")

    assert relaypipe.cmd_lint(_args(path=str(doc), strict=False)) == 0
    assert relaypipe.cmd_lint(_args(path=str(doc), strict=True)) == 1


def test_lint_strict_propagates_through_main_dispatch(tmp_path, monkeypatch, capsys):
    """§3.4 #3 — the exit code must survive the top-level CLI dispatcher.

    Regression for CP017: `cmd_lint` returned 1 correctly, but
    `hermes_cli.main.main()` discarded subcommand return values, so
    `handoff lint --strict` printed its violation and still exited 0 —
    silently defeating any CI or pre-dispatch gate built on it. Asserting at
    the function level alone did not catch this; the dispatcher is the seam.
    """
    from hermes_cli.main import main

    doc = tmp_path / "big.md"
    doc.write_text("x\n" * 410, encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv", ["hermes", "handoff", "lint", "--path", str(doc), "--strict"]
    )
    assert main() == 1

    monkeypatch.setattr(
        "sys.argv", ["hermes", "handoff", "lint", "--path", str(doc)]
    )
    assert main() == 0


# ---------------------------------------------------------------------------
# §4.4 — decision gates
# ---------------------------------------------------------------------------

def test_locked_without_review_event_is_refused(tmp_path):
    """§4.4 #1 — the core D4 guarantee."""
    from hermes_cli import relaypipe

    with pytest.raises(SystemExit) as exc:
        relaypipe.cmd_decide(_args(
            id="deck-slide4", status="locked", review_event=None,
            dir=str(tmp_path / "a"),
        ))
    assert "requires --review-event" in str(exc.value)


def test_proposed_to_locked_in_one_step_is_allowed_with_review_event(tmp_path, capsys):
    """§4.4 #2 — the gate is a review event, not a prior status."""
    from hermes_cli import relaypipe

    d = str(tmp_path / "a")
    relaypipe.cmd_decide(_args(id="deck-slide4", status="proposed", dir=d))
    rc = relaypipe.cmd_decide(_args(
        id="deck-slide4", status="locked",
        review_event="read the full deck 2026-07-18", dir=d,
    ))
    assert rc == 0

    records = relaypipe._read_decisions(Path(d) / "decisions.jsonl")
    assert records[-1]["status"] == "locked"
    assert records[-1]["review_event"] == "read the full deck 2026-07-18"


def test_deck_lifecycle_proposed_acknowledged_locked(tmp_path, capsys):
    """§4.4 #4 — acknowledged must not read as locked."""
    from hermes_cli import relaypipe

    d = str(tmp_path / "a")
    relaypipe.cmd_decide(_args(id="deck-slide4", status="proposed", dir=d))
    relaypipe.cmd_decide(_args(id="deck-slide4", status="acknowledged", dir=d))

    relaypipe.cmd_decisions(_args(dir=d))
    assert "acknowledged" in capsys.readouterr().out

    relaypipe.cmd_decide(_args(
        id="deck-slide4", status="locked",
        review_event="viewed the complete deck", dir=d,
    ))
    relaypipe.cmd_decisions(_args(dir=d))
    out = capsys.readouterr().out
    assert "locked" in out and "viewed the complete deck" in out


def test_code_lifecycle_lock_requires_execution_evidence(tmp_path, capsys):
    """§4.4 #5 — same lifecycle for code, gated on running the program."""
    from hermes_cli import relaypipe

    d = str(tmp_path / "a")
    relaypipe.cmd_decide(_args(id="handoff-cli", status="proposed", dir=d))

    with pytest.raises(SystemExit):
        relaypipe.cmd_decide(_args(id="handoff-cli", status="locked", dir=d))

    relaypipe.cmd_decide(_args(
        id="handoff-cli", status="locked",
        review_event="executed hermes handoff end to end", dir=d,
    ))
    records = relaypipe._read_decisions(Path(d) / "decisions.jsonl")
    assert records[-1]["review_event"] == "executed hermes handoff end to end"
