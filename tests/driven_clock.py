"""A ``time`` module stand-in whose named clocks the test advances; everything else is the real one.

Patch it over the module under test (``monkeypatch.setattr(mod, "time", DrivenClock("monotonic"))``)
instead of sleeping for an ordering or an elapsed-time assertion. On Windows ``time.monotonic()``
ticks every 15.625 ms, and ``time.time()`` / file mtimes do too unless some process has raised the
system timer, so a sleep shorter than a tick often measured no elapsed time at all.

Each driven clock starts at its real counterpart's current value and moves only by ``advance()``, so
a timestamp the code under test caches during the test stays on that clock's scale afterwards.
"""

from __future__ import annotations

import time


class DrivenClock:
    def __init__(self, *driven: str):
        self._origins = {name: getattr(time, name)() for name in driven}
        self._elapsed = 0.0

    def advance(self, seconds: float) -> None:
        self._elapsed += seconds

    def __getattr__(self, name):
        origins = self.__dict__.get("_origins", {})
        if name in origins:
            return lambda: origins[name] + self._elapsed
        return getattr(time, name)
