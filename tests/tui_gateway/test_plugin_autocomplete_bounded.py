"""Plugin ``@prefix:`` autocomplete is bounded on the RPC worker (futures audit, G10/G11).

The provider coroutine ran through ``with ThreadPoolExecutor()`` + an unbounded ``result()``: a
provider that never answered pinned an RPC pool worker for good, one per keystroke.
"""

import threading
import time

import pytest

from agent.context_references import (
    ContextCompletionItem, ContextReferenceProvider, _context_reference_providers,
    register_context_reference_provider,
)


class _StalledProvider(ContextReferenceProvider):
    prefix = "stall"
    description = "never answers"

    def __init__(self, release: threading.Event):
        self.release = release
        self.calls = 0

    async def autocomplete(self, query: str, *, limit: int = 10) -> list[ContextCompletionItem]:
        self.calls += 1
        self.release.wait(4.0)
        return [ContextCompletionItem(text="late")]

    async def expand(self, target: str) -> str | None:
        return None


@pytest.fixture(autouse=True)
def _clean_registry():
    _context_reference_providers.clear()
    yield
    _context_reference_providers.clear()


def test_stalled_provider_releases_worker_and_is_not_restarted_per_keystroke(monkeypatch):
    from tui_gateway import methods_complete

    monkeypatch.setattr(methods_complete, "_PLUGIN_AUTOCOMPLETE_TIMEOUT_S", 0.3, raising=False)
    release = threading.Event()
    provider = _StalledProvider(release)
    register_context_reference_provider(provider)
    start = time.monotonic()
    try:
        assert methods_complete._plugin_reference_items("stall", "a") is None
        assert methods_complete._plugin_reference_items("stall", "ab") is None
    finally:
        release.set()
    assert time.monotonic() - start < 2.5
    assert provider.calls == 1
