"""Refuse the real Codex auth host for the whole test session, in-process AND in child Pythons.

Installed by ``tests/conftest.py`` at import time (before collection, module/session fixtures and
test-module imports) and, through ``sitecustomize.py`` in this directory (prepended to the
children's ``PYTHONPATH``), in every Python interpreter a test spawns with the inherited
environment. Two layers, both before any connection exists:

* name resolution (``socket.getaddrinfo``): urllib, requests, raw sockets, httpx;
* the httpx sync/async transports (a request routed through a proxy never resolves the host
  locally). In a child, httpx is patched lazily when it is first imported (no startup cost).

Every attempt is recorded (``ATTEMPTS``) and raises ``ConnectionRefusedError``. Standalone on
purpose: no Hermes imports, so it can run from ``sitecustomize`` before anything else.
"""

from __future__ import annotations

import os
import socket
import sys

ENV_HOSTS = "HERMES_TEST_BLOCKED_AUTH_HOSTS"
DEFAULT_HOSTS = ("auth.openai.com",)
ATTEMPTS: list = []
_HOSTS: set = set()


def _refuse(host) -> None:
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host or "")
    if name.lower().rstrip(".") in _HOSTS:
        ATTEMPTS.append(name)
        raise ConnectionRefusedError(f"test session: the real Codex auth host {name} is blocked")


def _patch_httpx(httpx) -> None:
    transport, async_transport = httpx.HTTPTransport, httpx.AsyncHTTPTransport
    if getattr(transport, "_hermes_net_guard", False):
        return
    sync_handle, async_handle = transport.handle_request, async_transport.handle_async_request

    def handle_request(self, request):
        _refuse(request.url.host)
        return sync_handle(self, request)

    async def handle_async_request(self, request):
        _refuse(request.url.host)
        return await async_handle(self, request)

    transport.handle_request = handle_request
    async_transport.handle_async_request = handle_async_request
    transport._hermes_net_guard = True


class _PatchHttpxOnImport:
    """Meta-path hook: patch httpx right after it is first imported (children pay nothing)."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "httpx":
            return None
        from importlib.machinery import PathFinder
        spec = PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        exec_module = spec.loader.exec_module

        def exec_and_patch(module):
            exec_module(module)
            _patch_httpx(module)

        spec.loader.exec_module = exec_and_patch
        return spec


def install(hosts=None) -> None:
    """Idempotent. *hosts* defaults to ``$HERMES_TEST_BLOCKED_AUTH_HOSTS`` / ``DEFAULT_HOSTS``."""
    if hosts is None:
        hosts = [h for h in os.environ.get(ENV_HOSTS, "").split(",") if h] or DEFAULT_HOSTS
    _HOSTS.update(h.lower().rstrip(".") for h in hosts if h)
    if not getattr(socket.getaddrinfo, "_hermes_net_guard", False):
        real_getaddrinfo = socket.getaddrinfo

        def getaddrinfo(host, *args, **kwargs):
            _refuse(host)
            return real_getaddrinfo(host, *args, **kwargs)

        getaddrinfo._hermes_net_guard = True
        socket.getaddrinfo = getaddrinfo
    if "httpx" in sys.modules:
        _patch_httpx(sys.modules["httpx"])
    elif not any(isinstance(f, _PatchHttpxOnImport) for f in sys.meta_path):
        sys.meta_path.insert(0, _PatchHttpxOnImport())
