"""Concurrent turns writing skill usage or memory queue on the lock; nothing is lost, nobody waits 1 s/turn.

Reproduced for T1 (2026-09-26, the lock audit both reviewers asked for): skill usage and the memory store
took msvcrt.LK_LOCK, which retries once a second and gives up after ten tries. Threads of one backend
contending through separate handles serialized at one acquisition per second (6 concurrent usage bumps:
5 s), and a fresh skill-usage lock file was padded by truncating it while a sibling thread held it
(PermissionError: the usage update silently dropped). Real modules against a temp HERMES_HOME.
"""

from __future__ import annotations

import json
import threading
import time

WRITERS = 12


def _run_concurrently(target) -> float:
    threads = [threading.Thread(target=target, args=(i,)) for i in range(WRITERS)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return time.monotonic() - started


def test_concurrent_skill_usage_bumps_all_land_without_one_second_steps(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / ".usage.json").write_text(json.dumps({f"skill-{i}": {"use_count": 0} for i in range(WRITERS)}),
                                                 encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    from tools import skill_usage

    # The lock file does not exist yet: the first acquirers race to create and pad it.
    elapsed = _run_concurrently(lambda i: skill_usage.bump_use(f"skill-{i}"))

    data = json.loads((home / "skills" / ".usage.json").read_text(encoding="utf-8"))
    lost = [i for i in range(WRITERS) if data[f"skill-{i}"].get("use_count") != 1]
    assert not lost, f"usage bumps lost under contention: {lost}"
    assert elapsed < 5.0, f"{WRITERS} concurrent bumps took {elapsed:.1f}s (one-per-second lock polling)"


def test_concurrent_memory_writers_queue_instead_of_polling(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from tools.memory_tool_store import MemoryStore

    target = home / "memories" / "MEMORY.md"
    inside, overlaps = [0], []

    def write(_i):
        with MemoryStore._file_lock(target):
            inside[0] += 1
            overlaps.append(inside[0])
            time.sleep(0.05)
            inside[0] -= 1

    elapsed = _run_concurrently(write)

    assert max(overlaps) == 1, "two writers held the memory lock at once"
    assert elapsed < 5.0, f"{WRITERS} memory writes took {elapsed:.1f}s (one-per-second lock polling)"
