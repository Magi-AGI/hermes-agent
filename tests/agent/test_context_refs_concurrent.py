"""Tests for concurrent @-reference expansion in context_references.

test_refs_expand_concurrently asserts that N URL refs are fetched CONCURRENTLY.
It proves this with an asyncio.Barrier rendezvous rather than a stopwatch: all
N fetches must be in flight at the same instant before any is allowed to
return. On the serial `for ref in refs: await` loop the first fetch waits for
partners that never arrive and the test fails; with asyncio.gather it passes.
The output-contract test guards that concurrency does NOT change ordering,
warnings, blocks, or token accounting.
"""
from __future__ import annotations

import asyncio

import pytest

from agent.context_references import preprocess_context_references_async


async def _slow_fetcher(url: str) -> str:
    # Simulate a per-URL network fetch (web_extract round trip).
    await asyncio.sleep(0.2)
    return f"CONTENT[{url}]"


@pytest.mark.asyncio
async def test_refs_expand_concurrently(tmp_path):
    # Three independent URL refs in one message.
    msg = "see @url:https://a.example/x @url:https://b.example/y @url:https://c.example/z please"

    # Concurrency is proven by construction, not by measuring elapsed time.
    #
    # The old form asserted `elapsed < 0.4` ("well under 2x one 0.2s fetch").
    # That makes the event-loop scheduler and any fixed setup part of the
    # assertion: under a loaded CI box the inequality can flip with nothing
    # wrong in the code under test, and the margin shrinks silently if setup
    # cost is ever added ahead of dispatch.
    #
    # A barrier asserts the invariant directly: all THREE fetches must be
    # inside the fetcher AT THE SAME TIME before any is allowed to return. If
    # expansion ever goes serial the first fetch blocks waiting for partners
    # that will not arrive, the barrier times out, and the test fails with an
    # explicit message. No wall-clock constant, no load sensitivity.
    N_REFS = 3
    rendezvous = asyncio.Barrier(N_REFS)
    entered: list[str] = []
    overlapped = asyncio.Event()

    async def barrier_fetcher(url: str) -> str:
        entered.append(url)
        # Generous relative to real scheduling latency (a rendezvous between
        # already-dispatched coroutines needs milliseconds), but finite so a
        # serial regression fails fast instead of hanging the suite.
        async with asyncio.timeout(10):
            await rendezvous.wait()
        overlapped.set()
        return f"CONTENT[{url}]"

    try:
        res = await preprocess_context_references_async(
            msg, cwd=tmp_path, context_length=100_000, url_fetcher=barrier_fetcher,
        )
    except (asyncio.BrokenBarrierError, TimeoutError):  # pragma: no cover - serial regression
        pytest.fail(
            "references did not expand concurrently: a fetch reached the "
            f"rendezvous alone, so expansion never had {N_REFS} fetches in "
            f"flight at once (entered: {entered})"
        )

    # The barrier only clears when all three fetches are in flight together.
    assert overlapped.is_set(), f"references never overlapped (entered: {entered})"
    assert len(entered) == N_REFS, f"expected {N_REFS} fetches, got {entered}"
    # All three blocks present, in order.
    assert res.expanded
    body = res.message
    assert body.index("a.example") < body.index("b.example") < body.index("c.example"), \
        "reference blocks must stay in original order"


@pytest.mark.asyncio
async def test_concurrent_preserves_output_contract(tmp_path):
    """Concurrency must not change which blocks/warnings appear or their order."""
    msg = "@url:https://one.example/p @url:https://two.example/q"
    res = await preprocess_context_references_async(
        msg, cwd=tmp_path, context_length=100_000, url_fetcher=_slow_fetcher,
    )
    assert "CONTENT[https://one.example/p]" in res.message
    assert "CONTENT[https://two.example/q]" in res.message
    assert res.message.index("one.example") < res.message.index("two.example")
    assert res.injected_tokens > 0


def test_hung_url_reference_fails_alone_at_the_expand_deadline(tmp_path):
    """A fetch that never answers costs its own reference (a warning), not the prompt: the rest
    still expands and the caller is released at ``timeouts.context_references.expand`` — even
    when the stalled fetch holds a ``to_thread`` worker, as web extraction does (futures audit,
    G10: the sync wrapper used to wait on the whole expansion with no bound at all)."""
    import threading
    import time

    from agent.context_references import preprocess_context_references
    from hermes_constants import get_hermes_home

    (get_hermes_home() / "config.yaml").write_text(
        "timeouts:\n  context_references:\n    expand: 0.5\n", encoding="utf-8")
    release = threading.Event()

    async def fetcher(url: str) -> str:
        if "stalled" in url:
            await asyncio.to_thread(release.wait, 6.0)
        return f"CONTENT[{url}]"

    start = time.monotonic()
    try:
        res = preprocess_context_references(
            "see @url:https://stalled.example/x and @url:https://ok.example/y",
            cwd=tmp_path, context_length=100_000, url_fetcher=fetcher)
    finally:
        release.set()
    assert time.monotonic() - start < 4.0
    assert not res.blocked
    assert "CONTENT[https://ok.example/y]" in res.message
    assert any("stalled.example" in w and "timed out" in w for w in res.warnings)


_EXIT_PROBE = """
import asyncio, os, sys, threading, time
sys.path.insert(0, sys.argv[1])
home = sys.argv[2]
os.environ["HERMES_HOME"] = home
from agent.context_references import preprocess_context_references
stuck = threading.Event()  # never set: the abandoned worker would wait forever

async def fetch(url):
    await asyncio.to_thread(stuck.wait)
    return "never"

result = preprocess_context_references("see @url:https://stuck.example/x", cwd=home, context_length=100000,
                                       url_fetcher=fetch)
print("returned", result.warnings, flush=True)
"""


def test_an_abandoned_fetch_worker_does_not_hold_process_exit(tmp_path):
    """Round 1 (Codex): the caller came back at the deadline, but a stock executor's worker is joined
    at interpreter exit, so a one-shot process hung on a fetch that never answers."""
    import subprocess
    import sys
    import time
    from pathlib import Path

    (tmp_path / "config.yaml").write_text(
        "timeouts:\n  context_references:\n    expand: 0.3\n", encoding="utf-8")
    script = tmp_path / "exit_probe.py"
    script.write_text(_EXIT_PROBE, encoding="utf-8")
    repo = str(Path(__file__).resolve().parents[2])
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(script), repo, str(tmp_path)], capture_output=True, text=True,
                          timeout=90)
    assert "returned" in proc.stdout, proc.stderr[-2000:]
    assert time.monotonic() - started < 60  # the probe itself imports the agent stack
