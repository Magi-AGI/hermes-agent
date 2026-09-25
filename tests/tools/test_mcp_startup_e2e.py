"""MCP startup discovery end to end: real stdio servers and a loopback OAuth endpoint, one fresh
process per run with its own HERMES_HOME (gate item G8).

Covers: a server that never answers ``initialize`` and whose helper escapes the SDK's job object
(child cleanup), an expired OAuth token and an unreachable server (discovery stays
non-interactive and bounded while a healthy server registers), two profiles starting at once,
and the schema cache serving ``lazy: true`` servers without a spawn.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import psutil
import pytest
import yaml

pytest.importorskip("mcp.server")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_FAST_SERVER = """
from mcp.server import MCPServer
mcp = MCPServer("fast")

@mcp.tool()
def ping() -> str:
    return "pong"

if __name__ == "__main__":
    mcp.run(transport="stdio")
"""

# Spawns a helper first (like npx -> node), then never answers initialize.
_HANG_SERVER = """
import os, subprocess, sys
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(os.environ["HANG_PIDFILE"], "w").write(str(helper.pid))
sys.stdin.read()
"""

_CHILD = """
import json, os, sys, threading, time
repo, home = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
os.environ["HERMES_HOME"] = home
import webbrowser
browser = []
webbrowser.open = lambda *a, **k: browser.append(a) or True
from tools.registry import registry
from tools.mcp_tool_discovery import discover_mcp_tools, get_mcp_status

started = time.monotonic()
fast_at = []

def _watch():
    while not fast_at and time.monotonic() - started < 120:
        if any(e.name.startswith("mcp__fast__") for e in registry.get_all_entries()):
            fast_at.append(time.monotonic() - started)
        time.sleep(0.05)

threading.Thread(target=_watch, daemon=True).start()
discover_mcp_tools()
elapsed = time.monotonic() - started
time.sleep(float(os.environ.get("POST_DISCOVERY_SLEEP", "0")))
import psutil
spawned = [p.pid for p in psutil.Process().children(recursive=True)]
helper_alive = None
pidfile = os.environ.get("HANG_PIDFILE")
if pidfile and os.path.exists(pidfile):
    helper_alive = psutil.pid_exists(int(open(pidfile).read()))
print("RESULT " + json.dumps({
    "elapsed": elapsed, "fast_at": fast_at[0] if fast_at else None, "browser": len(browser),
    "status": {e["name"]: {"status": e["status"], "error": e.get("error")} for e in get_mcp_status()},
    "spawned": spawned, "helper_alive": helper_alive}), flush=True)
os._exit(0)
"""


def _write_home(home: Path, servers: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": servers}), encoding="utf-8")


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    if not path.exists():
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _discover(tmp_path: Path, home: Path, *, extra_env: dict | None = None, wait: bool = True):
    child = _script(tmp_path, "discover_child.py", _CHILD)
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN"))}
    env.update({"HERMES_HOME": str(home), "PYTHONPATH": str(_REPO_ROOT), **(extra_env or {})})
    proc = subprocess.Popen([sys.executable, str(child), str(_REPO_ROOT), str(home)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    return _result(proc) if wait else proc


def _result(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=180)
    for line in out.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    raise AssertionError(f"discovery child produced no result (rc={proc.returncode}):\n{err[-4000:]}")


def _kill_leftover(pidfile: Path) -> None:
    if pidfile.exists():
        try:
            proc = psutil.Process(int(pidfile.read_text()))
            if proc.cmdline()[-1] == "import time; time.sleep(600)":
                proc.kill()
        except (psutil.Error, ValueError, IndexError):
            pass


def test_failed_server_leaves_no_helper_behind_while_the_backend_lives(tmp_path):
    """A server that never answers ``initialize`` is torn down at its connect timeout. Anything it
    spawned goes with it, even a helper that escaped the SDK's job object: before the fix every
    failed attempt stranded one (the leaked ``mcp-remote`` bridges)."""
    home = tmp_path / "home"
    pidfile = tmp_path / "helper.pid"
    hang = _script(tmp_path, "hang_server.py", _HANG_SERVER)
    _write_home(home, {"hang": {"command": sys.executable, "args": [str(hang)], "connect_timeout": 3,
                                "env": {"HANG_PIDFILE": str(pidfile)}}})
    try:
        result = _discover(tmp_path, home, extra_env={"HANG_PIDFILE": str(pidfile),
                                                      "POST_DISCOVERY_SLEEP": "3"})
    finally:
        alive_after_run = pidfile.exists() and psutil.pid_exists(int(pidfile.read_text()))
        _kill_leftover(pidfile)
    assert result["status"]["hang"]["status"] == "failed"
    assert result["helper_alive"] is False
    assert not alive_after_run


def test_expired_token_and_dead_server_fail_fast_while_a_healthy_server_registers(tmp_path):
    """Startup never goes interactive: an expired OAuth token whose refresh is rejected parks the
    server as needs-auth without a browser or a callback wait, an unreachable URL fails at its own
    bound, and neither holds up the healthy server."""
    hits: list = []

    class _AuthServer(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def _json(self, code, body, headers=None):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _base(self):
            return f"http://127.0.0.1:{self.server.server_address[1]}"

        def _challenge(self):
            meta = f'Bearer resource_metadata="{self._base()}/.well-known/oauth-protected-resource"'
            self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": meta})

        def do_GET(self):
            hits.append(("GET", self.path))
            if self.path.startswith("/.well-known/oauth-protected-resource"):
                return self._json(200, {"resource": self._base() + "/mcp", "authorization_servers": [self._base()]})
            if self.path.startswith("/.well-known/"):
                return self._json(200, {
                    "issuer": self._base(), "authorization_endpoint": self._base() + "/authorize",
                    "token_endpoint": self._base() + "/token", "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"]})
            return self._challenge()

        def do_POST(self):
            hits.append(("POST", self.path))
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path.startswith("/token"):
                return self._json(400, {"error": "invalid_grant"})
            return self._challenge()

    class _Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    auth = _Threaded(("127.0.0.1", 0), _AuthServer)
    threading.Thread(target=auth.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    tokens = home / "mcp-tokens"
    tokens.mkdir(parents=True)
    (tokens / "wiki.json").write_text(json.dumps({
        "access_token": "expired", "token_type": "Bearer", "refresh_token": "stale",
        "expires_in": 3600, "expires_at": time.time() - 3600}), encoding="utf-8")
    (tokens / "wiki.client.json").write_text(json.dumps({
        "client_id": "fake", "redirect_uris": ["http://127.0.0.1:1/callback"],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"]}), encoding="utf-8")
    fast = _script(tmp_path, "fast_server.py", _FAST_SERVER)
    _write_home(home, {
        "fast": {"command": sys.executable, "args": [str(fast)]},
        "wiki": {"url": f"http://127.0.0.1:{auth.server_address[1]}/mcp", "auth": "oauth", "connect_timeout": 20},
        "gone": {"url": "http://127.0.0.1:9/mcp", "connect_timeout": 4},
    })
    try:
        result = _discover(tmp_path, home)
    finally:
        auth.shutdown()
    assert result["status"]["fast"]["status"] == "connected"
    assert result["status"]["wiki"]["status"] == "failed"
    assert result["status"]["gone"]["status"] == "failed"
    assert result["browser"] == 0
    assert ("POST", "/token") in hits  # the refresh was tried before giving up
    assert result["elapsed"] < 20  # "gone" is bounded by its own 4 s; "wiki" never waits on a callback


def test_two_profiles_starting_together_do_not_wait_on_each_other(tmp_path):
    """Each profile home has its own discovery lock, so one profile's slow server never delays
    another profile's healthy one."""
    fast = _script(tmp_path, "fast_server.py", _FAST_SERVER)
    hang = _script(tmp_path, "hang_server.py", _HANG_SERVER)
    homes, pidfiles, procs = [], [], []
    for name in ("alpha", "beta"):
        home = tmp_path / name
        pidfile = tmp_path / f"{name}.pid"
        _write_home(home, {
            "fast": {"command": sys.executable, "args": [str(fast)]},
            "slow": {"command": sys.executable, "args": [str(hang)], "connect_timeout": 12,
                     "env": {"HANG_PIDFILE": str(pidfile)}},
        })
        homes.append(home)
        pidfiles.append(pidfile)
    try:
        procs = [_discover(tmp_path, home, extra_env={"HANG_PIDFILE": str(pf)}, wait=False)
                 for home, pf in zip(homes, pidfiles)]
        results = [_result(proc) for proc in procs]
    finally:
        for pidfile in pidfiles:
            _kill_leftover(pidfile)
    for result in results:
        assert result["fast_at"] is not None and result["fast_at"] < 10, result


def test_lazy_server_registers_from_the_schema_cache_without_spawning(tmp_path):
    """A live connect writes the schema cache without the SDK's default ``ttl_ms=0``, so the next
    start serves a ``lazy: true`` server from it with no process spawned. A legacy ``ttl_ms=0``
    entry is a miss once, and the connect it forces rewrites it."""
    fast = _script(tmp_path, "fast_server.py", _FAST_SERVER)
    home = tmp_path / "home"
    _write_home(home, {"fast": {"command": sys.executable, "args": [str(fast)], "lazy": True}})
    cache = home / "cache" / "mcp_schema_cache.json"

    cold = _discover(tmp_path, home)
    assert cold["status"]["fast"]["status"] == "connected"
    assert "ttl_ms" not in json.loads(cache.read_text(encoding="utf-8"))["fast"]

    warm = _discover(tmp_path, home)
    assert warm["status"]["fast"]["status"] == "lazy"
    assert warm["fast_at"] is not None and warm["spawned"] == []

    entries = json.loads(cache.read_text(encoding="utf-8"))
    entries["fast"].update({"ttl_ms": 0, "written_at": time.time()})
    cache.write_text(json.dumps(entries), encoding="utf-8")
    legacy = _discover(tmp_path, home)
    assert legacy["status"]["fast"]["status"] == "connected"
    healed = _discover(tmp_path, home)
    assert healed["status"]["fast"]["status"] == "lazy" and healed["spawned"] == []
