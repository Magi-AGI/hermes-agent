"""Child-process worker for the same-profile multi-process DB stress test.

Run as a standalone OS process (NOT a thread) by
``tests/integration/test_same_profile_multi_process_sessions.py``. Each worker
opens the SAME profile ``state.db`` and writes one distinct session so the
parent can prove that N isolated same-profile backends contend on SQLite
without lock explosions.

Contract (all via argv/env, emits a single JSON status line on stdout):
  - Wait on a start-gate file so every worker's writes overlap in time.
  - Open ``SessionDB(db_path)``, create a unique session, append several
    deterministic user/assistant messages (one carries a session-unique token),
    set a deterministic title, and update model / model_config metadata.
  - Exit 0 with ``{"ok": true, ...}`` on success; exit 1 with
    ``{"ok": false, "error": ..., "trace": ...}`` (also echoed to stderr) on
    any failure — including a propagated ``database is locked``.

Kept dependency-light and deterministic so it is reliable in CI/dev.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path


def _wait_for_gate(gate_path: str, timeout_s: float) -> None:
    """Block until the parent creates the gate file (bounded)."""
    deadline = time.monotonic() + timeout_s
    while not os.path.exists(gate_path):
        if time.monotonic() > deadline:
            # Proceed anyway — the parent may have released late; overlap is a
            # best-effort optimization, not a correctness requirement.
            return
        time.sleep(0.005)


def main() -> int:
    raw = sys.argv[1]
    spec = json.loads(raw)

    db_path = spec["db_path"]
    session_id = spec["session_id"]
    unique_token = spec["unique_token"]
    title = spec["title"]
    initial_model = spec["initial_model"]
    final_model = spec["final_model"]
    model_config_json = spec["model_config_json"]
    n_pairs = int(spec["n_pairs"])
    gate_path = spec["gate_path"]
    gate_timeout_s = float(spec.get("gate_timeout_s", 10.0))

    # Import AFTER arg parse so a bad invocation fails fast and cheap. Repo root
    # is on PYTHONPATH (set by the parent), and HERMES_HOME is pinned to a temp
    # dir so nothing touches the real user home.
    from hermes_state import SessionDB

    _wait_for_gate(gate_path, gate_timeout_s)

    db = SessionDB(Path(db_path))
    appended = 0
    try:
        db.create_session(session_id, source="cli", model=initial_model)

        # Deterministic user/assistant pairs. The first user message carries the
        # session-unique token so the parent's FTS search maps a token → exactly
        # one session.
        for i in range(n_pairs):
            user_content = f"message {i} for {session_id}"
            if i == 0:
                user_content = f"{user_content} token {unique_token}"
            db.append_message(session_id, role="user", content=user_content)
            appended += 1
            db.append_message(
                session_id,
                role="assistant",
                content=f"reply {i} for {session_id}",
            )
            appended += 1

        db.set_session_title(session_id, title)
        # Exercise both metadata write paths: meta (model_config + model) then a
        # dedicated model update to the final model.
        db.update_session_meta(session_id, model_config_json, model=initial_model)
        db.update_session_model(session_id, final_model)
    finally:
        db.close()

    print(json.dumps({
        "ok": True,
        "session_id": session_id,
        "appended": appended,
        "pid": os.getpid(),
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — surface ALL failures to the parent
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
            "pid": os.getpid(),
        }
        # Emit on BOTH streams so the parent catches it regardless of which it
        # inspects, and so a propagated 'database is locked' is unmissable.
        sys.stderr.write(json.dumps(payload) + "\n")
        print(json.dumps(payload))
        sys.exit(1)
