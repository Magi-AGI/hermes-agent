"""A ``time`` module stand-in whose named clocks the test advances; everything else is the real one.

Patch it over the module under test (``monkeypatch.setattr(mod, "time", DrivenClock("monotonic"))``)
instead of sleeping for an ordering or an elapsed-time assertion. On Windows ``time.monotonic()``
ticks every 15.625 ms, and ``time.time()`` / file mtimes do too unless some process has raised the
system timer, so a sleep shorter than a tick often measured no elapsed time at all.
"""

from __future__ import annotations

import time


class DrivenClock:
    def __init__(self, *driven: str):
        self._driven = frozenset(driven)
        self.now = time.time()

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __getattr__(self, name):
        if name in self._driven:
            return lambda: self.now
        return getattr(time, name)
