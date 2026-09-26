"""tests/driven_clock.py: a driven clock stays on its real clock's scale, so a timestamp the code under
test caches while it is patched in is still meaningful after the patch ends (Codex T5 r2: a fake
monotonic() seeded from time.time() left a cache entry ~1.8e9 s in the future)."""

from __future__ import annotations

import time

from tests.driven_clock import DrivenClock


def test_driven_clocks_start_on_their_real_scale_and_move_only_when_advanced():
    before = {"monotonic": time.monotonic(), "time": time.time()}
    clock = DrivenClock("monotonic", "time")
    after = {"monotonic": time.monotonic(), "time": time.time()}
    first = {name: getattr(clock, name)() for name in before}
    clock.advance(5.0)
    for name in before:
        assert before[name] <= first[name] <= after[name], name
        assert getattr(clock, name)() == first[name] + 5.0, name
    assert clock.perf_counter is time.perf_counter  # undriven: the real function
