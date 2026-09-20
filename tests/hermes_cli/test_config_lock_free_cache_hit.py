"""A cached config read must not block behind a config WRITER.

``_load_config_impl`` served cache hits from inside ``_CONFIG_LOCK``, which
``save_config()`` holds across an atomic YAML write. A cache hit itself costs
~0.024us-scale work, but measured on a clean tree the same cached read took
**10010ms** while another thread held the lock.

On the desktop backend the same read sits under every ``model.options`` build
(per model, via ``agent.models_dev``), so one config write convoyed the whole
RPC pool (2026-09-13). Upstream's hook-timeout memo from the same commit is not
carried (it depends on later upstream-main plugins commits).

These tests drive the real functions against a temp HERMES_HOME -- no mocks of
the thing under test, no source reading.
"""

from __future__ import annotations

import os
import threading
import time

import pytest


# Generous: the point is 10s-vs-instant, not a tight timing assertion.
MAX_BLOCKED_READ_SECS = 2.0


@pytest.fixture()
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    lines = ["plugins:", "  hook_callback_timeout: 30", "agent:", "  max_turns: 500"]
    for i in range(100):
        lines += [f"section_{i}:", f"  key_a: value_{i}"]
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import config as cfgmod

    cfgmod._LOAD_CONFIG_CACHE.clear()
    yield home
    cfgmod._LOAD_CONFIG_CACHE.clear()


def _bump_mtime(path):
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_cached_read_completes_while_another_thread_holds_the_config_lock(config_home):
    from hermes_cli import config as cfgmod

    cfgmod.load_config_readonly()  # prime the cache

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with cfgmod._CONFIG_LOCK:
            holding.set()
            release.wait(timeout=30)

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    try:
        assert holding.wait(timeout=10), "lock holder never started"
        started = time.perf_counter()
        cfg = cfgmod.load_config_readonly()
        elapsed = time.perf_counter() - started
    finally:
        release.set()
        thread.join(timeout=10)

    assert isinstance(cfg, dict) and cfg
    assert elapsed < MAX_BLOCKED_READ_SECS, (
        f"a cached load_config_readonly() blocked {elapsed:.1f}s behind a held "
        "_CONFIG_LOCK; cache hits must not serialize against config writers"
    )


def test_deepcopy_read_also_completes_under_a_held_lock(config_home):
    from hermes_cli import config as cfgmod

    cfgmod.load_config()

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with cfgmod._CONFIG_LOCK:
            holding.set()
            release.wait(timeout=30)

    thread = threading.Thread(target=_hold, daemon=True)
    thread.start()
    try:
        assert holding.wait(timeout=10)
        started = time.perf_counter()
        cfg = cfgmod.load_config()
        elapsed = time.perf_counter() - started
    finally:
        release.set()
        thread.join(timeout=10)

    assert cfg["agent"]["max_turns"] == 500
    assert elapsed < MAX_BLOCKED_READ_SECS


def test_fast_path_still_sees_a_changed_config_file(config_home):
    """Freshness is not sacrificed for the lock-free read."""
    from hermes_cli import config as cfgmod

    assert cfgmod.load_config_readonly()["agent"]["max_turns"] == 500

    path = config_home / "config.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("max_turns: 500", "max_turns: 123"),
        encoding="utf-8",
    )
    _bump_mtime(path)

    assert cfgmod.load_config_readonly()["agent"]["max_turns"] == 123


def test_load_config_still_returns_an_isolated_object(config_home):
    """The deepcopy contract must survive the fast path."""
    from hermes_cli import config as cfgmod

    first = cfgmod.load_config()
    first["agent"]["max_turns"] = -1
    assert cfgmod.load_config()["agent"]["max_turns"] == 500
    assert cfgmod.load_config_readonly()["agent"]["max_turns"] == 500


def test_concurrent_readers_and_a_writer_never_see_a_torn_config(config_home):
    """The fast path reads a tuple that is replaced wholesale, never mutated."""
    from hermes_cli import config as cfgmod

    cfgmod.load_config_readonly()
    stop = threading.Event()
    errors: list[BaseException] = []
    seen: list[int] = []

    def _writer():
        try:
            while not stop.is_set():
                cfg = cfgmod.load_config()
                cfg.setdefault("agent", {})["max_turns"] = 500
                cfgmod.save_config(cfg)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _reader():
        try:
            for _ in range(300):
                cfg = cfgmod.load_config_readonly()
                # Every observation must be a complete, well-formed config.
                assert isinstance(cfg, dict)
                assert isinstance(cfg.get("agent"), dict)
                seen.append(int(cfg["agent"]["max_turns"]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    writer = threading.Thread(target=_writer, daemon=True)
    readers = [threading.Thread(target=_reader, daemon=True) for _ in range(4)]
    writer.start()
    for r in readers:
        r.start()
    for r in readers:
        r.join(timeout=60)
    stop.set()
    writer.join(timeout=30)

    assert not errors, f"torn/failed read under concurrency: {errors[0]!r}"
    assert seen, "readers observed nothing"
