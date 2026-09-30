"""A43: every settings cache keys on file CONTENT, not just metadata.

On Windows a same-size in-place edit inside the mtime tick (or with the mtime restored) keeps
``(mtime_ns, size, ino, ctime_ns)``: ino and ctime (creation time there) are stable across an
in-place rewrite. Live example: ``model.context_length: 32768 -> 65536`` (same byte length) edited
while Hermes ran stayed stale. The tests force the equal-metadata case with ``os.utime`` and assert
through the real loaders against a temp HERMES_HOME; on POSIX (where ctime moves) they still hold.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import config as config_mod
from utils import file_signature


def _rewrite_in_place_keeping_metadata(path: Path, data: bytes) -> None:
    """Same-size in-place rewrite with the original mtime restored (what a Windows editor inside one
    timer tick looks like to stat)."""
    before = path.stat()
    assert len(data) == before.st_size, "the scenario needs a same-size edit"
    with path.open("r+b") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.read_bytes() == data


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    yield home
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()


def test_every_config_loader_sees_a_same_size_same_mtime_edit(home):
    from hermes_cli.config_effective import _EFFECTIVE_CACHE, load_user_config_effective

    _EFFECTIVE_CACHE.clear()
    cfg = home / "config.yaml"
    cfg.write_bytes(b"model:\n  context_length: 32768\n")
    loaders = {
        "load_config": lambda: config_mod.load_config()["model"]["context_length"],
        "load_config_readonly": lambda: config_mod.load_config_readonly()["model"]["context_length"],
        "read_raw_config": lambda: config_mod.read_raw_config()["model"]["context_length"],
        "read_raw_config_readonly": lambda: config_mod.read_raw_config_readonly()["model"]["context_length"],
        "load_user_config_effective": lambda: load_user_config_effective()["model"]["context_length"],
    }
    assert {name: read() for name, read in loaders.items()} == dict.fromkeys(loaders, 32768)
    metadata = file_signature(cfg.stat())

    _rewrite_in_place_keeping_metadata(cfg, b"model:\n  context_length: 65536\n")

    stale = {name: value for name, read in loaders.items() if (value := read()) != 65536}
    assert not stale, f"served stale after a same-size edit (metadata unchanged: {file_signature(cfg.stat()) == metadata})"


def test_managed_config_same_size_edit_is_seen(home, tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    (home / "config.yaml").write_text("agent:\n  max_turns: 7\n", encoding="utf-8")
    policy = managed / "config.yaml"
    policy.write_bytes(b"approvals:\n  deny: []  \n")
    assert config_mod.load_config_readonly()["approvals"]["deny"] == []

    _rewrite_in_place_keeping_metadata(policy, b"approvals:\n  deny: [rm]\n")

    assert managed_scope.load_managed_config()["approvals"]["deny"] == ["rm"]
    assert config_mod.load_config_readonly()["approvals"]["deny"] == ["rm"]


def test_side_caches_of_config_and_env_see_a_same_size_edit(home):
    from agent import secret_scope, skill_utils
    from gateway.run_profile_reconcile import profile_serve_signature

    skill_utils._raw_config_cache_clear()
    secret_scope.invalidate_env_file_cache()
    cfg, env = home / "config.yaml", home / ".env"
    cfg.write_bytes(b"skills:\n  create_dir: aaaa\n")
    env.write_bytes(b"OPENROUTER_API_KEY=sk-or-FAKE-1111\n")
    assert skill_utils._skills_cfg_get("create_dir") == "aaaa"
    assert secret_scope.load_env_file(env)["OPENROUTER_API_KEY"] == "sk-or-FAKE-1111"
    served = profile_serve_signature(home)

    _rewrite_in_place_keeping_metadata(cfg, b"skills:\n  create_dir: bbbb\n")
    _rewrite_in_place_keeping_metadata(env, b"OPENROUTER_API_KEY=sk-or-FAKE-2222\n")  # same-length rotation

    assert skill_utils._skills_cfg_get("create_dir") == "bbbb"
    assert secret_scope.load_env_file(env)["OPENROUTER_API_KEY"] == "sk-or-FAKE-2222"
    assert profile_serve_signature(home) != served, "gateway rescan must see the edited profile"


def test_same_size_fix_of_a_broken_config_ends_the_failure(home):
    cfg = home / "config.yaml"
    good_a, broken, good_b = b"agent:\n  max_turns: 11\n", b"agent:\n  max_turns: [1\n", b"agent:\n  max_turns: 22\n"
    assert len(good_a) == len(broken) == len(good_b)
    cfg.write_bytes(good_a)
    assert config_mod.load_config_readonly()["agent"]["max_turns"] == 11

    _rewrite_in_place_keeping_metadata(cfg, broken)
    assert config_mod.load_config_readonly()["agent"]["max_turns"] == 11  # last-known-good
    assert config_mod.get_active_config_parse_failure()

    _rewrite_in_place_keeping_metadata(cfg, good_b)
    assert config_mod.load_config_readonly()["agent"]["max_turns"] == 22
    assert config_mod.get_active_config_parse_failure() is None


def test_unchanged_file_stays_a_cache_hit_without_reparsing(home, monkeypatch):
    cfg = home / "config.yaml"
    cfg.write_text("".join(f"section_{i}:\n  key: value_{i}\n" for i in range(200)), encoding="utf-8")
    first = config_mod.load_config_readonly()
    raw_first = config_mod.read_raw_config_readonly()
    parses = []
    real = config_mod.fast_safe_load
    monkeypatch.setattr(config_mod, "fast_safe_load", lambda text: parses.append(1) or real(text))

    started = time.perf_counter()
    for _ in range(200):
        assert config_mod.load_config_readonly() is first  # identity invariant: the cached object
        assert config_mod.read_raw_config_readonly() is raw_first
    elapsed = time.perf_counter() - started

    assert parses == [], "an unchanged file must not be re-parsed"
    assert elapsed < 2.0, f"400 cached reads took {elapsed:.2f}s"
