"""model.save_key and /api/model/recommended-default read the model inventory cache-only.

Both run on a request worker (a tui-rpc pool thread / the dashboard's request pool). A blocking
inventory build probes every authed provider live, so one hanging provider held the worker (the
09-08 pool exhaustion). They now read cache-only and wait, bounded, only on the one provider the
user just authenticated, via that provider's own single-flight background refresh (review: Codex,
sync bucket B residual).
"""

from __future__ import annotations

import threading
import time

from hermes_cli import inventory
from hermes_cli import models as models_mod


def _payload(models):
    return {"providers": [{"slug": "openrouter", "models": list(models)},
                          {"slug": "other", "models": ["x"]}]}


def test_every_build_is_cache_only(monkeypatch):
    calls = []

    def _fake_build(ctx, **kwargs):
        calls.append(kwargs)
        return _payload(["m1"])

    monkeypatch.setattr(inventory, "build_models_payload", _fake_build)
    _payload_out, row = inventory.build_models_payload_for_provider(object(), "OpenRouter", picker_hints=True)

    assert row == {"slug": "openrouter", "models": ["m1"]}
    assert calls and all(c["non_blocking_catalogs"] is True and c["probe_custom_providers"] is False for c in calls)
    assert calls[0]["picker_hints"] is True
    assert len(calls) == 1, "a warm row must not wait or rebuild"


def test_cold_row_waits_for_its_own_refresh_then_rereads(monkeypatch):
    builds = []
    waited = []

    def _fake_build(ctx, **kwargs):
        builds.append(kwargs)
        return _payload([] if len(builds) == 1 else ["fresh"])

    monkeypatch.setattr(inventory, "build_models_payload", _fake_build)
    monkeypatch.setattr(models_mod, "wait_for_provider_catalog_refresh",
                        lambda slug, timeout_s: waited.append((slug, timeout_s)))

    _payload_out, row = inventory.build_models_payload_for_provider(object(), "openrouter")

    assert waited == [("openrouter", inventory._AUTHENTICATED_PROVIDER_CATALOG_WAIT_S)]
    assert row["models"] == ["fresh"]
    assert len(builds) == 2


def test_wait_returns_as_soon_as_the_refresh_finishes(monkeypatch):
    monkeypatch.setattr(models_mod, "_swr_refresh_inflight", {"openrouter"})
    threading.Timer(0.2, lambda: models_mod._swr_refresh_inflight.discard("openrouter")).start()

    started = time.monotonic()
    models_mod.wait_for_provider_catalog_refresh("openrouter", 5.0)
    assert time.monotonic() - started < 2.0


def test_wait_is_bounded_when_the_refresh_hangs(monkeypatch):
    monkeypatch.setattr(models_mod, "_swr_refresh_inflight", {"openrouter"})

    started = time.monotonic()
    models_mod.wait_for_provider_catalog_refresh("openrouter", 0.3)
    elapsed = time.monotonic() - started
    assert 0.25 <= elapsed < 2.0


def test_wait_never_fetches(monkeypatch):
    def _no_fetch(*_a, **_k):
        raise AssertionError("the wait must never probe a provider itself")

    monkeypatch.setattr(models_mod, "provider_model_ids", _no_fetch)
    monkeypatch.setattr(models_mod, "_swr_refresh_inflight", set())
    models_mod.wait_for_provider_catalog_refresh("openrouter", 1.0)


def test_hanging_provider_does_not_hold_the_inventory_read(monkeypatch):
    """End to end through the real inventory builder: a provider whose live /v1/models never
    returns must not block the cache-only read (it holds only its own daemon refresh thread)."""
    release = threading.Event()

    def _hang(*_a, **_k):
        release.wait(30)
        return []

    monkeypatch.setattr(models_mod, "provider_model_ids", _hang)
    monkeypatch.setattr(models_mod, "_load_provider_models_cache", lambda: {})
    monkeypatch.setattr(inventory, "_AUTHENTICATED_PROVIDER_CATALOG_WAIT_S", 0.5)
    try:
        started = time.monotonic()
        inventory.build_models_payload_for_provider(inventory.load_picker_context(), "openrouter")
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 10.0, f"inventory read blocked {elapsed:.1f}s on a hanging provider"


def _record_build(monkeypatch):
    calls = []

    def _fake_build(ctx, **kwargs):
        calls.append(kwargs)
        return {"providers": []}

    monkeypatch.setattr(inventory, "build_models_payload", _fake_build)
    return calls


def test_kanban_model_options_reads_catalogs_cache_only(monkeypatch):
    """Review (Codex, bucket D caller audit): the kanban drawer's request worker must not wait on a
    live provider probe."""
    from plugins.kanban.dashboard import plugin_api

    calls = _record_build(monkeypatch)
    plugin_api.model_options()
    assert calls and calls[0].get("non_blocking_catalogs") is True


def test_acp_model_state_reads_catalogs_cache_only(monkeypatch):
    """ACP builds the picker synchronously inside async session handlers: a live probe would block
    its event loop."""
    from acp_adapter import model_catalog

    calls = _record_build(monkeypatch)
    monkeypatch.setattr(model_catalog, "_named_custom_provider_catalogs", lambda: [])
    model_catalog.build_model_state("m", "openrouter", "")
    assert calls and calls[0].get("non_blocking_catalogs") is True
