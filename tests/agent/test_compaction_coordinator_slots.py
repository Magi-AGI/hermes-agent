"""Phase 0 — root-scoped compaction slot primitives.

Pure additions with no callers: the queue is still dark. These tests pin the
*semantics* the later wiring will depend on, and — critically — the cross-profile
concurrency regression that the original Gate 0 failure would have slipped past.

The single most important behaviour under test is the three-way outcome split:

    ACQUIRED           — you own it, you must release it
    DENIED             — genuinely full (a SUCCESSFUL observation)
    COORDINATOR_ERROR  — queue unusable → caller FAILS OPEN and compacts unbounded

A coordinator bug that reported DENIED instead of COORDINATOR_ERROR would defer
compaction forever, machine-wide — a permanent no-compaction stall, the exact
opposite of the fail-open contract. Several tests below exist only to nail that.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent import compaction_coordinator as cc
from agent.compaction_coordinator import (
    SCHEMA_VERSION,
    SlotOutcome,
    compaction_db_path,
    get_compaction_slot_load,
    get_schema_version,
    make_holder,
    refresh_compaction_slot,
    release_compaction_slot,
    try_acquire_compaction_slot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """Point the coordinator at a temp root (never the developer's real one)."""
    r = tmp_path / "hermes-root"
    r.mkdir()
    monkeypatch.setenv("HERMES_COMPACTION_HOME", str(r))
    # Reset the once-per-process path log so each test's resolution is observable.
    monkeypatch.setattr(cc, "_logged_path", None, raising=False)
    return r


# ── Schema / location ───────────────────────────────────────────────────────


class TestSchemaAndLocation:
    def test_ddl_is_created_in_the_ROOT_db_not_profile_state_db(self, root, tmp_path, monkeypatch):
        profile_home = tmp_path / "hermes-root" / "profiles" / "a"
        profile_home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))

        res = try_acquire_compaction_slot("s1")
        assert res.outcome is SlotOutcome.ACQUIRED

        db = root / "compaction.db"
        assert db.exists(), "coordinator DB must be created at the ROOT"
        assert compaction_db_path() == db

        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
        assert "compaction_slots" in tables

        # The profile-local state.db must be untouched — that substrate is rejected.
        assert not (profile_home / "state.db").exists()

    def test_db_is_created_lazily_on_first_use_not_on_import(self, root):
        assert not (root / "compaction.db").exists()
        try_acquire_compaction_slot("s1")
        assert (root / "compaction.db").exists()


# ── Acquire / deny / reclaim ────────────────────────────────────────────────


class TestAcquireAndDeny:
    def test_acquire_under_cap_succeeds(self, root):
        res = try_acquire_compaction_slot("s1", max_concurrent=2)
        assert res.outcome is SlotOutcome.ACQUIRED
        assert res.acquired is True
        assert res.slot_id == "0"
        assert res.holder
        assert res.slots_in_use == 1
        assert res.max_concurrent == 2
        assert res.expires_at > time.time()

    def test_second_acquire_takes_the_next_slot_id(self, root):
        a = try_acquire_compaction_slot("s1", max_concurrent=2)
        b = try_acquire_compaction_slot("s2", max_concurrent=2)
        assert {a.slot_id, b.slot_id} == {"0", "1"}
        assert b.slots_in_use == 2

    def test_acquire_at_cap_is_DENIED_not_an_error(self, root):
        first = try_acquire_compaction_slot("s1", max_concurrent=1)
        assert first.acquired

        denied = try_acquire_compaction_slot("s2", max_concurrent=1)
        assert denied.outcome is SlotOutcome.DENIED
        assert denied.coordinator_failed is False       # NOT an error
        assert denied.error is None
        assert denied.slot_id is None                   # nothing to release
        assert denied.slots_in_use == 1
        assert denied.max_concurrent == 1

    def test_max_concurrent_is_clamped_to_at_least_one(self, root):
        """0 would deadlock ALL compaction, violating fail-open."""
        res = try_acquire_compaction_slot("s1", max_concurrent=0)
        assert res.acquired
        assert res.max_concurrent == 1

    def test_expired_lease_is_reclaimed_by_the_next_acquirer(self, root):
        """A hard-killed backend must self-heal within one TTL."""
        dead = try_acquire_compaction_slot("dead", max_concurrent=1, ttl_seconds=1.0)
        assert dead.acquired

        # Same instant, cap reached → denied.
        assert try_acquire_compaction_slot("live", max_concurrent=1).outcome is SlotOutcome.DENIED

        # Now look from a point past the dead holder's TTL.
        later = time.time() + 5.0
        res = try_acquire_compaction_slot("live", max_concurrent=1, now=later)
        assert res.outcome is SlotOutcome.ACQUIRED
        assert res.reclaimed_expired == 1
        assert res.slot_id == "0"

    def test_diagnostics_fields_are_recorded(self, root):
        res = try_acquire_compaction_slot(
            "sess-1", profile="coder", source="kanban", max_concurrent=1,
        )
        assert res.acquired
        load = get_compaction_slot_load(max_concurrent=1)
        assert load.ok
        assert load.slots_in_use == 1
        holder = load.holders[0]
        assert holder["session_id"] == "sess-1"
        assert holder["profile"] == "coder"
        assert holder["source"] == "kanban"


# ── Release / refresh ───────────────────────────────────────────────────────


class TestReleaseAndRefresh:
    def test_release_frees_the_slot(self, root):
        a = try_acquire_compaction_slot("s1", max_concurrent=1)
        assert try_acquire_compaction_slot("s2", max_concurrent=1).outcome is SlotOutcome.DENIED

        rel = release_compaction_slot(a.slot_id, a.holder)
        assert rel.outcome is SlotOutcome.ACQUIRED  # "the row was ours and is gone"

        assert try_acquire_compaction_slot("s2", max_concurrent=1).acquired

    def test_release_is_idempotent(self, root):
        a = try_acquire_compaction_slot("s1")
        assert release_compaction_slot(a.slot_id, a.holder).outcome is SlotOutcome.ACQUIRED
        # Second release is a benign no-op, NOT an error.
        second = release_compaction_slot(a.slot_id, a.holder)
        assert second.outcome is SlotOutcome.DENIED
        assert second.coordinator_failed is False
        assert second.error is None

    def test_release_is_holder_scoped(self, root):
        """A stranger cannot release a slot TTL-reclaim may have reassigned."""
        a = try_acquire_compaction_slot("s1", max_concurrent=1)
        assert release_compaction_slot(a.slot_id, "someone-else").outcome is SlotOutcome.DENIED
        assert get_compaction_slot_load().slots_in_use == 1  # still held

    def test_refresh_extends_the_lease(self, root):
        a = try_acquire_compaction_slot("s1", ttl_seconds=10)
        r = refresh_compaction_slot(a.slot_id, a.holder, ttl_seconds=600)
        assert r.outcome is SlotOutcome.ACQUIRED
        assert r.expires_at > a.expires_at

        load = get_compaction_slot_load()
        assert load.holders[0]["expires_at"] == pytest.approx(r.expires_at)

    def test_refresh_of_a_slot_we_no_longer_hold_is_DENIED(self, root):
        a = try_acquire_compaction_slot("s1")
        release_compaction_slot(a.slot_id, a.holder)
        lost = refresh_compaction_slot(a.slot_id, a.holder)
        assert lost.outcome is SlotOutcome.DENIED
        assert lost.coordinator_failed is False  # lost ownership != coordinator broken

    def test_refresh_of_an_expired_lease_is_DENIED(self, root):
        a = try_acquire_compaction_slot("s1", ttl_seconds=1.0)
        lost = refresh_compaction_slot(a.slot_id, a.holder, now=time.time() + 5.0)
        assert lost.outcome is SlotOutcome.DENIED


# ── THE critical invariant: errors are NEVER denials ────────────────────────


class TestCoordinatorErrorNeverCollapsesToDenied:
    """A broken coordinator must FAIL OPEN, never look like a full queue.

    If any of these returned DENIED, the caller would defer compaction — forever,
    on every session, machine-wide. Degrading to unbounded compaction is always
    better than silently freezing it.
    """

    @pytest.mark.parametrize("boom", [
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.Error("generic sqlite failure"),
        PermissionError("root is not writable"),
        RuntimeError("something totally unexpected"),
        ValueError("module/version skew"),
    ])
    def test_acquire_maps_every_failure_to_COORDINATOR_ERROR(self, root, monkeypatch, boom):
        def _explode(*a, **k):
            raise boom

        monkeypatch.setattr(cc, "_connect", _explode)

        res = try_acquire_compaction_slot("s1", max_concurrent=1)
        assert res.outcome is SlotOutcome.COORDINATOR_ERROR, (
            f"{type(boom).__name__} was collapsed into {res.outcome} — a broken "
            f"coordinator that reports DENIED freezes compaction machine-wide."
        )
        assert res.coordinator_failed is True
        assert res.error and type(boom).__name__ in res.error
        assert res.slot_id is None

    def test_refresh_surfaces_coordinator_error_distinctly(self, root, monkeypatch):
        a = try_acquire_compaction_slot("s1")

        def _explode(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(cc, "_connect", _explode)
        res = refresh_compaction_slot(a.slot_id, a.holder)
        # Distinct from DENIED: a lease refresher must tolerate a transient blip
        # rather than concluding it lost ownership.
        assert res.outcome is SlotOutcome.COORDINATOR_ERROR
        assert res.coordinator_failed is True

    def test_release_surfaces_coordinator_error(self, root, monkeypatch):
        a = try_acquire_compaction_slot("s1")

        monkeypatch.setattr(cc, "_connect", lambda *a, **k: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk I/O error")))
        res = release_compaction_slot(a.slot_id, a.holder)
        assert res.outcome is SlotOutcome.COORDINATOR_ERROR
        # The lease still expires by TTL, so the slot self-heals regardless.

    def test_slot_load_reports_error_rather_than_a_false_zero(self, root, monkeypatch):
        """A broken read must not look like 'the queue is empty'.

        The DB is created first (via a real acquire) so this exercises a genuinely
        BROKEN read, not a missing one — those are different answers now that the
        diagnostic is read-only: missing DB legitimately means "no slots held",
        while an I/O error means "don't trust this number".
        """
        try_acquire_compaction_slot("s1")  # DB now exists

        monkeypatch.setattr(cc, "_open_readonly", lambda *a, **k: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk I/O error")))
        load = get_compaction_slot_load(max_concurrent=1)
        assert load.ok is False
        assert load.error
        assert load.slots_in_use == 0  # but ok=False tells the caller not to trust it

    def test_denied_is_only_returned_on_a_successful_observation(self, root):
        """Positive control: DENIED really does mean 'transaction succeeded, full'."""
        try_acquire_compaction_slot("s1", max_concurrent=1)
        res = try_acquire_compaction_slot("s2", max_concurrent=1)
        assert res.outcome is SlotOutcome.DENIED
        assert res.error is None
        assert res.slots_in_use == 1  # a real, observed count


# ── Malformed inputs must FAIL OPEN, never raise and never DENY ─────────────


class TestMalformedInputsFailOpen:
    """Config values reach these primitives from YAML.

    ``compaction_queue.max_concurrent: "one"`` (or null, or a nested dict) is a
    hand-edited-config reality. A raw int()/float() would throw straight out
    through compress_context and break the TURN. The correct degradation for any
    unusable coordinator input is the same as for a broken DB: COORDINATOR_ERROR
    → fail open → compact unbounded. Never DENIED (that would freeze compaction
    machine-wide), and never an escaping exception.
    """

    BAD = ["one", None, "", [], {}, object(), float("nan"), float("inf")]

    @pytest.mark.parametrize("bad", BAD)
    def test_bad_max_concurrent_is_coordinator_error(self, root, bad):
        res = try_acquire_compaction_slot("s1", max_concurrent=bad)
        assert res.outcome is SlotOutcome.COORDINATOR_ERROR
        assert res.coordinator_failed is True
        assert res.error
        assert res.slot_id is None

    @pytest.mark.parametrize("bad", BAD)
    def test_bad_ttl_is_coordinator_error(self, root, bad):
        res = try_acquire_compaction_slot("s1", ttl_seconds=bad)
        assert res.outcome is SlotOutcome.COORDINATOR_ERROR

    @pytest.mark.parametrize("bad", ["x", [], {}, float("nan")])
    def test_bad_now_is_coordinator_error(self, root, bad):
        res = try_acquire_compaction_slot("s1", now=bad)
        assert res.outcome is SlotOutcome.COORDINATOR_ERROR

    def test_bad_input_on_refresh_is_coordinator_error(self, root):
        a = try_acquire_compaction_slot("s1")
        res = refresh_compaction_slot(a.slot_id, a.holder, ttl_seconds="soon")
        assert res.outcome is SlotOutcome.COORDINATOR_ERROR
        assert res.coordinator_failed is True

    def test_bad_input_on_slot_load_reports_error_not_a_false_zero(self, root):
        load = get_compaction_slot_load(now="whenever")
        assert load.ok is False
        assert load.error

    def test_bad_input_NEVER_returns_denied(self, root):
        """The invariant, stated directly: malformed config must not look 'full'."""
        for bad in self.BAD:
            res = try_acquire_compaction_slot("s1", max_concurrent=bad)
            assert res.outcome is not SlotOutcome.DENIED, (
                f"max_concurrent={bad!r} produced DENIED — a malformed config would "
                f"silently freeze compaction machine-wide instead of failing open."
            )

    @pytest.mark.parametrize("value,expected", [(0, 1), (-5, 1), (1, 1), (3, 3), (2.9, 2)])
    def test_valid_numbers_are_clamped_not_rejected(self, root, value, expected):
        """A valid-but-non-positive number CLAMPS to 1 (0 would deadlock all
        compaction). Only NON-NUMERIC values are input errors."""
        res = try_acquire_compaction_slot("s1", max_concurrent=value)
        assert res.outcome is SlotOutcome.ACQUIRED
        assert res.max_concurrent == expected

    def test_bools_are_rejected_not_silently_treated_as_ints(self, root):
        """bool is an int subclass in Python — `max_concurrent: true` from YAML
        must not quietly mean 1."""
        assert try_acquire_compaction_slot(
            "s1", max_concurrent=True,
        ).outcome is SlotOutcome.COORDINATOR_ERROR


# ── Schema metadata / versioning ────────────────────────────────────────────


class TestDiagnosticsAreSideEffectFree:
    """A diagnostic must never materialise the coordinator it reports on.

    ``_connect()`` deliberately mkdirs the root, creates the DB, runs the DDL and
    stamps the schema version — right for the write path, wrong for a read. If a
    ``hermes doctor``-style call created ``compaction.db`` just by asking about
    it, "has the queue ever run here?" would become unanswerable, and a fresh
    root would be littered with compaction.db/-wal/-shm on inspection.
    """

    def _artefacts(self, root):
        return sorted(p.name for p in root.glob("compaction.db*"))

    def test_get_schema_version_on_missing_db_returns_None_and_creates_nothing(self, root):
        assert self._artefacts(root) == []          # genuinely cold

        assert get_schema_version() is None

        assert self._artefacts(root) == [], (
            f"get_schema_version() created {self._artefacts(root)} — a diagnostic "
            f"must not create the DB or its WAL/SHM sidecars."
        )
        assert not (root / "compaction.db").exists()
        assert not (root / "compaction.db-wal").exists()
        assert not (root / "compaction.db-shm").exists()

    def test_get_slot_load_on_missing_db_reports_empty_and_creates_nothing(self, root):
        assert self._artefacts(root) == []

        load = get_compaction_slot_load(max_concurrent=1)

        # "No DB" is a legitimate answer — no slots are held — NOT an error.
        assert load.ok is True
        assert load.slots_in_use == 0
        assert load.holders == []
        assert self._artefacts(root) == [], (
            f"get_compaction_slot_load() created {self._artefacts(root)}"
        )

    def test_repeated_diagnostics_never_create_the_db(self, root):
        for _ in range(3):
            get_schema_version()
            get_compaction_slot_load()
        assert self._artefacts(root) == []

    def test_diagnostics_still_work_after_the_db_exists(self, root):
        """The read-only path must not break the normal case."""
        a = try_acquire_compaction_slot("s1", profile="coder", max_concurrent=1)
        assert a.acquired

        assert get_schema_version() == SCHEMA_VERSION
        load = get_compaction_slot_load(max_concurrent=1)
        assert load.ok and load.slots_in_use == 1
        assert load.holders[0]["profile"] == "coder"

    def test_readonly_diagnostic_cannot_write(self, root):
        """mode=ro is belt-and-braces: even a race cannot resurrect an empty DB."""
        try_acquire_compaction_slot("s1")
        conn = cc._open_readonly()
        assert conn is not None
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO compaction_meta VALUES ('x','y')")
        finally:
            conn.close()

    def test_write_path_still_creates_the_db(self, root):
        """Sanity: only the WRITE path is allowed to materialise the coordinator."""
        assert not (root / "compaction.db").exists()
        try_acquire_compaction_slot("s1")
        assert (root / "compaction.db").exists()


class TestSchemaMetadata:
    def test_schema_version_is_stamped_on_first_use(self, root):
        assert get_schema_version() is None or get_schema_version() == SCHEMA_VERSION
        try_acquire_compaction_slot("s1")

        assert get_schema_version() == SCHEMA_VERSION

        conn = sqlite3.connect(str(root / "compaction.db"))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        row = conn.execute(
            "SELECT value FROM compaction_meta WHERE key='schema_version'").fetchone()
        conn.close()
        assert "compaction_meta" in tables
        assert int(row[0]) == SCHEMA_VERSION

    def test_existing_stamp_is_not_overwritten(self, root):
        """A future migration must read the OLD value, migrate, then bump it.

        INSERT OR IGNORE means a pre-existing (older) stamp survives contact with
        new code — otherwise the migration would have nothing to branch on.
        """
        try_acquire_compaction_slot("s1")
        db = root / "compaction.db"

        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE compaction_meta SET value='0' WHERE key='schema_version'")
        conn.commit()
        conn.close()

        try_acquire_compaction_slot("s2", max_concurrent=2)  # re-opens, re-runs DDL+stamp
        assert get_schema_version() == 0, "an existing schema stamp must not be clobbered"

    def test_schema_version_is_independent_of_state_db(self, root):
        """Different file, different lifecycle — the two must not be coupled.

        Asserted on BEHAVIOUR (what the module actually imports and stamps), not
        on a source-text grep: the module's own comments legitimately *mention*
        hermes_state.SCHEMA_VERSION to explain the independence.
        """
        import ast

        import hermes_state

        # 1. The coordinator stamps ITS OWN version, not state.db's.
        try_acquire_compaction_slot("s1")
        assert get_schema_version() == SCHEMA_VERSION

        # 2. It never imports state.db's schema version or its profile-local path.
        tree = ast.parse(Path(cc.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") == "hermes_state":
                imported.update(a.name for a in node.names)

        assert imported == {"apply_wal_with_fallback"}, (
            f"the coordinator must borrow ONLY the WAL helper from hermes_state; "
            f"importing schema/path symbols would re-couple it to the profile-local "
            f"substrate. Got: {imported}"
        )
        assert "SCHEMA_VERSION" not in imported
        assert "DEFAULT_DB_PATH" not in imported
        assert "SessionDB" not in imported

        # 3. Sanity: state.db's own version is untouched by our stamping.
        assert isinstance(hermes_state.SCHEMA_VERSION, int)


# ── Cross-profile, multi-process concurrency (THE regression test) ──────────


_WORKER = r"""
import json, os, sys, time
from agent.compaction_coordinator import (
    try_acquire_compaction_slot, release_compaction_slot, SlotOutcome,
)

start_at = float(os.environ["START_AT"])
hold_s   = float(os.environ["HOLD_S"])
label    = os.environ["LABEL"]

# Start together so a broken (per-profile) coordinator would genuinely overlap.
while time.time() < start_at:
    time.sleep(0.005)

acquired_from, released_at = None, None
deadline = time.time() + 30
while time.time() < deadline:
    res = try_acquire_compaction_slot(
        session_id=label, max_concurrent=1, ttl_seconds=60,
        profile=os.path.basename(os.environ.get("HERMES_HOME", "")), source="test",
    )
    if res.outcome is SlotOutcome.ACQUIRED:
        acquired_from = time.time()
        time.sleep(hold_s)          # HOLD the slot
        released_at = time.time()
        release_compaction_slot(res.slot_id, res.holder)
        break
    if res.outcome is SlotOutcome.COORDINATOR_ERROR:
        print(json.dumps({"label": label, "error": res.error}))
        sys.exit(1)
    time.sleep(0.02)                # DENIED → poll again

print(json.dumps({
    "label": label,
    "hermes_home": os.environ.get("HERMES_HOME", ""),
    "acquired_at": acquired_from,
    "released_at": released_at,
}))
"""


class TestCrossProfileConcurrency:
    """≥2 profiles under ONE root, max_concurrent=1 — holds must NOT overlap.

    This is the regression test for the original Gate 0 bug. Note carefully why
    it asserts NON-OVERLAPPING TIME INTERVALS rather than "each holder saw
    slots_in_use == 1": under the REJECTED profile-local substrate each process
    would have its own coordinator DB, so each would acquire instantly and each
    would observe a count of exactly 1 — and a count-based assertion would pass
    while the bound did nothing. Only the overlap check distinguishes a real
    machine-wide bound from N independent per-profile semaphores.
    """

    def _run(self, root, homes, hold_s=0.6):
        start_at = time.time() + 2.0
        procs = []
        for label, home in homes.items():
            env = dict(os.environ)
            env.update({
                "HERMES_HOME": str(home),
                "PYTHONPATH": str(REPO_ROOT),
                "START_AT": str(start_at),
                "HOLD_S": str(hold_s),
                "LABEL": label,
            })
            # DELIBERATELY NOT setting HERMES_COMPACTION_HOME. The override
            # short-circuits compaction_home() before get_default_hermes_root() is
            # ever consulted — so pinning it here would force every child to the
            # same file and the test would pass even against the REJECTED
            # profile-scoped resolution. It would mask the very bug it exists to
            # catch. Instead we rely on genuine root resolution from each child's
            # divergent HERMES_HOME (the temp root is outside ~/.hermes, so
            # get_default_hermes_root() exercises its real profiles-parent branch).
            env.pop("HERMES_COMPACTION_HOME", None)
            procs.append((label, subprocess.Popen(
                [sys.executable, "-c", _WORKER],
                cwd=str(REPO_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )))

        results = []
        for label, p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, f"{label} failed: {out}\n{err}"
            line = [ln for ln in out.strip().splitlines() if ln.startswith("{")][-1]
            results.append(json.loads(line))
        return results

    def test_at_most_one_slot_is_held_across_profiles_at_any_instant(self, tmp_path):
        root = tmp_path / "hermes-root"
        homes = {
            "root-agent": root,
            "worker-a": root / "profiles" / "a",     # e.g. kanban task assigned to 'a'
            "backend-b": root / "profiles" / "b",    # e.g. desktop session on profile 'b'
        }
        for h in homes.values():
            h.mkdir(parents=True, exist_ok=True)

        results = self._run(root, homes)

        # All three eventually compacted — no deadlock, no starvation.
        assert len(results) == 3
        for r in results:
            assert r["acquired_at"] and r["released_at"], f"{r['label']} never acquired"

        # THE ASSERTION: hold intervals must not overlap. Under the rejected
        # per-profile substrate all three would hold simultaneously.
        spans = sorted(
            [(r["acquired_at"], r["released_at"], r["label"]) for r in results]
        )
        for (a_start, a_end, a_lbl), (b_start, b_end, b_lbl) in zip(spans, spans[1:]):
            assert b_start >= a_end, (
                f"max_concurrent=1 violated across profiles: {a_lbl} held "
                f"[{a_start:.3f},{a_end:.3f}] while {b_lbl} acquired at {b_start:.3f}. "
                f"The bound is not machine-wide — this is the per-profile fork bug."
            )

        # And they really did run under different profiles (fixture sanity).
        assert len({r["hermes_home"] for r in results}) == 3

        # One coordinator file, at the root.
        assert (root / "compaction.db").exists()
        assert not (root / "profiles" / "a" / "compaction.db").exists()
        assert not (root / "profiles" / "b" / "compaction.db").exists()


# ── First-use / schema-creation contention ─────────────────────────────────

_FIRST_USE_WORKER = r"""
import json, os, sys, time
from agent.compaction_coordinator import (
    try_acquire_compaction_slot, release_compaction_slot, get_schema_version, SlotOutcome,
)

start_at = float(os.environ["START_AT"])
label    = os.environ["LABEL"]

# Every process slams the EMPTY root at the same instant, so they race
# file creation + WAL pragma + DDL + the schema stamp, not just the INSERT.
while time.time() < start_at:
    time.sleep(0.002)

res = try_acquire_compaction_slot(
    session_id=label, max_concurrent=1, ttl_seconds=60, source="first-use",
)
outcome = res.outcome.value
if res.outcome is SlotOutcome.ACQUIRED:
    time.sleep(0.05)
    release_compaction_slot(res.slot_id, res.holder)

print(json.dumps({
    "label": label,
    "outcome": outcome,
    "error": res.error,
    "schema_version": get_schema_version(),
}))
"""


class TestFirstUseSchemaContention:
    """Many processes racing an EMPTY root must not produce spurious bypasses.

    _connect() does file creation + WAL pragma + DDL + schema stamp OUTSIDE the
    BEGIN IMMEDIATE retry loop. Ordinary SQLite lock contention there must be
    absorbed (SQLite's own busy_timeout covers it), NOT surfaced as
    COORDINATOR_ERROR — because COORDINATOR_ERROR means "bypass the queue and
    compact unbounded". A herd cold-starting on a fresh machine is exactly when
    the bound matters most; if first-use contention made everyone fail open, the
    queue would be useless precisely at the moment of peak load.

    So: every process must land on ACQUIRED or DENIED. Zero COORDINATOR_ERROR.
    """

    def test_concurrent_first_use_never_yields_coordinator_error(self, tmp_path):
        root = tmp_path / "cold-root"
        root.mkdir()
        assert not (root / "compaction.db").exists()  # genuinely cold

        n = 8
        start_at = time.time() + 2.0
        procs = []
        for i in range(n):
            env = dict(os.environ)
            env.update({
                "HERMES_HOME": str(root),
                "HERMES_COMPACTION_HOME": str(root),
                "PYTHONPATH": str(REPO_ROOT),
                "START_AT": str(start_at),
                "LABEL": f"p{i}",
            })
            procs.append(subprocess.Popen(
                [sys.executable, "-c", _FIRST_USE_WORKER],
                cwd=str(REPO_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))

        results = []
        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, f"worker crashed: {out}\n{err}"
            line = [ln for ln in out.strip().splitlines() if ln.startswith("{")][-1]
            results.append(json.loads(line))

        errors = [r for r in results if r["outcome"] == "coordinator_error"]
        assert not errors, (
            "first-use/schema contention produced COORDINATOR_ERROR — those "
            "processes would BYPASS the queue and compact unbounded, exactly when "
            f"the bound matters most (cold-start herd): {errors}"
        )

        outcomes = {r["outcome"] for r in results}
        assert outcomes <= {"acquired", "denied"}, outcomes
        # At least one got through, and the cap was respected at every instant.
        assert sum(r["outcome"] == "acquired" for r in results) >= 1

        # The schema stamp survived the race exactly once, with the right value.
        assert all(r["schema_version"] == SCHEMA_VERSION for r in results)
        conn = sqlite3.connect(str(root / "compaction.db"))
        rows = conn.execute(
            "SELECT COUNT(*) FROM compaction_meta WHERE key='schema_version'").fetchone()[0]
        conn.close()
        assert rows == 1, "concurrent first-use must not duplicate the schema stamp"
