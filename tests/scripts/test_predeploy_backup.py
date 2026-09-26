"""scripts/predeploy_backup.py (deploy-gate checklist F7) on a synthetic sealed install.

The backup must cover the root and every profile home (including one registered outside ``profiles/``),
copy databases with SQLite's backup API (rows still only in the WAL included: a raw copy of the main file
would lose them), carry skills, credential-protocol state and saved windows, leave the sources untouched,
and prove itself by restoring into a scratch directory. The script runs as a real subprocess with an
allow-listed environment whose user dirs are sealed inside the test (evals/desktop_bug_campaign/sealed_home).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "predeploy_backup.py"
WAL_ROWS = 25


def _sealed_env(os_home: Path) -> dict:
    spec = importlib.util.spec_from_file_location(
        "sealed_home_for_backup_test", REPO / "evals" / "desktop_bug_campaign" / "sealed_home.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    env = module.sealed_env(os_home)
    env["PYTHONPATH"] = str(REPO)
    return env


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _install(tmp_path: Path):
    """A root with one profile, a home registered outside profiles/, and desktop data. Returns the
    install paths and an open connection holding WAL-only rows in the root compaction.db."""
    from hermes_state import SessionDB

    root, work, desktop = tmp_path / "hermes", tmp_path / "elsewhere" / "work", tmp_path / "AppData" / "Hermes"
    for home, sid in ((root, "root-session"), (root / "profiles" / "p1", "p1-session"), (work, "work-session")):
        home.mkdir(parents=True, exist_ok=True)
        db = SessionDB(home / "state.db")
        db.create_session(sid, source="desktop")
        db.append_messages_batch(sid, [{"role": "user", "content": f"{sid} {i}"} for i in range(40)])
        db.close()
        _write(home / "config.yaml", f"model: {{default: {sid}}}\n")
        _write(home / "auth.json", json.dumps({"credential_pool": {"openai-codex": [{"refresh_token": "fake"}]}}))
    _write(root / ".env", "OPENAI_API_KEY=fake\n")
    _write(root / "auth.chains.json", json.dumps({"chains": {"c1": {"owner": str(work / "auth.json")}}}))
    _write(root / "auth.ops" / "op-1.json", "{}")
    _write(root / "auth.migration.lock", "")
    _write(root / "profiles" / "p1" / "auth.recovery" / "c1.json", "{}")
    _write(root / "profiles" / "p1" / "skills" / "research" / "SKILL.md", "---\nname: research\n---\n")
    _write(root / "profiles" / ".locks" / "p1.lock", "")
    for excluded in ("hermes-agent/scratch.db", "node_modules/pkg/x.db", "state-snapshots/s1/state.db"):
        (root / excluded).parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(root / excluded).close()
    _write(desktop / "session-windows-state.json", '{"windows": [{"sessionId": "root-session"}]}')
    _write(desktop / "window-state.json", "{}")
    _write(desktop / "Local Storage" / "leveldb" / "000003.log", "ui-state")
    _write(desktop / "Cache" / "Cache_Data" / "data_0", "regenerable")

    wal = sqlite3.connect(root / "compaction.db", isolation_level=None)
    wal.execute("PRAGMA journal_mode=WAL")
    wal.execute("PRAGMA wal_autocheckpoint=0")
    wal.execute("CREATE TABLE slots (id INTEGER PRIMARY KEY, holder TEXT)")
    wal.executemany("INSERT INTO slots (holder) VALUES (?)", [(f"s{i}",) for i in range(WAL_ROWS)])
    return root, work, desktop, wal


def _fingerprint(*trees: Path) -> dict:
    """Content of every source file, minus SQLite's own -wal/-shm sidecars."""
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for tree in trees for p in sorted(tree.rglob("*"))
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))}


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=REPO, env=_sealed_env(tmp_path / "os-home"),
                          capture_output=True, text=True, encoding="utf-8", timeout=300)


def test_backup_covers_every_home_leaves_sources_untouched_and_verifies_by_restoring(tmp_path):
    root, work, desktop, wal = _install(tmp_path)
    try:
        before = _fingerprint(root, work, desktop)
        out = tmp_path / "backup"
        run = _run(tmp_path, "--root", str(root), "--desktop-data", str(desktop), "--out", str(out))
        after = _fingerprint(root, work, desktop)
    finally:
        wal.close()
    assert run.returncode == 0 and run.stdout.rstrip().endswith("VERIFIED"), run.stdout + run.stderr
    assert after == before
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    by_path = {entry["path"]: entry for entry in manifest["entries"]}
    work_label = next(home["label"] for home in manifest["homes"] if Path(home["path"]) == work)
    assert set(by_path) == {
        "homes/root/state.db", "homes/root/compaction.db", "homes/root/config.yaml", "homes/root/.env",
        "homes/root/auth.json", "homes/root/auth.chains.json", "homes/root/auth.ops/op-1.json",
        "homes/profiles/p1/state.db", "homes/profiles/p1/config.yaml", "homes/profiles/p1/auth.json",
        "homes/profiles/p1/auth.recovery/c1.json", "homes/profiles/p1/skills/research/SKILL.md",
        f"homes/{work_label}/state.db", f"homes/{work_label}/config.yaml", f"homes/{work_label}/auth.json",
        "desktop/session-windows-state.json", "desktop/window-state.json", "desktop/Local Storage/leveldb/000003.log",
    }
    assert by_path["homes/root/compaction.db"]["tables"] == {"slots": WAL_ROWS}
    assert by_path["homes/profiles/p1/state.db"]["tables"]["messages"] == 40


def test_verify_rejects_a_backup_that_no_longer_restores_intact(tmp_path):
    root, _work, desktop, wal = _install(tmp_path)
    wal.close()
    out = tmp_path / "backup"
    assert _run(tmp_path, "--root", str(root), "--desktop-data", str(desktop), "--out", str(out)).returncode == 0
    db = out / "homes" / "root" / "state.db"
    data = bytearray(db.read_bytes())
    data[len(data) // 2] ^= 0xFF
    db.write_bytes(bytes(data))
    (out / "homes" / "profiles" / "p1" / "skills" / "research" / "SKILL.md").unlink()

    run = _run(tmp_path, "--verify", str(out))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "restored copy differs from the manifest: homes/root/state.db" in run.stdout
    assert "missing from the backup: homes/profiles/p1/skills/research/SKILL.md" in run.stdout


def test_refuses_an_output_directory_inside_the_install(tmp_path):
    root, _work, desktop, wal = _install(tmp_path)
    wal.close()
    run = _run(tmp_path, "--root", str(root), "--desktop-data", str(desktop), "--out", str(root / "backups-x"))
    assert run.returncode != 0 and "overlaps the source" in (run.stdout + run.stderr)
    assert not (root / "backups-x").exists()
