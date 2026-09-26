"""Harness-only egress guard for the throwaway backend (never imported by Hermes itself).

The concurrency harness puts this directory first on the backend's PYTHONPATH, so Python imports it at
startup (and so does every Python child the backend spawns, which inherits the PYTHONPATH). In those
processes:
- name resolution (getaddrinfo, gethostbyname[_ex]) refuses every name that is not a loopback address
  or exactly ``localhost``, BEFORE the OS resolver runs, so no external DNS query is ever sent;
- TCP connect/connect_ex and UDP sendto refuse every address that is not a loopback IP;
- each refusal is appended to ``$HARNESS_EGRESS_LOG``: a real backend runs background fetches (model
  catalog, models.dev, GitHub compare) that the harness must neither make nor silently lose track of.
Not covered: non-Python child processes (git, node, ...) resolve and connect on their own; the
harness's socket watcher samples those as a diagnostic, which is not proof of zero egress.
"""

import ipaddress
import os
import socket
import threading

_LOG = os.environ.get("HARNESS_EGRESS_LOG", "")
_LOCK = threading.Lock()
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_getaddrinfo = socket.getaddrinfo
_real_gethostbyname = socket.gethostbyname
_real_gethostbyname_ex = socket.gethostbyname_ex


def _is_loopback(host) -> bool:
    """A loopback IP (127.0.0.0/8, ::1) or exactly ``localhost``; nothing matched by prefix."""
    if host is None:
        return True  # getaddrinfo(None, port): the wildcard/local address, no resolution
    name = str(host).strip("[]")
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _record(what: str, target) -> None:
    if _LOG:
        with _LOCK, open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {what} {target!r}\n")


def _host_of(address):
    return address[0] if isinstance(address, tuple) and address else address


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(_host_of(address)):
        _record("connect", address)
        raise ConnectionRefusedError(f"harness egress guard: refused connect {address!r}")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(_host_of(address)):
        _record("connect", address)
        raise ConnectionRefusedError(f"harness egress guard: refused connect {address!r}")
    return _real_connect_ex(self, address)


def _guarded_sendto(self, data, *args):
    address = args[-1] if args else None
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(_host_of(address)):
        _record("sendto", address)
        raise ConnectionRefusedError(f"harness egress guard: refused sendto {address!r}")
    return _real_sendto(self, data, *args)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _is_loopback(host):
        _record("resolve", host)
        raise socket.gaierror(socket.EAI_NONAME, f"harness egress guard: refused resolving {host!r}")
    return _real_getaddrinfo(host, *args, **kwargs)


def _guarded_gethostbyname(host):
    if not _is_loopback(host):
        _record("resolve", host)
        raise socket.gaierror(socket.EAI_NONAME, f"harness egress guard: refused resolving {host!r}")
    return _real_gethostbyname(host)


def _guarded_gethostbyname_ex(host):
    if not _is_loopback(host):
        _record("resolve", host)
        raise socket.gaierror(socket.EAI_NONAME, f"harness egress guard: refused resolving {host!r}")
    return _real_gethostbyname_ex(host)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.socket.sendto = _guarded_sendto
socket.getaddrinfo = _guarded_getaddrinfo
socket.gethostbyname = _guarded_gethostbyname
socket.gethostbyname_ex = _guarded_gethostbyname_ex


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
