"""Windows reconnecting together over one checkout spawn ``git`` once for the branch, not once each.

Found by the shared-backend concurrency harness (2026-09-25): every ``session.resume`` builds a
``session.info`` whose branch came from a fresh ``git`` spawn; 27 windows reconnecting at once spent
most of the burst creating ``git`` processes (resume p95 2.5 s against the 2 s acceptance bar).
"""

from __future__ import annotations

import threading
import time

from tui_gateway import git_probe


def test_concurrent_branch_reads_share_one_git_spawn_and_see_a_switch_after_the_ttl(tmp_path, monkeypatch):
    calls = {"n": 0}
    current = {"branch": "main"}
    gate = threading.Event()

    def fake_git(argv, timeout):
        calls["n"] += 1
        gate.wait(timeout=5)  # hold the first spawn so every window's read overlaps it
        return current["branch"]

    monkeypatch.setattr(git_probe, "bounded_git_probe", fake_git)
    monkeypatch.setattr(git_probe, "_branch_cache", {}, raising=False)
    monkeypatch.setattr(git_probe, "_branch_inflight", {}, raising=False)
    monkeypatch.setattr(git_probe, "_BRANCH_TTL", 0.3, raising=False)
    results: list = []
    readers = [threading.Thread(target=lambda: results.append(git_probe.branch(str(tmp_path)))) for _ in range(27)]
    for t in readers:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in readers:
        t.join(timeout=10)

    assert results == ["main"] * 27
    assert calls["n"] == 1, f"{calls['n']} git spawns for 27 concurrent reads of one checkout"

    current["branch"] = "feature"
    time.sleep(0.4)
    assert git_probe.branch(str(tmp_path)) == "feature", "a branch switch must show once the TTL passes"
