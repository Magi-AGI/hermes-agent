"""Harness-only egress guard for the throwaway backend (never imported by Hermes itself).

The concurrency harness puts this directory first on the backend's PYTHONPATH, so Python imports it at
startup. Every socket connect to a non-loopback address is refused and recorded in
``$HARNESS_EGRESS_LOG``: a real backend runs background fetches (model catalog, models.dev,
GitHub compare) that the harness must neither make nor silently lose track of.
"""

import os
import socket
import threading

_LOG = os.environ.get("HARNESS_EGRESS_LOG", "")
_LOCK = threading.Lock()
_LOOPBACK = ("127.", "::1", "localhost")
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _target(address) -> str:
    return str(address[0]) if isinstance(address, tuple) and address else str(address)


def _refuse(address) -> None:
    if _LOG:
        with _LOCK, open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {address!r}\n")
    raise ConnectionRefusedError(f"harness egress guard: refused {address!r}")


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _target(address).startswith(_LOOPBACK):
        _refuse(address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _target(address).startswith(_LOOPBACK):
        _refuse(address)
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex


# D6 measurement only (``HARNESS_ALLOW_FIXTURE_COMPACTION=1``, set by the harness for its throwaway
# backend): the subscription-only compaction guard rightly refuses the loopback fixture model, which
# left compaction unmeasurable. Here, and only here, the route screen admits the fixture so the REAL
# dispatch -> llm lane -> compaction path runs; production code and every real backend keep the guard.
if os.environ.get("HARNESS_ALLOW_FIXTURE_COMPACTION") == "1":
    import importlib.abc
    import sys

    class _AdmitFixtureCompaction(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname != "agent.auxiliary_client":
                return None
            sys.meta_path.remove(self)
            try:
                import importlib.util
                spec = importlib.util.find_spec(fullname)
            finally:
                sys.meta_path.insert(0, self)
            real_exec = spec.loader.exec_module

            def exec_module(module):
                real_exec(module)
                module._compression_client_allowed = lambda label, client: True
            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, _AdmitFixtureCompaction())
