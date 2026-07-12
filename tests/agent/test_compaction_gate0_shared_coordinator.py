"""GATE 0 — executable proof that the compaction coordinator path is SHARED.

This is the blocking gate from ``docs/plans/2026-07-11-compaction-queue-spec.md``
§9.0-GATE. **No slot/queue code may be written until it passes.**

What it proves
--------------
Every process in the compaction "herd" — a gateway/desktop backend serving a
non-launch profile, a kanban worker (whose ``HERMES_HOME`` is its task's
*assignee profile*), and a root/CLI agent — resolves the **same** root-scoped
coordinator path, *despite* each running under a **different** ``HERMES_HOME``.

Why it is shaped this way (do not "simplify" it)
------------------------------------------------
* **Real subprocesses, not threads and not mocks.** The defect class being
  guarded is *path resolution driven by process environment*. A thread shares the
  parent's ``os.environ``; a mock of ``get_hermes_home`` would assert nothing at
  all. Only a real ``HERMES_HOME=...`` child proves the property.
* **Cross-profile, not same-profile.** A same-profile test passes just as happily
  against the *rejected* profile-local ``state.db`` substrate, so it would prove
  nothing. The divergence is the whole point.
* **A positive equality assertion, plus a divergence contrast.** The failure mode
  here is *silent*: a mis-scoped coordinator fails open, so every acquire would
  succeed and every log would look healthy while the bound did nothing. So we
  assert the coordinator paths are EQUAL **and** that the profile-local
  ``state.db`` paths DIFFER. Without the contrast the test is tautological — it
  would pass even if the helper returned a constant for the wrong reason.
* **No ``HERMES_COMPACTION_HOME`` override in the main proof.** The override
  exists for tests, but using it here would test the override rather than the
  production resolution. A temp root outside ``~/.hermes`` already exercises the
  real ``get_default_hermes_root()`` path (its ``parent.name == "profiles"``
  branch), so the main gate runs against genuine production logic. The override
  gets its own separate test.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Each herd process reports: the HERMES_HOME it was launched with, the coordinator
# path it resolves, and — for contrast — the profile-local state.db path that the
# REJECTED substrate would have used. hermes_state.DEFAULT_DB_PATH is read rather
# than recomputed, because that module-level constant IS the thing that made the
# old design profile-scoped.
_PROBE = """
import json, os, sys
from agent.compaction_coordinator import compaction_db_path
import hermes_state
print(json.dumps({
    "hermes_home": os.environ.get("HERMES_HOME", ""),
    "coordinator": str(compaction_db_path()),
    "state_db": str(hermes_state.DEFAULT_DB_PATH),
}))
"""


def _run_probe(hermes_home: Path, script: str = _PROBE, extra_env=None) -> dict:
    """Run the probe in a REAL subprocess under the given HERMES_HOME."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(hermes_home)
    # Ensure the child never inherits a stray override from the developer's shell.
    env.pop("HERMES_COMPACTION_HOME", None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"probe failed (rc={proc.returncode})\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    # The agent package can emit warnings on stderr; parse the last stdout line.
    line = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")][-1]
    return json.loads(line)


@pytest.fixture()
def herd(tmp_path):
    """One temp Hermes root with two distinct profiles, plus the root itself.

    Mirrors the real herd:
      * <root>              — CLI / gateway launched at the root profile
      * <root>/profiles/a   — e.g. a kanban worker for a task assigned to 'a'
      * <root>/profiles/b   — e.g. a desktop backend session bound to profile 'b'
    """
    root = tmp_path / "hermes-root"
    (root / "profiles" / "a").mkdir(parents=True)
    (root / "profiles" / "b").mkdir(parents=True)
    return {
        "root": root,
        "a": root / "profiles" / "a",
        "b": root / "profiles" / "b",
    }


class TestGate0SharedCoordinatorPath:
    def test_all_herd_processes_resolve_the_same_coordinator_path(self, herd):
        """THE GATE. Three real processes, three different HERMES_HOME values,
        one coordinator path."""
        root_proc = _run_probe(herd["root"])
        worker_a = _run_probe(herd["a"])
        backend_b = _run_probe(herd["b"])

        expected = herd["root"] / "compaction.db"

        # 1. Every herd process resolves the SAME coordinator path...
        assert root_proc["coordinator"] == worker_a["coordinator"] == backend_b["coordinator"], (
            "Herd processes resolved DIFFERENT coordinator paths — the semaphore would "
            "silently fork per profile and bound nothing:\n"
            f"  root       -> {root_proc['coordinator']}\n"
            f"  profiles/a -> {worker_a['coordinator']}\n"
            f"  profiles/b -> {backend_b['coordinator']}"
        )

        # 2. ...and it is the root-scoped path we intended (not merely "equal").
        assert Path(root_proc["coordinator"]) == expected

        # 3. THE CONTRAST that makes this non-tautological: the profile-local
        #    state.db — the REJECTED substrate — genuinely diverges across the
        #    same three processes. This is the bug Gate 0 caught, made executable.
        state_dbs = {root_proc["state_db"], worker_a["state_db"], backend_b["state_db"]}
        assert len(state_dbs) == 3, (
            "Expected the profile-local state.db paths to DIVERGE across profiles. "
            "If they don't, this fixture isn't reproducing the real herd and the "
            f"gate proves nothing. Got: {state_dbs}"
        )
        assert Path(worker_a["state_db"]) == herd["a"] / "state.db"
        assert Path(backend_b["state_db"]) == herd["b"] / "state.db"

    def test_probe_sanity_each_process_really_ran_under_its_own_profile(self, herd):
        """Guard against a fixture bug silently making all three identical."""
        assert _run_probe(herd["a"])["hermes_home"] == str(herd["a"])
        assert _run_probe(herd["b"])["hermes_home"] == str(herd["b"])


class TestCallTimeResolution:
    """The import-time trap (spec §3.1) — the coordinator must NOT freeze its path.

    ``hermes_state.DEFAULT_DB_PATH`` is evaluated at *module import*, and
    ``set_hermes_home_override()`` is a ContextVar that does not mutate
    ``os.environ`` — so it never updates that constant. If the coordinator cached
    its path the same way, a gateway process that imports under one profile and
    then serves a session bound to another would coordinate against a stale path.
    """

    _REBIND_PROBE = """
import json, os
from pathlib import Path

# Import the coordinator under profile 'a'...
from agent.compaction_coordinator import compaction_db_path
import hermes_state

first = str(compaction_db_path())
frozen_state_db = str(hermes_state.DEFAULT_DB_PATH)   # captured at import

# ...then REBIND the environment to profile 'b', exactly as a gateway does when
# it binds a session to a non-launch profile.
os.environ["HERMES_HOME"] = os.environ["REBIND_TO"]

second = str(compaction_db_path())                     # must re-resolve at CALL time
state_db_after = str(hermes_state.DEFAULT_DB_PATH)     # still frozen — the trap

print(json.dumps({
    "first": first,
    "second": second,
    "frozen_state_db": frozen_state_db,
    "state_db_after": state_db_after,
}))
"""

    def test_coordinator_resolves_at_call_time_not_import_time(self, herd):
        out = _run_probe(
            herd["a"],
            script=self._REBIND_PROBE,
            extra_env={"REBIND_TO": str(herd["b"])},
        )
        expected = str(herd["root"] / "compaction.db")

        # Re-resolved after the rebind, and still the shared root path. (Root
        # anchoring means the value is stable across the rebind by design — which
        # is exactly the property the queue depends on.)
        assert out["first"] == expected
        assert out["second"] == expected

        # And the control: state.db's import-time constant is demonstrably FROZEN
        # at the launch profile even after HERMES_HOME changed. This proves the
        # trap is real (not hypothetical), and that the coordinator avoids it.
        assert out["frozen_state_db"] == out["state_db_after"]
        assert Path(out["frozen_state_db"]) == herd["a"] / "state.db"

    # ── The STRONG call-time tests (Codex note, 005b) ────────────────────────
    #
    # The rebind test above moves between two profiles under the SAME root. Because
    # the coordinator is root-anchored, both resolve to the same value — so a module
    # that had CACHED its path at import would still pass it. That test proves the
    # herd property, but it does NOT prove call-time resolution.
    #
    # These two do: they rebind across DIFFERENT ROOTS after import, so the expected
    # value genuinely changes. A cached path fails them.

    _CROSS_ROOT_PROBE = """
import json, os
from agent.compaction_coordinator import compaction_db_path   # imported under root A

before = str(compaction_db_path())

# Rebind to a COMPLETELY DIFFERENT root after import.
os.environ["HERMES_HOME"] = os.environ["REBIND_ROOT_B"]

after = str(compaction_db_path())
print(json.dumps({"before": before, "after": after}))
"""

    def test_call_time_resolution_across_DIFFERENT_roots(self, herd, tmp_path):
        """Import under root A, rebind HERMES_HOME to root B, re-resolve.

        This is the test that actually falsifies a cached path: the expected value
        CHANGES across the rebind, so a module-level constant captured at import
        cannot satisfy both assertions.
        """
        root_b = tmp_path / "hermes-root-b"
        root_b.mkdir()

        out = _run_probe(
            herd["a"],  # import under root A (via profiles/a)
            script=self._CROSS_ROOT_PROBE,
            extra_env={"REBIND_ROOT_B": str(root_b)},
        )

        assert Path(out["before"]) == herd["root"] / "compaction.db"
        assert Path(out["after"]) == root_b / "compaction.db", (
            "compaction_db_path() did not re-resolve after HERMES_HOME moved to a "
            "different ROOT — the module is caching its path at import time, which "
            "is exactly the hermes_state.DEFAULT_DB_PATH trap this design must avoid."
        )
        assert out["before"] != out["after"]

    _OVERRIDE_REBIND_PROBE = """
import json, os
from agent.compaction_coordinator import compaction_db_path   # imported with NO override

before = str(compaction_db_path())

# Set the override AFTER import — a cached path would ignore it entirely.
os.environ["HERMES_COMPACTION_HOME"] = os.environ["LATE_OVERRIDE"]

after = str(compaction_db_path())
print(json.dumps({"before": before, "after": after}))
"""

    def test_compaction_home_override_applied_AFTER_import_is_honoured(self, herd, tmp_path):
        """The override must be read at call time too, not captured at import."""
        late_root = tmp_path / "late-override-root"
        late_root.mkdir()

        out = _run_probe(
            herd["a"],
            script=self._OVERRIDE_REBIND_PROBE,
            extra_env={"LATE_OVERRIDE": str(late_root)},
        )

        assert Path(out["before"]) == herd["root"] / "compaction.db"
        assert Path(out["after"]) == late_root / "compaction.db", (
            "HERMES_COMPACTION_HOME set after import was ignored — the override is "
            "being captured at import time rather than read at call time."
        )

    _OVERRIDE_PROBE = """
import json, os
from agent.compaction_coordinator import compaction_db_path
print(json.dumps({"coordinator": str(compaction_db_path())}))
"""

    def test_compaction_home_override_is_honoured_and_call_time(self, herd, tmp_path):
        """HERMES_COMPACTION_HOME relocates the coordinator (tests / Docker)."""
        override_root = tmp_path / "override-root"
        override_root.mkdir()
        out = _run_probe(
            herd["a"],
            script=self._OVERRIDE_PROBE,
            extra_env={"HERMES_COMPACTION_HOME": str(override_root)},
        )
        assert Path(out["coordinator"]) == override_root / "compaction.db"


class TestNoQueueYet:
    """Scope discipline.

    Step B was path-helper only. Step C (Phase 0) adds the slot primitives — so
    those symbols are now EXPECTED. What must still NOT exist is any activation
    surface: no ``compaction_queue`` config, and no caller wiring the coordinator
    into compression. The queue stays dark until a separate, user-approved step.
    """

    def test_slot_primitives_exist_after_phase_0(self):
        from agent import compaction_coordinator as cc

        for expected in (
            "try_acquire_compaction_slot",
            "release_compaction_slot",
            "refresh_compaction_slot",
            "get_compaction_slot_load",
            "SlotOutcome",
            "SlotResult",
        ):
            assert hasattr(cc, expected), f"Phase 0 must provide {expected}"

    def test_compaction_queue_config_exists_but_is_DARK(self):
        """Phase 1 adds the config block — but it must default to disabled.

        (This guard previously asserted the block did not exist at all. Phase 1
        deliberately introduces it; what must NOT change is that it ships off.)
        """
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["compaction_queue"]["enabled"] is False

    def test_the_coordinator_is_imported_only_by_the_compaction_path(self):
        """Phase 2 wires the queue into compress_context — and nowhere else.

        (This previously asserted NO importers at all. Phase 2 legitimately adds
        one; what must not happen is the coordinator leaking into the rest of the
        runtime. The queue stays behaviourally dark by CONFIG — enabled defaults to
        false — not by absence of a caller.)
        """
        import subprocess

        out = subprocess.run(
            ["git", "grep", "-nE",
             r"^\s*(from +agent +import +compaction_coordinator"
             r"|from +agent\.compaction_coordinator +import"
             r"|import +agent\.compaction_coordinator)",
             "--", "agent", "hermes_cli", "gateway", "tui_gateway"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        ).stdout.strip()
        importers = {ln.split(":", 1)[0] for ln in out.splitlines() if ln}
        assert importers <= {"agent/conversation_compression.py"}, (
            f"the coordinator is imported outside the compaction path: {sorted(importers)}"
        )

    def test_helper_opens_no_database(self, herd, monkeypatch):
        """Resolving the path must not create or touch ANY DB file.

        Step B is path resolution only — no sqlite3.connect, no schema, no WAL. If
        importing the module or calling the helper materialised a DB (or its WAL/SHM
        sidecars), the "no behaviour change, ships dark" guarantee would be false:
        the live InstallDir backend would start writing files on restart.

        monkeypatch (not raw os.environ mutation) so a developer who genuinely has
        HERMES_COMPACTION_HOME set in their shell has it preserved and restored.
        """
        from agent.compaction_coordinator import compaction_db_path

        monkeypatch.setenv("HERMES_COMPACTION_HOME", str(herd["root"]))

        path = compaction_db_path()
        assert path == herd["root"] / "compaction.db"

        # Neither the DB nor its SQLite sidecars may exist — resolution is pure.
        for sidecar in ("", "-wal", "-shm"):
            probe = Path(str(path) + sidecar)
            assert not probe.exists(), (
                f"path resolution created {probe.name} — the Step B helper must not "
                f"open, create, or touch a database."
            )

        # And the root itself must contain no compaction artefacts at all.
        assert not list(herd["root"].glob("compaction.db*")), (
            f"unexpected compaction artefacts in {herd['root']}: "
            f"{[p.name for p in herd['root'].glob('compaction.db*')]}"
        )

    _SIDE_EFFECT_PROBE = """
import json, os
from pathlib import Path

root = Path(os.environ["HERMES_COMPACTION_HOME"])

# Import alone must create nothing...
from agent.compaction_coordinator import compaction_db_path
after_import = sorted(p.name for p in root.glob("compaction.db*"))

# ...and neither must resolving the path, repeatedly.
for _ in range(3):
    compaction_db_path()
after_calls = sorted(p.name for p in root.glob("compaction.db*"))

print(json.dumps({"after_import": after_import, "after_calls": after_calls}))
"""

    def test_import_and_resolution_create_no_files_in_a_real_process(self, herd, tmp_path):
        """Same no-side-effect guarantee, proven in a REAL subprocess.

        The in-process test above can be masked by the module already being imported
        by an earlier test. A fresh interpreter proves import itself is inert.
        """
        clean_root = tmp_path / "clean-root"
        clean_root.mkdir()

        out = _run_probe(
            herd["a"],
            script=self._SIDE_EFFECT_PROBE,
            extra_env={"HERMES_COMPACTION_HOME": str(clean_root)},
        )

        assert out["after_import"] == [], (
            f"importing agent.compaction_coordinator created files: {out['after_import']}"
        )
        assert out["after_calls"] == [], (
            f"resolving compaction_db_path() created files: {out['after_calls']}"
        )
        assert not list(clean_root.iterdir()), (
            f"the coordinator root should still be empty, found: "
            f"{[p.name for p in clean_root.iterdir()]}"
        )
