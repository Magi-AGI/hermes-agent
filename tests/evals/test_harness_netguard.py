"""The concurrency harness's egress guard refuses external names before resolving them and external
addresses before connecting, and matches loopback exactly (T1 round 3, Codex: prefix matching let
``localhost.review.invalid`` through, and resolution was never guarded). Recording fakes stand in for
the real resolver and connect, so nothing here touches the network; the guard runs in a subprocess
because it patches the socket module for the whole interpreter.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parents[2] / "evals" / "shared_backend_concurrency" / "netguard" / "sitecustomize.py"

_PROBE = r"""
import importlib.util, json, socket, sys
forwarded = {"resolve": [], "connect": []}
socket.getaddrinfo = lambda host, *a, **k: forwarded["resolve"].append(host) or []
socket.gethostbyname = lambda host: forwarded["resolve"].append(host) or "127.0.0.1"
socket.socket.connect = lambda self, address: forwarded["connect"].append(address[0])
spec = importlib.util.spec_from_file_location("netguard_under_test", sys.argv[1])
spec.loader.exec_module(importlib.util.module_from_spec(spec))
refused = []
for host in ("localhost", "127.0.0.1", "::1", "models.example.invalid", "localhost.review.invalid", "127.evil.invalid"):
    try:
        socket.getaddrinfo(host, 443)
    except socket.gaierror:
        refused.append(["resolve", host])
for address in (("127.0.0.1", 80), ("::1", 80), ("93.184.216.34", 443), ("localhost.review.invalid", 443)):
    family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.connect(address)
    except ConnectionRefusedError:
        refused.append(["connect", address[0]])
    finally:
        s.close()
print(json.dumps({"forwarded": forwarded, "refused": refused}))
"""


def test_the_guard_forwards_only_exact_loopback_and_never_resolves_external_names():
    out = subprocess.run([sys.executable, "-c", _PROBE, str(GUARD)], capture_output=True, text=True, timeout=60,
                         env={"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])

    assert result["forwarded"]["resolve"] == ["localhost", "127.0.0.1", "::1"], (
        "an external name reached the OS resolver (a DNS query would have left the machine)")
    assert result["forwarded"]["connect"] == ["127.0.0.1", "::1"]
    assert ["resolve", "localhost.review.invalid"] in result["refused"], "prefix matching let a lookalike through"
    assert ["connect", "93.184.216.34"] in result["refused"]
