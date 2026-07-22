"""Phase 3 — read-only compaction queue slot-load diagnostic.

Two properties are non-negotiable and both are pinned here:

1. **It never creates the coordinator DB.** A diagnostic that materialises the
   thing it reports on makes "has the queue ever run here?" unanswerable, and
   would litter a fresh root with compaction.db/-wal/-shm just because somebody
   opened a dashboard.
2. **It never raises.** It renders in CLI/dashboard paths, so a missing or broken
   coordinator must degrade to ok=False + an error string, not take the page down.

And one subtlety that is easy to get wrong: a DB that exists but cannot be READ
must NOT report a healthy zero. "0 slots in use" and "I couldn't read it" look
identical to a renderer unless ok/error say otherwise — and conflating them would
hide a broken coordinator behind an idle-looking queue.
"""

import sqlite3
from unittest.mock import patch

import pytest

from agent import compaction_coordinator as cc
from agent.compaction_coordinator import release_compaction_slot, try_acquire_compaction_slot
from hermes_cli.kanban_diagnostics import compaction_queue_slot_load


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "hermes-root"
    r.mkdir()
    monkeypatch.setenv("HERMES_COMPACTION_HOME", str(r))
    monkeypatch.setattr(cc, "_logged_path", None, raising=False)
    return r


def _cfg(**queue):
    return {"compaction_queue": queue}


def _artefacts(root):
    return sorted(p.name for p in root.glob("compaction.db*"))


class TestColdRoot:
    def test_no_db_reports_empty_and_creates_nothing(self, root):
        out = compaction_queue_slot_load(_cfg(enabled=True, max_concurrent=2))

        assert out["ok"] is True
        assert out["error"] is None
        assert out["slots_in_use"] == 0
        assert out["holders"] == []
        assert out["enabled"] is True
        assert out["max_concurrent"] == 2
        assert out["slots_max"] == 2

        assert _artefacts(root) == [], (
            f"the diagnostic created {_artefacts(root)} — it must be read-only"
        )

    def test_repeated_calls_still_create_nothing(self, root):
        for _ in range(3):
            compaction_queue_slot_load(_cfg(enabled=True))
        assert _artefacts(root) == []

    def test_defaults_when_no_config_block(self, root):
        out = compaction_queue_slot_load({})
        assert out["ok"] is True
        assert out["enabled"] is False       # dark by default
        assert out["max_concurrent"] == 1
        assert out["slots_in_use"] == 0

    def test_malformed_config_is_clamped_not_fatal(self, root):
        out = compaction_queue_slot_load(_cfg(enabled="yes", max_concurrent=0))
        assert out["ok"] is True
        assert out["enabled"] is True
        assert out["max_concurrent"] == 1    # 0 would deadlock all compaction


class TestWithHeldSlots:
    def test_reports_a_held_slot_with_its_diagnostics_fields(self, root):
        held = try_acquire_compaction_slot(
            "sess-42", max_concurrent=1, profile="coder", source="kanban",
        )
        assert held.acquired

        out = compaction_queue_slot_load(_cfg(enabled=True, max_concurrent=1))

        assert out["ok"] is True
        assert out["slots_in_use"] == 1
        assert out["slots_max"] == 1
        assert len(out["holders"]) == 1

        h = out["holders"][0]
        assert h["session_id"] == "sess-42"
        assert h["profile"] == "coder"
        assert h["source"] == "kanban"
        assert h["slot_id"] == "0"
        assert h["holder"]

    def test_release_is_reflected(self, root):
        held = try_acquire_compaction_slot("s", max_concurrent=1)
        assert compaction_queue_slot_load(_cfg(enabled=True))["slots_in_use"] == 1

        release_compaction_slot(held.slot_id, held.holder)
        assert compaction_queue_slot_load(_cfg(enabled=True))["slots_in_use"] == 0

    def test_reports_root_wide_load_across_profiles(self, root):
        """The coordinator is ROOT-scoped: the diagnostic reports every profile."""
        try_acquire_compaction_slot("a", max_concurrent=2, profile="alpha")
        try_acquire_compaction_slot("b", max_concurrent=2, profile="beta")

        out = compaction_queue_slot_load(_cfg(enabled=True, max_concurrent=2))
        assert out["slots_in_use"] == 2
        assert {h["profile"] for h in out["holders"]} == {"alpha", "beta"}

    def test_max_concurrent_comes_from_config_not_the_db(self, root):
        try_acquire_compaction_slot("s", max_concurrent=1)
        out = compaction_queue_slot_load(_cfg(enabled=True, max_concurrent=4))
        assert out["slots_max"] == 4
        assert out["slots_in_use"] == 1


class TestFailureModes:
    def test_broken_coordinator_import_does_not_raise(self, root):
        with patch.dict("sys.modules", {"agent.compaction_coordinator": None}):
            out = compaction_queue_slot_load(_cfg(enabled=True))
        assert out["ok"] is False
        assert out["error"]
        assert out["slots_in_use"] is None
        assert out["holders"] == []
        # Config-derived fields still populated, so a renderer can show something.
        assert out["enabled"] is True
        assert out["max_concurrent"] == 1

    def test_unreadable_db_is_NOT_reported_as_a_healthy_zero(self, root):
        """The subtle one: a corrupt DB must not look like an idle queue."""
        (root / "compaction.db").write_bytes(b"this is not a sqlite database")

        out = compaction_queue_slot_load(_cfg(enabled=True))

        assert out["ok"] is False
        assert out["error"]
        assert out["slots_in_use"] is None, (
            "an unreadable coordinator reported 0 slots — indistinguishable from idle"
        )

    def test_coordinator_raising_does_not_propagate(self, root):
        with patch.object(cc, "get_compaction_slot_load",
                          side_effect=RuntimeError("boom")):
            out = compaction_queue_slot_load(_cfg(enabled=True))
        assert out["ok"] is False
        assert "boom" in out["error"]

    def test_disabled_queue_still_reports_load_honestly(self, root):
        """Disabled in config, but a stale slot row may still exist — report it."""
        try_acquire_compaction_slot("s", max_concurrent=1)
        out = compaction_queue_slot_load(_cfg(enabled=False))
        assert out["enabled"] is False
        assert out["ok"] is True
        assert out["slots_in_use"] == 1
