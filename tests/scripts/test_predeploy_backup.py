"""scripts/predeploy_backup.py (deploy-gate checklist F7) on a synthetic sealed install.

The backup must cover the root and every profile home (including one registered outside ``profiles/``),
snapshot databases with SQLite (rows still only in the WAL included: a raw copy of the main file would lose
them) without SQLite ever opening a source (even read-only it rewrites the source's ``-shm``), carry
skills, the credential-protocol state wherever the protocol keeps it, and saved windows, and prove itself
by restoring into a scratch directory. A backup that could not read or copy something must never verify.
The script runs as a real subprocess with an allow-listed environment whose user dirs are sealed inside
the test (evals/desktop_bug_campaign/sealed_home).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

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
    """A root with one profile and a retired (dotted) profile directory, a home registered outside
    profiles/, a registered credential store under another file name outside every home, and desktop
    data. Returns the paths and an open connection holding WAL-only rows in the root compaction.db."""
    from hermes_state import SessionDB

    root, work, desktop = tmp_path / "hermes", tmp_path / "elsewhere" / "work", tmp_path / "AppData" / "Hermes"
    external = tmp_path / "elsewhere" / "external-owner"
    for home, sid in ((root, "root-session"), (root / "profiles" / "p1", "p1-session"), (work, "work-session")):
        home.mkdir(parents=True, exist_ok=True)
        db = SessionDB(home / "state.db")
        db.create_session(sid, source="desktop")
        db.append_messages_batch(sid, [{"role": "user", "content": f"{sid} {i}"} for i in range(40)])
        db.close()
        _write(home / "config.yaml", f"model: {{default: {sid}}}\n")
        _write(home / "auth.json", json.dumps({"credential_pool": {"openai-codex": [{"refresh_token": "fake"}]}}))
    _write(root / ".env", "OPENAI_API_KEY=fake\n")
    legacy = tmp_path / "elsewhere" / "legacy" / "auth.codex-rotation-rescue.json"
    _write(root / "auth.chains.json", json.dumps({"version": 1, "chains": {"c1": {
        "owner": str(work / "auth.json"), "state": "fenced", "epoch": 1,
        "rows": [{"row": "r1", "store": str(external / "credentials.json")}]}}, "ops": {},
        "legacy_rescues": {str(legacy): {"store": str(external / "credentials.json"), "chains": ["c1"]}}}))
    _write(legacy, "{}")
    _write(root / "auth.ops" / "op-1.json", "{}")
    _write(root / "auth.migration.lock", "")
    _write(root / "profiles" / "p1" / "auth.recovery" / "c1.json", "{}")
    _write(root / "profiles" / "p1" / "skills" / "research" / "SKILL.md", "---\nname: research\n---\n")
    _write(root / "profiles" / ".locks" / "p1.lock", "")
    _write(root / "profiles" / ".retired-old" / "auth.codex-rotation-rescue.json", "{}")
    _write(external / "credentials.json", "{}")
    _write(external / "auth.recovery" / "c1.json", "{}")
    _write(external / "auth.codex-rotation-rescue.json", "{}")
    _write(external / "unrelated.txt", "not credential state")
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
    return root, work, external, desktop, wal


def _fingerprint(*trees: Path) -> dict:
    """Content of every source file, SQLite's -wal/-shm sidecars included."""
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for tree in trees for p in sorted(tree.rglob("*")) if p.is_file()}


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=REPO, env=_sealed_env(tmp_path / "os-home"),
                          capture_output=True, text=True, encoding="utf-8", timeout=300)


def test_backup_covers_every_home_leaves_sources_untouched_and_verifies_by_restoring(tmp_path):
    root, work, external, desktop, wal = _install(tmp_path)
    try:
        before = _fingerprint(root, work.parent, desktop)
        out = tmp_path / "backup"
        run = _run(tmp_path, "--root", str(root), "--desktop-data", str(desktop), "--out", str(out))
        after = _fingerprint(root, work.parent, desktop)
    finally:
        wal.close()
    assert run.returncode == 0 and run.stdout.rstrip().endswith("VERIFIED"), run.stdout + run.stderr
    assert after == before
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    sources = {Path(entry["source"]).resolve().relative_to(tmp_path.resolve()).as_posix(): entry
               for entry in manifest["entries"]}
    assert set(sources) == {
        "hermes/state.db", "hermes/compaction.db", "hermes/config.yaml", "hermes/.env", "hermes/auth.json",
        "hermes/auth.chains.json", "hermes/auth.ops/op-1.json",
        "hermes/profiles/p1/state.db", "hermes/profiles/p1/config.yaml", "hermes/profiles/p1/auth.json",
        "hermes/profiles/p1/auth.recovery/c1.json", "hermes/profiles/p1/skills/research/SKILL.md",
        "hermes/profiles/.retired-old/auth.codex-rotation-rescue.json",
        "elsewhere/work/state.db", "elsewhere/work/config.yaml", "elsewhere/work/auth.json",
        "elsewhere/external-owner/credentials.json", "elsewhere/external-owner/auth.recovery/c1.json",
        "elsewhere/external-owner/auth.codex-rotation-rescue.json", "elsewhere/legacy/auth.codex-rotation-rescue.json",
        "AppData/Hermes/session-windows-state.json", "AppData/Hermes/window-state.json",
        "AppData/Hermes/Local Storage/leveldb/000003.log",
    }
    assert sources["hermes/compaction.db"]["tables"] == {"slots": WAL_ROWS}
    assert sources["hermes/profiles/p1/state.db"]["tables"]["messages"] == 40


@pytest.mark.parametrize("damage", ["zeroed_database", "unreadable_registry", "registry_without_sections",
                                    "registry_with_a_malformed_chain", "missing_extra_home"])
def test_an_incomplete_backup_never_verifies(tmp_path, damage):
    root, _work, _external, desktop, wal = _install(tmp_path)
    wal.close()
    args = ["--root", str(root), "--desktop-data", str(desktop)]
    if damage == "zeroed_database":
        (root / "profiles" / "p1" / "state.db").write_bytes(b"\0" * 4096)  # the #68474 zeroed signature
    elif damage == "unreadable_registry":
        (root / "auth.chains.json").write_text("{not json", encoding="utf-8")
    elif damage == "registry_without_sections":  # the credential protocol reads this as unreadable
        (root / "auth.chains.json").write_text("{}", encoding="utf-8")
    elif damage == "registry_with_a_malformed_chain":  # the store it names must not silently drop out
        (root / "auth.chains.json").write_text(json.dumps({"version": 1, "ops": {}, "chains": {
            "c1": str(tmp_path / "elsewhere" / "external-owner" / "credentials.json")}}), encoding="utf-8")
    else:
        args += ["--extra-home", str(tmp_path / "no-such-home")]
    out = tmp_path / "backup"

    run = _run(tmp_path, *args, "--out", str(out))
    reverify = _run(tmp_path, "--verify", str(out))
    assert run.returncode == 1 and "NOT VERIFIED" in run.stdout, run.stdout + run.stderr
    assert reverify.returncode == 1 and "backup incomplete" in reverify.stdout, reverify.stdout


def test_verify_rejects_a_backup_that_no_longer_restores_intact(tmp_path):
    root, _work, _external, desktop, wal = _install(tmp_path)
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
    root, _work, _external, desktop, wal = _install(tmp_path)
    wal.close()
    run = _run(tmp_path, "--root", str(root), "--desktop-data", str(desktop), "--out", str(root / "backups-x"))
    assert run.returncode != 0 and "overlaps the source" in (run.stdout + run.stderr)
    assert not (root / "backups-x").exists()


def _load_tool():
    spec = importlib.util.spec_from_file_location("predeploy_backup_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


def test_a_home_inside_the_desktop_data_keeps_its_database_verification(tmp_path):
    """The desktop may keep the Hermes home inside its data directory (HERMES_DESKTOP_USER_DATA_DIR/
    hermes-home): the desktop walk must not downgrade that home's databases to plain files, which skipped
    the snapshot and integrity check and let a corrupt database verify (F7 r2, Codex)."""
    desktop = tmp_path / "AppData" / "Hermes"
    root = desktop / "hermes-home"
    root.mkdir(parents=True)
    healthy = sqlite3.connect(root / "state.db")
    healthy.executescript("CREATE TABLE t (x); INSERT INTO t VALUES (1);")
    healthy.close()
    corrupt = sqlite3.connect(root / "kanban.db")
    corrupt.executescript("CREATE TABLE t (x TEXT); " + "INSERT INTO t VALUES (randomblob(3000));" * 20)
    corrupt.close()
    data = bytearray((root / "kanban.db").read_bytes())
    data[4096:8192] = bytes([0xFF]) * 4096  # header intact, a b-tree page destroyed
    (root / "kanban.db").write_bytes(bytes(data))
    out = tmp_path / "backup"

    run = _run(tmp_path, "--root", str(root), "--desktop-data", str(desktop), "--out", str(out))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    kinds = {Path(e["source"]).name: e["kind"] for e in manifest["entries"]}
    assert kinds["state.db"] == "sqlite"
    assert run.returncode == 1 and "kanban.db" in run.stdout, run.stdout + run.stderr


def test_a_database_that_disappears_after_the_inventory_is_a_failure_not_an_empty_backup(tmp_path):
    tool = _load_tool()
    root = tmp_path / "hermes"
    root.mkdir()
    conn = sqlite3.connect(root / "state.db")
    conn.executescript("CREATE TABLE t (x); INSERT INTO t VALUES (1);")
    conn.close()
    inv = tool.build_inventory(root, [], None)
    (root / "state.db").unlink()
    out = tmp_path / "backup"

    tool.backup(out, inv, None)
    problems = tool.verify(out)
    assert any("disappeared" in p and "state.db" in p for p in problems), problems
    assert not [e for e in json.loads((out / "manifest.json").read_text(encoding="utf-8"))["entries"]
                if e["kind"] == "sqlite"]
