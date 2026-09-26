"""Harness-only egress guard for the throwaway backend (never imported by Hermes itself).

The concurrency harness puts this directory first on the backend's PYTHONPATH, so Python imports it at
startup. Every socket connect to a non-loopback address is refused and recorded in
``$HERMES_HARNESS_EGRESS_LOG``: a real backend runs background fetches (model catalog, models.dev,
GitHub compare) that the harness must neither make nor silently lose track of.
"""

import os
import socket
import threading

_LOG = os.environ.get("HERMES_HARNESS_EGRESS_LOG", "")
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
