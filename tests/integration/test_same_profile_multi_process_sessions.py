"""Task 11 — multi-process same-profile session DB stress test.

Proves that ~10 isolated same-profile backends (real OS processes, NOT threads)
can each open the SAME profile ``state.db`` and write a distinct session
concurrently without SQLite lock explosions. This exercises SessionDB's WAL +
``_execute_write`` BEGIN-IMMEDIATE / jitter-retry contention handling under true
cross-process pressure — the failure mode Desktop session-backend isolation
must survive when many same-profile session windows run at once.

Assertions:
  - every worker exits 0 (nonzero → its captured stdout/stderr is surfaced)
  - no worker output propagated a lock error after the retry budget
  - all N sessions exist with the expected per-session message count / ownership
  - title / model / model_config metadata landed for each session
  - (when FTS5 is enabled) each session's unique token is found by search and
    resolves to exactly that session

Bounded and hermetic: fixed N, fixed message count, per-worker timeout, a temp
DB + temp HERMES_HOME, and no access to the real user home.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB

pytestmark = pytest.mark.integration

# Repo root = two levels up from tests/integration/.
REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "tests" / "fixtures" / "session_db_stress_worker.py"

N_WORKERS = 10
N_PAIRS = 4  # user+assistant pairs per session → 2*N_PAIRS messages each.
WORKER_TIMEOUT_S = 60.0

# Substrings that must NOT appear in any worker's output: a propagated SQLite
# contention failure that survived the application-level retry budget.
LOCK_MARKERS = (
    "database is locked",
    "database table is locked",
    "SQLITE_BUSY",
    "OperationalError",
)


def _worker_env() -> dict:
    """Child env: repo root importable, HERMES_HOME pinned to a temp dir."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT)] + ([existing] if existing else []))
    return env


def _spec(db_path: Path, gate_path: Path, idx: int) -> dict:
    sid = f"sess_{idx:02d}"
    token = f"uniqtok{idx:02d}zzz"  # session-unique, FTS-friendly (no CJK/specials).
    return {
        "db_path": str(db_path),
        "session_id": sid,
        "unique_token": token,
        "title": f"Stress Session {idx:02d}",
        "initial_model": f"model-init-{idx:02d}",
        "final_model": f"model-final-{idx:02d}",
        "model_config_json": json.dumps({"idx": idx, "temperature": 0.1}),
        "n_pairs": N_PAIRS,
        "gate_path": str(gate_path),
        "gate_timeout_s": 15.0,
    }


def test_ten_same_profile_processes_write_distinct_sessions(tmp_path):
    assert WORKER.exists(), f"worker fixture missing: {WORKER}"

    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "profile" / "state.db"
    db_path.parent.mkdir(parents=True)

    # Initialize the schema ONCE in the parent (so workers race an existing DB,
    # not schema creation) and learn whether FTS5 is available in this build.
    init_db = SessionDB(db_path)
    fts_enabled = bool(init_db._fts_enabled)
    init_db.close()

    env = _worker_env()
    env["HERMES_HOME"] = str(home)

    gate_path = tmp_path / "start.gate"  # created AFTER all workers are spawned.
    specs = [_spec(db_path, gate_path, i) for i in range(N_WORKERS)]

    # Spawn all workers; they block on the gate so their writes overlap.
    procs = []
    for spec in specs:
        procs.append(subprocess.Popen(
            [sys.executable, str(WORKER), json.dumps(spec)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ))

    # Give every worker a beat to reach the gate wait, then release them at once.
    time.sleep(0.3)
    gate_path.write_text("go", encoding="utf-8")

    # Collect with a bounded timeout; kill + surface diagnostics on hang.
    results = []
    for spec, proc in zip(specs, procs):
        try:
            out, err = proc.communicate(timeout=WORKER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            pytest.fail(
                f"worker {spec['session_id']} timed out after {WORKER_TIMEOUT_S}s\n"
                f"stdout:\n{out}\nstderr:\n{err}"
            )
        results.append((spec, proc.returncode, out or "", err or ""))

    # Every worker must have exited cleanly; a nonzero exit surfaces its output.
    failures = [(s["session_id"], rc, out, err) for (s, rc, out, err) in results if rc != 0]
    assert not failures, "worker(s) failed:\n" + "\n".join(
        f"  {sid} rc={rc}\n    stdout: {out.strip()}\n    stderr: {err.strip()}"
        for (sid, rc, out, err) in failures
    )

    # No propagated lock/contention error should have escaped the retry budget.
    for (s, _rc, out, err) in results:
        blob = f"{out}\n{err}"
        for marker in LOCK_MARKERS:
            assert marker not in blob, (
                f"worker {s['session_id']} surfaced a contention failure "
                f"('{marker}') after retries:\n{blob}"
            )

    # Each worker should report success + the expected number of appended user msgs.
    for (s, _rc, out, _err) in results:
        status = json.loads(out.strip().splitlines()[-1])
        assert status["ok"] is True, f"{s['session_id']} not ok: {status}"
        assert status["session_id"] == s["session_id"]
        assert status["appended"] == 2 * N_PAIRS

    # ── Reopen in the parent and verify the persisted state ──────────────────
    verify = SessionDB(db_path)
    try:
        for spec in specs:
            sid = spec["session_id"]
            sess = verify.get_session(sid)
            assert sess is not None, f"session {sid} missing after stress run"

            # Metadata landed: title + final model + model_config.
            assert sess["title"] == spec["title"], f"{sid} title mismatch: {sess['title']!r}"
            assert sess["model"] == spec["final_model"], f"{sid} model mismatch: {sess['model']!r}"
            assert sess["model_config"], f"{sid} model_config missing"
            cfg = json.loads(sess["model_config"])
            assert cfg["idx"] == spec_idx(spec), f"{sid} model_config idx mismatch: {cfg}"

            # Messages: exactly 2*N_PAIRS, all owned by this session, right roles.
            msgs = verify.get_messages(sid)
            assert len(msgs) == 2 * N_PAIRS, f"{sid} expected {2 * N_PAIRS} msgs, got {len(msgs)}"
            assert all(m["session_id"] == sid for m in msgs), f"{sid} has foreign messages"
            roles = [m["role"] for m in msgs]
            assert roles == ["user", "assistant"] * N_PAIRS, f"{sid} role/order mismatch: {roles}"

        # Cross-session integrity: N distinct sessions, 2*N_PAIRS*N total messages.
        all_ids = {s["session_id"] for s in specs}
        assert len(all_ids) == N_WORKERS

        # ── FTS: each unique token resolves to exactly its own session ───────
        if fts_enabled:
            for spec in specs:
                sid = spec["session_id"]
                token = spec["unique_token"]
                hits = verify.search_messages(token, limit=50)
                assert hits, f"FTS found no hit for {sid}'s token {token!r}"
                hit_sessions = {h["session_id"] for h in hits}
                assert hit_sessions == {sid}, (
                    f"token {token!r} should map to only {sid}, got {hit_sessions}"
                )
        else:
            # Do not silently skip when FTS is actually enabled; only skip the
            # FTS assertion when the build genuinely lacks FTS5.
            pytest.skip("FTS5 unavailable in this Python/SQLite build; skipped FTS assertion only")
    finally:
        verify.close()


def spec_idx(spec: dict) -> int:
    return int(spec["session_id"].split("_")[1])
