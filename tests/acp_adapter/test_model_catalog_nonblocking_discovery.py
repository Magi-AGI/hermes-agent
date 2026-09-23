"""ACP named custom-provider discovery never blocks the caller (review: Codex, sync bucket D).

ACP builds its model picker synchronously inside async session handlers. The per-attempt probe
timeout is not an overall deadline: two cold endpoints meant several sequential probes on the
event loop. Discovery now serves the last result (declared models when cold) and refreshes each
endpoint in the background, one refresh at a time, in the caller's profile context.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from acp_adapter import model_catalog


@pytest.fixture(autouse=True)
def _fresh_discovery_state(monkeypatch):
    monkeypatch.setattr(model_catalog, "_discovered", {})
    monkeypatch.setattr(model_catalog, "_discovery_inflight", set())


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_cold_endpoints_return_declared_models_without_waiting(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "k")
    cfg = {"providers": {
        f"relay{i}": {"name": f"Relay {i}", "base_url": f"https://relay{i}.example/v1",
                      "key_env": "SOME_KEY", "default_model": f"declared-{i}"}
        for i in range(2)}}
    release = threading.Event()

    def _hang(*_a, **_k):
        release.wait(30)
        return ["live-model"]

    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.model_switch_providers._fetch_picker_live_models", side_effect=_hang):
        try:
            started = time.monotonic()
            catalogs = model_catalog._named_custom_provider_catalogs()
            elapsed = time.monotonic() - started
        finally:
            release.set()
    assert elapsed < 2.0, f"discovery blocked the caller for {elapsed:.1f}s"
    assert sorted(m for _slug, _label, models in catalogs for m, _ in models) == ["declared-0", "declared-1"]


def test_a_finished_refresh_is_served_on_the_next_build(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "k")
    cfg = {"providers": {"relay": {"name": "Relay", "base_url": "https://relay.example/v1",
                                   "key_env": "SOME_KEY", "default_model": "model-a"}}}
    with patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.model_switch_providers._fetch_picker_live_models", return_value=["model-a", "model-b"]):
        first = model_catalog._named_custom_provider_catalogs()
        assert _wait_until(lambda: not model_catalog._discovery_inflight)
        second = model_catalog._named_custom_provider_catalogs()
    assert [m for m, _ in first[0][2]] == ["model-a"]
    assert [m for m, _ in second[0][2]] == ["model-a", "model-b"]


def test_one_refresh_per_endpoint_at_a_time():
    release = threading.Event()
    calls = []

    def _fetch():
        calls.append(1)
        release.wait(10)
        return ["m"]

    key = ("home", "relay", "https://relay.example/v1", "")
    try:
        for _ in range(5):
            assert model_catalog._discovered_models_nonblocking(key, _fetch) is None
        assert _wait_until(lambda: calls == [1])
        time.sleep(0.1)
        assert calls == [1]
    finally:
        release.set()
    assert _wait_until(lambda: not model_catalog._discovery_inflight)
    assert model_catalog._discovered_models_nonblocking(key, _fetch) == ["m"]


def test_refresh_runs_in_the_callers_profile_context(tmp_path):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    seen = []
    home_b = tmp_path / "profile-b"
    home_b.mkdir()
    token = set_hermes_home_override(home_b)
    try:
        model_catalog._discovered_models_nonblocking(("k",), lambda: seen.append(get_hermes_home()) or [])
    finally:
        reset_hermes_home_override(token)
    assert _wait_until(lambda: bool(seen))
    assert seen[0] == home_b
