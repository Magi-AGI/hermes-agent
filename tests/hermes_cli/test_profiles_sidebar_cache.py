"""Regression tests for dashboard sidebar scan coalescing."""

import inspect
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from hermes_cli.web_routers import profiles

# Hang guard for the burst tests below; every wait in them ends on an event.
_HANG_GUARD = 30.0
_REAL_LOCK = threading.Lock


class _LockWatch:
    """Stands in for ``threading`` while the single-flight decorator builds its locks, and records
    which threads are between asking for one of them and getting it. ``scan_started()`` (called
    from the fake scan) counts scans and notes the locks the scanning thread holds at that moment;
    a thread still pending on one of those is blocked behind the in-flight scan."""

    def __init__(self):
        self.cond = threading.Condition(_REAL_LOCK())
        self.scans = 0
        self._scan_locks = ()
        self._locks = []
        self._pending = {}

    def Lock(self):  # noqa: N802 - the threading.Lock factory the decorator calls
        lock = _WatchedLock(self)
        self._locks.append(lock)
        return lock

    def __getattr__(self, name):
        return getattr(threading, name)

    def scan_started(self):
        me = threading.get_ident()
        with self.cond:
            self.scans += 1
            if self.scans == 1:
                self._scan_locks = tuple(lock for lock in self._locks if lock.owner == me)
            self.cond.notify_all()

    def pending(self, lock, thread_id, is_pending):
        with self.cond:
            waiters = self._pending.setdefault(lock, set())
            (waiters.add if is_pending else waiters.discard)(thread_id)
            self.cond.notify_all()

    def wait_for_burst(self, followers, timeout):
        def ready():
            waiting = set().union(*(self._pending.get(lock, set()) for lock in self._scan_locks))
            return self.scans > 1 or len(waiting) >= followers

        with self.cond:
            return self.cond.wait_for(ready, timeout)


class _WatchedLock:
    def __init__(self, watch):
        self._lock, self._watch, self.owner = _REAL_LOCK(), watch, None

    def acquire(self, blocking=True, timeout=-1):
        # Pending from before the real acquire until after ownership is published, with no check of
        # who holds the lock first: that check raced the holder publishing itself (Codex r2).
        me = threading.get_ident()
        self._watch.pending(self, me, True)
        try:
            acquired = self._lock.acquire(blocking, timeout)
            if acquired:
                self.owner = me
            return acquired
        finally:
            self._watch.pending(self, me, False)

    def release(self):
        self.owner = None
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class SidebarCacheTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 5.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        profiles._sidebar_profile_cache_clear()
        self.addCleanup(profiles._sidebar_profile_cache_clear)

    def _burst(self, watch, workers, call, release):
        """Run ``call`` from ``workers`` threads and hold the one scan until every other caller is
        queued on a lock that scan holds, i.e. has missed the cache while the scan was in flight.
        (A 1 s wait for the first entry plus a 50 ms guess at the rest raced a loaded runner; an
        arrival signal before the call let a caller that came late read the finished cache and
        never meet the single-flight lock.) A second scan ends the wait at once and fails below.
        The long TTL keeps a queued caller from outliving the cached result."""
        with mock.patch.object(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 3600.0), \
                ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(call) for _ in range(workers)]
            try:
                self.assertTrue(watch.wait_for_burst(workers - 1, timeout=_HANG_GUARD))
            finally:
                release.set()
            results = [future.result(timeout=_HANG_GUARD) for future in futures]
        self.assertEqual(watch.scans, 1)
        return results

    def test_profile_cache_uses_db_and_wal_fingerprint_and_defensive_copies(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "state.db"
            wal_path = Path(f"{db_path}-wal")
            db_path.write_bytes(b"db-v1")
            wal_path.write_bytes(b"wal-v1")
            first_fingerprint = profiles._sidebar_db_fingerprint(db_path)
            first_key = (str(db_path), first_fingerprint, False, 0, (), 50, 100, ())
            payload = {"recents": None, "cron": [{"id": "one"}], "messaging": []}

            profiles._sidebar_profile_cache_put(first_key, payload)
            cached = profiles._sidebar_profile_cache_get(first_key)
            cached["cron"][0]["id"] = "mutated"
            self.assertEqual(
                profiles._sidebar_profile_cache_get(first_key)["cron"][0]["id"],
                "one",
            )

            wal_path.write_bytes(b"wal-v2-is-different")
            second_fingerprint = profiles._sidebar_db_fingerprint(db_path)
            second_key = (str(db_path), second_fingerprint, False, 0, (), 50, 100, ())
            self.assertNotEqual(first_fingerprint, second_fingerprint)
            self.assertIsNone(profiles._sidebar_profile_cache_get(second_key))

            profiles._sidebar_profile_cache_put(second_key, payload)
            self.assertIsNone(profiles._sidebar_profile_cache_get(first_key))

    def test_profile_cache_is_lru_bounded(self):
        with mock.patch.object(profiles, "_SIDEBAR_PROFILE_CACHE_MAX_ENTRIES", 2):
            for index in range(3):
                key = (f"/db/{index}", (index, None), False, 0, (), 50, 100, ())
                profiles._sidebar_profile_cache_put(key, {"index": index})
            self.assertEqual(len(profiles._SIDEBAR_PROFILE_CACHE), 2)

    def test_applies_defaults_and_returns_defensive_copies(self):
        calls = 0

        @profiles._sidebar_singleflight_cache
        def scan(profile="all", limit=20):
            nonlocal calls
            calls += 1
            return {"profile": profile, "rows": [{"limit": limit}]}

        first = scan()
        first["rows"][0]["limit"] = 999
        second = scan(profile="all", limit=20)

        self.assertEqual(calls, 1)
        self.assertEqual(second, {"profile": "all", "rows": [{"limit": 20}]})

    def test_coalesces_concurrent_identical_scans(self):
        workers = 12
        release = threading.Event()
        watch = _LockWatch()

        def scan(profile="all"):
            watch.scan_started()
            self.assertTrue(release.wait(timeout=_HANG_GUARD))
            return {"profile": profile, "rows": []}

        with mock.patch.object(profiles, "threading", watch):
            scan = profiles._sidebar_singleflight_cache(scan)

        results = self._burst(watch, workers, lambda: scan("default"), release)

        self.assertEqual(results, [{"profile": "default", "rows": []}] * workers)

    def test_expires(self):
        clock = iter((100.0, 100.0, 100.0, 106.0, 106.0, 106.0))
        calls = 0

        @profiles._sidebar_singleflight_cache
        def scan():
            nonlocal calls
            calls += 1
            return {"generation": calls}

        with mock.patch.object(profiles.time, "monotonic", side_effect=clock):
            self.assertEqual(scan(), {"generation": 1})
            self.assertEqual(scan(), {"generation": 2})
        self.assertEqual(calls, 2)

    def test_does_not_cache_failures(self):
        calls = 0

        @profiles._sidebar_singleflight_cache
        def scan():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient")
            return {"ok": True}

        with self.assertRaisesRegex(RuntimeError, "transient"):
            scan()
        self.assertEqual(scan(), {"ok": True})
        self.assertEqual(scan(), {"ok": True})
        self.assertEqual(calls, 2)

    def test_does_not_cache_payloads_that_carry_profile_errors(self):
        # A 200 with a non-empty errors[] is how a failed profile scan is
        # reported. Caching it for the TTL keeps the empty recents page in
        # front of a store that has already recovered.
        calls = 0

        @profiles._sidebar_singleflight_cache
        def scan():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "errors": [{"profile": "default", "error": "disk I/O error"}],
                    "recents": {"sessions": []},
                }
            return {"errors": [], "recents": {"sessions": [{"id": "yesterday"}]}}

        first = scan()
        second = scan()

        self.assertEqual(first["errors"][0]["error"], "disk I/O error")
        self.assertEqual(second["recents"]["sessions"], [{"id": "yesterday"}])
        self.assertEqual(calls, 2)

    def test_can_be_disabled(self):
        calls = 0

        @profiles._sidebar_singleflight_cache
        def scan():
            nonlocal calls
            calls += 1
            return calls

        with mock.patch.object(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 0.0):
            self.assertEqual((scan(), scan()), (1, 2))

    def test_preserves_fastapi_signature(self):
        def scan(profile: str = "all", limit: int = 20):
            return profile, limit

        wrapped = profiles._sidebar_singleflight_cache(scan)

        self.assertEqual(inspect.signature(wrapped), inspect.signature(scan))

    def test_projects_tree_coalesces_concurrent_scans_and_returns_copies(self):
        # /api/profiles/projects/tree fans out over every profile's state.db; desktop
        # background sync + sidebar refreshes overlap identical requests. One scan must
        # serve the whole burst, and no two callers may share the same payload object.
        workers = 8
        release = threading.Event()
        watch = _LockWatch()

        def fake_read(name, home, errors, fn):
            watch.scan_started()
            self.assertTrue(release.wait(timeout=_HANG_GUARD))
            return None

        # The endpoint's own body under a single-flight wrapper whose locks are observable; the
        # registered route carries the same decorator (checked last).
        with mock.patch.object(profiles, "threading", watch):
            tree = profiles._sidebar_singleflight_cache(profiles.get_profiles_projects_tree.__wrapped__)
        with mock.patch.object(profiles, "_profile_targets", return_value=[("default", Path("/nonexistent"))]), \
                mock.patch.object(profiles, "_read_profile_db", side_effect=fake_read):
            results = self._burst(watch, workers, tree, release)

        self.assertEqual(len({id(r) for r in results}), workers)
        self.assertEqual(results, [results[0]] * workers)
        self.assertEqual(results[0]["projects"], [])

        # The registered endpoint is the single-flight one: a repeat inside the TTL scans nothing.
        reads = []
        with mock.patch.object(profiles, "_profile_targets", return_value=[("default", Path("/nonexistent"))]), \
                mock.patch.object(profiles, "_read_profile_db", side_effect=lambda *a: reads.append(a)):
            self.assertEqual(profiles.get_profiles_projects_tree(), profiles.get_profiles_projects_tree())
        self.assertEqual(len(reads), 1)


if __name__ == "__main__":
    unittest.main()
