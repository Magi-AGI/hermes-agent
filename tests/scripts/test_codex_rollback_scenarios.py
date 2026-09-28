"""The rollback preparation against the REAL older build (deploy gate F8), real processes.

Runs ``scripts/codex_rollback_scenarios.py`` with the old build's checkout named by
``HERMES_TEST_ROLLBACK_OLD_TREE`` (a worktree of the deployed commit; the test is skipped without
one). Every step is recorded by a local single-use token endpoint; the script fails on any replayed
token in the asserted scenarios, and its CONTROL scenarios fail unless the old build DOES replay
without the preparation (so the oracle is known to detect the hazard). Fake credentials, loopback
only, throwaway Hermes roots.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OLD_TREE = os.environ.get("HERMES_TEST_ROLLBACK_OLD_TREE", "")


@pytest.mark.skipif(not OLD_TREE, reason="HERMES_TEST_ROLLBACK_OLD_TREE (a checkout of the deployed build) not set")
def test_no_consumed_codex_token_reaches_the_old_build(tmp_path):
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "codex_rollback_scenarios.py"), "--old-tree", OLD_TREE,
         "--only", "b0,c0,a,b,c,d,e,f,h", "--crash-points", "settle-adopted,op-apply,between-ops,fixes,receipt",
         "--out", str(out)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=1200)
    assert out.exists(), proc.stdout[-3000:] + proc.stderr[-3000:]
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["failures"] == [], json.dumps(report["failures"], indent=2)
    assert proc.returncode == 0
