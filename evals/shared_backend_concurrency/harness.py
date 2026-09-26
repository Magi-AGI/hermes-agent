"""~27 session windows against ONE real ``serve`` backend: the shared-gateway acceptance run.

Drives a throwaway backend (temp HERMES_HOME, ``serve --isolated``) with a deterministic loopback
model server, no vendor inference and no network. Each "window" is one WebSocket client that
answers server->client requests like the desktop does. Phases (DESIGN-SHARED-GATEWAY-CONCURRENCY
§5 + review notes):

  A  cold start: every window connects and creates a session at once
  B  picker burst: every window asks model.options within one second (optionally while the model
     server's /models hangs: a deliberately stuck catalog job)
  C  turns: 12 concurrent first turns (plain, tool, approval), 3 long turns stopped mid-stream,
     the other windows listing sessions and running slash commands meanwhile
  D  compaction: 4 manual /compress at once while other windows keep issuing slash commands
  E  reconnect burst: every socket dropped at once, then all reconnect and resume together
  F  (--freeze) the D7 signature on a real backend: every process of the tree suspended; /api/health
     must go silent and freeze_dump must capture the whole tree

It reports per-method latency percentiles, the /api/health latency (loop-drift proxy), connection
refusals, loopback socket states before/after, lost or duplicated turns, and "live owner" errors
(D4), and grades them against the pass criteria Lake approved (2026-09-24):
  session.resume p95 < 2 s and max < 10 s; model.options p95 < 3 s; no loop drift > 5 s;
  zero ECONNREFUSED; FinWait2 back to baseline within 60 s; zero lost or duplicated submits.

  python evals/shared_backend_concurrency/harness.py --repo . --out <dir> [--windows 27] [--freeze]
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL_ID = "concurrency-fixture"
TOKEN = "concurrency-fixture-token"


# ── deterministic model server ─────────────────────────────────────────────────────────────────

class ModelState:
    def __init__(self, reply_delay: float, compress_delay: float) -> None:
        self.reply_delay = reply_delay
        self.compress_delay = compress_delay
        self.hang_models = threading.Event()
        self.calls = defaultdict(int)
        self.lock = threading.Lock()


def _make_model_handler(state: ModelState):
    class Model(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            return

        def do_GET(self):
            if state.hang_models.is_set():
                time.sleep(30)
            # models.dev: an empty registry (a failed refresh to the loader), never a fake catalog.
            body = (b"{}" if self.path.startswith("/models-dev")
                    else json.dumps({"data": [{"id": MODEL_ID, "context_length": 128000}]}).encode())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = body.get("messages") or []
            last = messages[-1] if messages else {}
            text = json.dumps(messages[-3:]) if messages else ""
            kind = ("summary" if "summar" in json.dumps(messages[:2]).lower() and not body.get("tools")
                    else "after_tool" if last.get("role") == "tool"
                    else "approval" if "[approval]" in text
                    else "tool" if "[tool]" in text
                    else "slow" if "[slow]" in text
                    else "plain")
            with state.lock:
                state.calls[kind] += 1
            try:
                self._answer(kind, bool(body.get("stream")))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the agent hung up (interrupt): expected for [slow]

        def _answer(self, kind: str, stream: bool) -> None:
            if kind in ("tool", "approval"):
                command = "rm -rf ./harness-approval-probe-does-not-exist" if kind == "approval" else "echo harness"
                call = {"index": 0, "id": f"call_{kind}", "type": "function",
                        "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}
                chunks = [({"role": "assistant", "tool_calls": [call]}, None), ({}, "tool_calls")]
                delay = 0.0
            elif kind == "summary":
                time.sleep(state.compress_delay)
                chunks = [({"role": "assistant", "content": "Summary: the fixture conversation so far."}, None), ({}, "stop")]
                delay = 0.0
            elif kind == "slow":
                chunks = [({"role": "assistant", "content": f"slow {i} "}, None) for i in range(60)] + [({}, "stop")]
                delay = 0.5
            else:
                chunks = [({"role": "assistant", "content": f"part {i} "}, None) for i in range(8)] + [({}, "stop")]
                delay = state.reply_delay / 8
            self.send_response(200)
            if not stream:
                content = "".join(d.get("content", "") for d, _ in chunks)
                message = {"role": "assistant", "content": content or None}
                if kind in ("tool", "approval"):
                    message["tool_calls"] = [{k: v for k, v in chunks[0][0]["tool_calls"][0].items() if k != "index"}]
                payload = json.dumps({"id": "fx", "object": "chat.completion", "model": MODEL_ID,
                                      "choices": [{"index": 0, "message": message, "finish_reason": chunks[-1][1]}],
                                      "usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_tokens": 1010}}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for delta, finish in chunks:
                chunk = {"id": "fx", "object": "chat.completion.chunk", "model": MODEL_ID,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
                if delay:
                    time.sleep(delay)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

    return Model


# ── one window ─────────────────────────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latency = defaultdict(list)          # (phase, method) -> seconds
        self.errors = []                          # {phase, method, code, message}
        self.refused = 0
        self.approvals = 0
        self.busy_retries = 0
        self.successes = defaultdict(int)          # (phase, method) -> calls answered with a result

    def record(self, phase: str, method: str, seconds: float, error: dict | None) -> None:
        with self.lock:
            self.latency[(phase, method)].append(seconds)
            if not error:
                self.successes[(phase, method)] += 1
            if error:
                self.errors.append({"phase": phase, "method": method, "code": error.get("code"),
                                    "message": str(error.get("message"))[:200], "retry": (error.get("data") or {}).get("retry")})


class Window:
    _ids = itertools.count(1)

    def __init__(self, index: int, port: int, metrics: Metrics) -> None:
        self.index, self.port, self.metrics = index, port, metrics
        self.ws = None
        self.pending: dict = {}
        self.events: list = []
        self.cond = threading.Condition()
        self.session_id = None
        self.stored_id = None

    def connect(self) -> None:
        from websockets.sync.client import connect
        try:
            self.ws = connect(f"ws://127.0.0.1:{self.port}/api/ws?token={TOKEN}", max_size=64_000_000, open_timeout=60)
        except ConnectionRefusedError:
            with self.metrics.lock:
                self.metrics.refused += 1
            raise
        threading.Thread(target=self._read, daemon=True, name=f"window-{self.index}").start()
        self.rpc("-", "client.capabilities", {"server_requests": True})

    def _read(self) -> None:
        ws = self.ws
        try:
            while True:
                frame = json.loads(ws.recv())
                if frame.get("method") == "event":
                    with self.cond:
                        self.events.append((time.monotonic(), frame["params"]))
                        self.cond.notify_all()
                elif isinstance(frame.get("id"), str) and frame.get("method"):
                    self._answer_request(ws, frame)
                else:
                    with self.cond:
                        self.pending[frame.get("id")] = frame
                        self.cond.notify_all()
        except Exception:
            with self.cond:
                self.cond.notify_all()

    def _answer_request(self, ws, frame: dict) -> None:
        if frame["method"] == "approval":
            with self.metrics.lock:
                self.metrics.approvals += 1
            reply = {"jsonrpc": "2.0", "id": frame["id"], "result": {"choice": "deny"}}
        else:
            reply = {"jsonrpc": "2.0", "id": frame["id"], "error": {"code": -32601, "message": "harness: no handler"}}
        ws.send(json.dumps(reply))

    def rpc(self, phase: str, method: str, params: dict, timeout: float = 180.0):
        """One call as the desktop makes it: a BUSY (5037) marked retry "auto" is retried with the shared
        client's jittered backoff inside the same budget, and the sample is the time to the FINAL answer."""
        started = time.monotonic()
        deadline = started + timeout
        attempt = 0
        while True:
            attempt += 1
            rid = next(self._ids)
            self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
            with self.cond:
                self.cond.wait_for(lambda: rid in self.pending, timeout=max(0.0, deadline - time.monotonic()))
                frame = self.pending.pop(rid, None)
            error = frame.get("error") if frame else {"code": "timeout", "message": f"no reply in {timeout}s"}
            data = (error or {}).get("data") or {}
            if error and error.get("code") == 5037 and data.get("retry") == "auto":
                delay = (data.get("retry_after_ms") or 500) / 1000 * min(attempt, 4) * random.uniform(0.5, 1.5)
                if time.monotonic() + delay < deadline:
                    with self.metrics.lock:
                        self.metrics.busy_retries += 1
                    time.sleep(delay)
                    continue
            break
        if phase != "-":
            self.metrics.record(phase, method, time.monotonic() - started, error)
        return None if error else frame.get("result")

    def wait_turn_end(self, since: float, timeout: float = 180.0) -> list:
        """Events for this session after *since* up to the first message.complete / error."""
        def done():
            return any(at >= since and p.get("session_id") == self.session_id and p.get("type") in ("message.complete", "error")
                       for at, p in self.events)
        with self.cond:
            self.cond.wait_for(done, timeout=timeout)
            return [p for at, p in self.events if at >= since and p.get("session_id") == self.session_id]

    def drop(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


# ── measurements outside the RPC path ─────────────────────────────────────────────────────────

class NetworkWatch(threading.Thread):
    """Every non-loopback connection the backend tree opens during the run (there must be none)."""

    def __init__(self, root_pid: int) -> None:
        super().__init__(daemon=True)
        self.root_pid, self.remote, self.stop = root_pid, set(), threading.Event()

    def run(self) -> None:
        import psutil
        while not self.stop.is_set():
            try:
                tree = [psutil.Process(self.root_pid), *psutil.Process(self.root_pid).children(recursive=True)]
            except psutil.Error:
                tree = []
            for proc in tree:
                try:
                    for conn in proc.net_connections(kind="inet"):
                        if conn.raddr and conn.raddr.ip not in ("127.0.0.1", "::1"):
                            self.remote.add(f"{proc.pid}->{conn.raddr.ip}:{conn.raddr.port}")
                except psutil.Error:
                    continue
            self.stop.wait(1.0)


class HealthMonitor(threading.Thread):
    """GET /api/health every 0.5 s: the backend loop's responsiveness (loop-drift proxy)."""

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port, self.samples, self.stop = port, [], threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=15).read()
                self.samples.append(time.monotonic() - started)
            except (urllib.error.URLError, OSError, TimeoutError):
                self.samples.append(15.0)
            self.stop.wait(0.5)


def socket_states(port: int) -> dict:
    if sys.platform != "win32":
        return {}
    script = (f"Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object {{ $_.LocalPort -eq {port} -or $_.RemotePort -eq {port} }}"
              " | Group-Object State | ForEach-Object { \"$($_.Name)=$($_.Count)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60).stdout
    return dict(line.split("=", 1) for line in out.split() if "=" in line)


def pct(values: list, q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 3)


# ── scenario ───────────────────────────────────────────────────────────────────────────────────

def run_parallel(fns) -> list:
    results = [None] * len(fns)

    def call(i, fn):
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - a failed window is a finding, recorded as such
            results[i] = exc
    threads = [threading.Thread(target=call, args=(i, fn), daemon=True) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=600)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--windows", type=int, default=27)
    parser.add_argument("--port", type=int, default=18640)
    parser.add_argument("--reply-delay", type=float, default=1.0)
    parser.add_argument("--compress-delay", type=float, default=3.0)
    parser.add_argument("--stuck-catalog", action="store_true", help="hang the model server's /models during phase B")
    parser.add_argument("--freeze", action="store_true", help="phase F: suspend the backend tree and dump it")
    parser.add_argument("--pyspy", type=Path, help="py-spy executable: sample the backend during phase E")
    parser.add_argument("--node-dir", type=Path, help="Node directory: phase F runs the desktop's D7 liveness "
                                                       "supervisor (apps/desktop, vitest) against the frozen backend")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    repo = args.repo.resolve()

    state = ModelState(args.reply_delay, args.compress_delay)
    model = ThreadingHTTPServer(("127.0.0.1", args.port + 1), _make_model_handler(state))
    model.daemon_threads = True
    threading.Thread(target=model.serve_forever, daemon=True).start()

    import yaml
    home = Path(tempfile.mkdtemp(prefix="concurrency-harness-"))
    workdir = home / "work"
    workdir.mkdir()
    config = {"model": {"default": MODEL_ID, "provider": "custom", "base_url": f"http://127.0.0.1:{args.port + 1}/v1"},
              "agent": {"max_turns": 4}, "toolsets": ["terminal"], "platform_toolsets": {"gui": ["terminal"], "cli": ["terminal"]},
              "terminal": {"backend": "local", "cwd": str(workdir)},
              "memory": {"memory_enabled": False, "user_profile_enabled": False},
              "stt": {"enabled": False},  # the local STT prewarm would download a Whisper model
              # Background catalog fetches stay on loopback (the guard would refuse them anyway).
              "model_catalog": {"enabled": False},
              "models_dev": {"url": f"http://127.0.0.1:{args.port + 1}/models-dev.json"},
              "auxiliary": {"compression": {"provider": "custom", "model": MODEL_ID, "base_url": f"http://127.0.0.1:{args.port + 1}/v1"}}}
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (home / ".env").write_text("OPENAI_API_KEY=local-fixture\n", encoding="utf-8")
    (home / "SOUL.md").write_text("You are a deterministic test assistant.\n", encoding="utf-8")
    (home / ".no-bundled-skills").touch()
    if (repo / ".env").exists():
        # hermes_cli.main loads (and may sanitize in place) the checkout's .env at startup.
        raise SystemExit(f"{repo / '.env'} exists: the backend would read and may rewrite it; move it aside first")
    # Start from the OS basics only: inherited cache overrides (HF_HOME, XDG_*, pip/uv caches), proxies,
    # credentials or HERMES_* settings must not reach the throwaway backend.
    keep = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "NUMBER_OF_PROCESSORS", "OS",
            "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "COMMONPROGRAMFILES", "LANG", "LC_ALL"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    # Machine-wide locations must not be the real user's: on Windows Path.home() reads USERPROFILE (not
    # HOME), and the host-backend lock + rendezvous record live under it by default. A throwaway backend
    # there could claim the host lock and be offered to the LIVE desktop as its backend.
    os_home = home / "os-home"
    for sub in ("AppData/Local", "AppData/Roaming", "state", "gateway-locks", "cache", "tmp", "managed"):
        (os_home / sub).mkdir(parents=True, exist_ok=True)
    env.update(HERMES_HOME=str(home), HOME=str(os_home), USERPROFILE=str(os_home),
               LOCALAPPDATA=str(os_home / "AppData" / "Local"), APPDATA=str(os_home / "AppData" / "Roaming"),
               XDG_STATE_HOME=str(os_home / "state"), HERMES_GATEWAY_LOCK_DIR=str(os_home / "gateway-locks"),
               XDG_CACHE_HOME=str(os_home / "cache"), HF_HOME=str(os_home / "cache" / "hf"),
               TEMP=str(os_home / "tmp"), TMP=str(os_home / "tmp"), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               # An EMPTY managed scope: otherwise a system one (/etc/hermes) could override the fake route.
               HERMES_MANAGED_DIR=str(os_home / "managed"),
               HERMES_DASHBOARD_SESSION_TOKEN=TOKEN, HERMES_IGNORE_RULES="1",
               # netguard/ first: its sitecustomize refuses (and logs) every non-loopback connect.
               PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parent / "netguard"), str(repo))),
               HARNESS_EGRESS_LOG=str(args.out / "egress-refused.log"),
               # D6 only: the harness sitecustomize admits the fixture model as a compaction route in
               # THIS throwaway backend (the real guard refuses it); nothing else changes.
               HARNESS_ALLOW_FIXTURE_COMPACTION="1",
               OPENAI_API_KEY="local-fixture")
    serve_log = (args.out / "serve.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "serve", "--isolated", "--port", str(args.port)],
                            cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=serve_log, stderr=subprocess.STDOUT)
    report: dict = {"windows": args.windows, "freeze_requested": bool(args.freeze), "home": str(home), "backend_pid": proc.pid,
                    "sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()}
    metrics = Metrics()
    health = HealthMonitor(args.port)
    windows = [Window(i, args.port, metrics) for i in range(args.windows)]
    try:
        for _ in range(360):
            if proc.poll() is not None:
                raise RuntimeError(f"serve exited {proc.returncode}; see serve.log")
            try:
                with socket.create_connection(("127.0.0.1", args.port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        report["sockets_before"] = socket_states(args.port)
        health.start()
        network = NetworkWatch(proc.pid)
        network.start()

        # A: cold start
        t0 = time.monotonic()
        def open_window(w):
            w.connect()
            seed = [{"role": "user", "content": f"seed question {i}"} if i % 2 == 0 else
                    {"role": "assistant", "content": f"seed answer {i} " + "detail " * 40} for i in range(40)]
            created = w.rpc("A", "session.create", {"source": "desktop", "cwd": str(workdir), "title": f"window {w.index}",
                                                    **({"messages": seed} if w.index < 5 else {})})
            w.session_id, w.stored_id = created["session_id"], created["stored_session_id"]
        report["phase_A_failures"] = [repr(r) for r in run_parallel([lambda w=w: open_window(w) for w in windows]) if r]
        report["phase_A_s"] = round(time.monotonic() - t0, 2)
        live = [w for w in windows if w.session_id]

        # B: picker burst (optionally with a stuck catalog)
        if args.stuck_catalog:
            state.hang_models.set()
        t0 = time.monotonic()
        run_parallel([lambda w=w: w.rpc("B", "model.options", {"session_id": w.session_id}) for w in live]
                     + [lambda w=w: w.rpc("B", "session.list", {"limit": 50}) for w in live[:8]])
        state.hang_models.clear()
        report["phase_B_s"] = round(time.monotonic() - t0, 2)

        # C: turns + stops + background reads
        turners, stoppers, stream_stoppers, readers = live[5:17], live[17:20], live[20:22], live[22:]
        submitted: dict = {}

        def take_turn(w, kind):
            since = time.monotonic()
            prompt = {"tool": "run it [tool]", "approval": "clean up [approval]"}.get(kind, f"hello from window {w.index}")
            result = w.rpc("C", "prompt.submit", {"session_id": w.session_id, "text": prompt})
            submitted[w.index] = (prompt, result is not None)
            events = w.wait_turn_end(since)
            return sum(1 for p in events if p.get("type") == "message.complete")

        def stop_turn(w):
            since = time.monotonic()
            w.rpc("C", "prompt.submit", {"session_id": w.session_id, "text": "long answer please [slow]"})
            time.sleep(2.0)
            started = time.monotonic()
            status = (w.rpc("C", "session.interrupt", {"session_id": w.session_id}) or {}).get("status")
            w.wait_turn_end(since, timeout=60)
            return {"status": status, "stop_to_turn_end_s": round(time.monotonic() - started, 2)}

        def stop_while_streaming(w):
            """Stop a turn that is already streaming: wait for its first message.delta, then interrupt."""
            since = time.monotonic()
            w.rpc("C", "prompt.submit", {"session_id": w.session_id, "text": "long answer please [slow]"})
            with w.cond:
                streamed = w.cond.wait_for(lambda: any(
                    at >= since and p.get("session_id") == w.session_id and p.get("type") == "message.delta"
                    for at, p in w.events), timeout=60)
            started = time.monotonic()
            status = (w.rpc("C", "session.interrupt", {"session_id": w.session_id}) or {}).get("status")
            w.wait_turn_end(started, timeout=60)
            return {"streamed_before_stop": bool(streamed), "status": status,
                    "stop_to_turn_end_s": round(time.monotonic() - started, 2)}

        def keep_reading(w, until):
            while time.monotonic() < until:
                w.rpc("C", "session.list", {"limit": 50})
                w.rpc("C", "slash.exec", {"session_id": w.session_id, "command": "/help"})
        t0 = time.monotonic()
        kinds = ["plain", "tool", "approval"]
        results = run_parallel([lambda w=w, k=kinds[i % 3]: take_turn(w, k) for i, w in enumerate(turners)]
                               + [lambda w=w: stop_turn(w) for w in stoppers]
                               + [lambda w=w: stop_while_streaming(w) for w in stream_stoppers]
                               + [lambda w=w: keep_reading(w, time.monotonic() + 20) for w in readers])
        report["phase_C_s"] = round(time.monotonic() - t0, 2)
        completes = results[:len(turners)]
        report["turn_message_complete_counts"] = completes
        report["stops"] = [r if isinstance(r, dict) else repr(r) for r in results[len(turners):len(turners) + len(stoppers)]]
        first = len(turners) + len(stoppers)
        report["streaming_stops"] = [r if isinstance(r, dict) else repr(r) for r in results[first:first + len(stream_stoppers)]]

        # lost / duplicated submits: the stored transcript holds each prompt exactly once
        dupes, lost = [], []
        for w in turners:
            prompt, accepted = submitted.get(w.index, (None, False))
            history = w.rpc("-", "session.history", {"session_id": w.session_id}) or {}
            hits = sum(1 for m in history.get("messages", [])
                       if m.get("role") == "user" and prompt and prompt in str(m.get("text") or m.get("content") or ""))
            (dupes if hits > 1 else lost if accepted and hits == 0 else []).append(w.index)
        report["submits_lost"], report["submits_duplicated"] = lost, dupes

        # D (D6): compaction under load, through the real dispatch -> llm lane -> compaction path. First
        # one compaction alone (the baseline), then four at once while other windows keep typing. The
        # fixture summarizes in a fixed --compress-delay, so any extra time is the backend's own.
        # Provider throughput (the ~8 min seen live) is NOT measured here.
        def compress(w, phase):
            started = time.monotonic()
            result = w.rpc(phase, "session.compress", {"session_id": w.session_id}, timeout=600) or {}
            return {"window": w.index, "status": result.get("status"), "s": round(time.monotonic() - started, 2)}

        report["compaction_alone"] = compress(live[4], "D0")
        compactors, typists = live[:4], live[5:13]
        t0 = time.monotonic()
        stop_typing = time.monotonic() + 3 * args.compress_delay + 5

        def keep_typing(w):
            while time.monotonic() < stop_typing:
                w.rpc("D", "slash.exec", {"session_id": w.session_id, "command": "/help"})
        results = run_parallel([lambda w=w: compress(w, "D") for w in compactors]
                               + [lambda w=w: keep_typing(w) for w in typists])
        report["compactions_concurrent"] = [r if isinstance(r, dict) else repr(r) for r in results[:len(compactors)]]
        report["phase_D_s"] = round(time.monotonic() - t0, 2)

        # E: reconnect burst
        for w in live:
            w.drop()
        time.sleep(1.0)
        sampler = None
        if args.pyspy:
            import psutil
            # The venv launcher's direct child is the serve interpreter (slash workers hang below it).
            worker = next(iter(psutil.Process(proc.pid).children()), psutil.Process(proc.pid)).pid
            sampler = subprocess.Popen([str(args.pyspy), "record", "--pid", str(worker), "--duration", "8", "--rate", "250",
                                        "--format", "raw", "--threads", "--nonblocking", "-o", str(args.out / "phase_E.collapsed")],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
        t0 = time.monotonic()
        def reopen(w):
            w.connect()
            resumed = w.rpc("E", "session.resume", {"session_id": w.stored_id, "source": "desktop"})
            if not (isinstance(resumed, dict) and resumed.get("session_id") and isinstance(resumed.get("messages"), list)):
                raise RuntimeError(f"window {w.index}: session.resume returned no usable snapshot: {str(resumed)[:200]}")
            w.session_id = resumed["session_id"]
        report["phase_E_failures"] = [repr(r) for r in run_parallel([lambda w=w: reopen(w) for w in live]) if r]
        report["phase_E_s"] = round(time.monotonic() - t0, 2)
        if sampler is not None:
            sampler.wait(timeout=60)
        for w in live:
            w.drop()
        report["sockets_after_burst"] = socket_states(args.port)
        time.sleep(60)
        report["sockets_after_60s"] = socket_states(args.port)

        network.stop.set()
        report["non_loopback_connections"] = sorted(network.remote)
        egress = args.out / "egress-refused.log"
        report["egress_refused"] = egress.read_text(encoding="utf-8").splitlines() if egress.exists() else []
        if args.freeze:
            health.stop.set()  # phase F freezes the backend on purpose: it is not loop drift
            health.join(timeout=20)
            try:
                report["phase_F"] = freeze_phase(proc.pid, args.port, args.out, repo, args.node_dir, env)
            except Exception as exc:  # noqa: BLE001 - a requested phase that failed is a failed grade
                report["phase_F"] = {"error": repr(exc)}
    finally:
        health.stop.set()
        for w in windows:
            w.drop()
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"] if sys.platform == "win32" else ["kill", "-9", str(proc.pid)],
                       capture_output=True)
        model.shutdown()

    report["health_s"] = {"p50": pct(health.samples, 0.5), "p95": pct(health.samples, 0.95),
                          "max": max(health.samples) if health.samples else None, "samples": len(health.samples)}
    report["latency"] = {f"{phase}:{method}": {"n": len(v), "p50": pct(v, 0.5), "p95": pct(v, 0.95), "max": round(max(v), 3)}
                         for (phase, method), v in sorted(metrics.latency.items())}
    report["errors"] = metrics.errors
    report["busy_retries"] = metrics.busy_retries
    report["successes"] = {f"{phase}:{method}": n for (phase, method), n in sorted(metrics.successes.items())}
    report["live_owner_errors"] = [e for e in metrics.errors if "live owner" in e["message"]]
    report["refused_connections"] = metrics.refused
    report["approvals_answered"] = metrics.approvals
    report["model_calls"] = dict(state.calls)
    report["grades"] = grade(report, metrics)
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"grades": report["grades"], "latency": report["latency"], "health_s": report["health_s"]}, indent=2))
    return 0 if all(g["pass"] for g in report["grades"].values()) else 1


def grade(report: dict, metrics: Metrics) -> dict:
    resume = metrics.latency.get(("E", "session.resume"), [])
    options = metrics.latency.get(("B", "model.options"), [])
    windows = report["windows"]
    resumed_ok = metrics.successes.get(("E", "session.resume"), 0)
    options_ok = metrics.successes.get(("B", "model.options"), 0)
    freeze = report.get("phase_F")
    before = int(report.get("sockets_before", {}).get("FinWait2", 0))
    after = int(report.get("sockets_after_60s", {}).get("FinWait2", 0))
    completes = report.get("turn_message_complete_counts", [])
    return {
        # Latency grades only mean something when EVERY window's call succeeded (a refusal is fast).
        "every_resume_succeeded": {"value": f"{resumed_ok}/{windows}", "pass": resumed_ok == windows},
        "every_model_options_succeeded": {"value": f"{options_ok}/{windows}", "pass": options_ok == windows},
        "resume_p95_lt_2s": {"value": pct(resume, 0.95), "pass": bool(resume) and pct(resume, 0.95) < 2.0},
        "resume_max_lt_10s": {"value": max(resume) if resume else None, "pass": bool(resume) and max(resume) < 10.0},
        "model_options_p95_lt_3s": {"value": pct(options, 0.95), "pass": bool(options) and pct(options, 0.95) < 3.0},
        "loop_drift_lt_5s": {"value": report["health_s"]["max"], "pass": (report["health_s"]["max"] or 0) < 5.0},
        "zero_econnrefused": {"value": report["refused_connections"], "pass": report["refused_connections"] == 0},
        "finwait2_back_to_baseline_60s": {"value": [before, after], "pass": after <= before},
        "zero_lost_or_duplicated_submits": {"value": [report["submits_lost"], report["submits_duplicated"]],
                                            "pass": not report["submits_lost"] and not report["submits_duplicated"]},
        "every_turn_completed_once": {"value": completes, "pass": all(c == 1 for c in completes)},
        "no_live_owner_errors_D4": {"value": len(report["live_owner_errors"]), "pass": not report["live_owner_errors"]},
        # Beyond Lake's list (review note: stop responsiveness during bursts): a stop ends its turn in 5 s.
        "stop_while_streaming_ends_turn_within_5s": {
            "value": report.get("streaming_stops"),
            "pass": bool(report.get("streaming_stops")) and all(
                isinstance(r, dict) and r["streamed_before_stop"] and r["status"] == "interrupted"
                and r["stop_to_turn_end_s"] < 5.0 for r in report["streaming_stops"])},
        "stop_ends_turn_within_5s": {"value": report.get("stops"),
                                     "pass": all(isinstance(r, dict) and r["status"] == "interrupted"
                                                 and r["stop_to_turn_end_s"] < 5.0 for r in report.get("stops", []))},
        # Busy retries are normal and inside the latency samples; any error still standing after them
        # (an unrecovered 5037 included) is a failure.
        "no_rpc_errors_after_retries": {"value": len(report["errors"]), "pass": not report["errors"]},
        "no_non_loopback_connections": {"value": report.get("non_loopback_connections"),
                                        "pass": not report.get("non_loopback_connections")},
        # Informational: background fetches the guard refused (they must fail soft, never block a phase).
        "egress_refused_by_guard": {"value": report.get("egress_refused"), "pass": True},
        "all_windows_opened_and_reopened": {"value": [report["phase_A_failures"], report["phase_E_failures"]],
                                            "pass": not report["phase_A_failures"] and not report["phase_E_failures"]},
        "every_compaction_compressed_D6": {
            "value": [report.get("compaction_alone"), report.get("compactions_concurrent")],
            "pass": all(isinstance(r, dict) and r.get("status") == "compressed"
                        for r in [report.get("compaction_alone"), *(report.get("compactions_concurrent") or [])])},
        # Four at once must not queue behind each other in the backend: each takes about as long as one.
        "compactions_not_serialized_D6": {
            "value": {"alone_s": (report.get("compaction_alone") or {}).get("s"),
                      "concurrent_max_s": max((r.get("s", 0) for r in report.get("compactions_concurrent") or []
                                               if isinstance(r, dict)), default=None)},
            "pass": _compactions_parallel(report)},
        **({"freeze_phase_D7": {
            "value": freeze,
            "pass": isinstance(freeze, dict) and freeze.get("dumped_whole_tree") is True and freeze.get("dump_exit") == 0
                    and all(str(p).startswith("silent") for p in freeze.get("health_probes_while_frozen", []))
                    # With the supervisor e2e: it acted (dump before kill) and the frozen tree is gone.
                    and ("supervisor" not in freeze or (freeze.get("tree_gone") is True
                                                        and freeze["supervisor"].get("killed") is True))}}
           if report.get("freeze_requested") else {}),
    }


def _compactions_parallel(report: dict) -> bool:
    alone = (report.get("compaction_alone") or {}).get("s")
    concurrent = [r.get("s") for r in report.get("compactions_concurrent") or [] if isinstance(r, dict)]
    return bool(alone and len(concurrent) == 4 and max(concurrent) <= 1.5 * alone + 1.0)


def freeze_phase(root_pid: int, port: int, out: Path, repo: Path, node_dir: Path | None = None,
                 sealed_env: dict | None = None) -> dict:
    """The 09-07 signature on a real backend: suspend every process, then what the D7 supervisor sees.
    With *node_dir*, the desktop's liveness supervisor itself decides, dumps and kills it (vitest
    e2e, clock scaled 120 s -> 3 s); otherwise the harness only dumps the tree."""
    import ctypes

    import psutil
    tree = [root_pid, *(p.pid for p in psutil.Process(root_pid).children(recursive=True))]
    ntdll, kernel32 = ctypes.WinDLL("ntdll"), ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    for pid in tree:
        handle = kernel32.OpenProcess(0x0800, False, pid)
        ntdll.NtSuspendProcess(ctypes.c_void_p(handle))
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    probes = []
    for _ in range(3):
        started = time.monotonic()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5).read()
            probes.append("answered")
        except Exception as exc:  # noqa: BLE001
            probes.append(f"silent after {time.monotonic() - started:.1f}s ({type(exc).__name__})")
    if node_dir is not None:
        return {"tree": tree, "health_probes_while_frozen": probes,
                **_supervisor_e2e(root_pid, port, out, repo, node_dir, tree, sealed_env or {})}
    dump = subprocess.run([sys.executable, "-m", "hermes_cli.freeze_dump", str(root_pid), str(out / "freeze-dumps")],
                          cwd=repo, capture_output=True, text=True, timeout=180)
    files = [line for line in dump.stdout.splitlines() if line.strip()]
    return {"tree": tree, "health_probes_while_frozen": probes, "dump_exit": dump.returncode, "dumps": files,
            "dumped_whole_tree": len(files) == len(tree), "stderr": dump.stderr[-500:]}


def _supervisor_e2e(root_pid: int, port: int, out: Path, repo: Path, node_dir: Path, tree: list,
                    sealed_env: dict) -> dict:
    """Run the desktop's liveness supervisor (vitest e2e) against the frozen backend.

    Sealed like the backend (T1 r4, Codex): the SAME minimal environment (temp home, caches, empty
    managed scope, netguard on PYTHONPATH for the Python dump child), never the parent's credentials,
    NODE_OPTIONS or real user dirs; vitest is started with the pinned node.exe from the installed
    package (no npx resolution) and npm is forced offline. Absolute paths throughout: vitest runs with
    apps/desktop as its working directory."""
    import psutil
    out, repo = out.resolve(), repo.resolve()
    dump_dir, result_path = out / "freeze-dumps", out / "supervisor-e2e.json"
    target = {"pid": root_pid, "baseUrl": f"http://127.0.0.1:{port}", "python": sys.executable, "repo": str(repo),
              "dumpDir": str(dump_dir), "resultPath": str(result_path)}
    npm_cache = Path(sealed_env.get("TEMP") or out) / "npm-cache"
    env = {**sealed_env, "LIVENESS_E2E_TARGET": json.dumps(target),
           "PATH": os.pathsep.join((str(node_dir), sealed_env.get("PATH", ""))),
           "npm_config_offline": "true", "npm_config_cache": str(npm_cache)}
    node = str(node_dir / ("node.exe" if sys.platform == "win32" else "node"))
    vitest = repo / "node_modules" / "vitest" / "vitest.mjs"
    run = subprocess.run([node, str(vitest), "run", "--project", "electron", "electron/backend-liveness.e2e.test.ts"],
                         cwd=repo / "apps" / "desktop", env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=300)
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    dumps = sorted(p.name for p in dump_dir.glob("*.dmp")) if dump_dir.exists() else []
    return {"supervisor": result, "supervisor_exit": run.returncode, "dumps": dumps,
            "dumped_whole_tree": len(dumps) == len(tree),
            "tree_gone": not any(psutil.pid_exists(pid) for pid in tree),
            "dump_exit": 0 if run.returncode == 0 else run.returncode,
            "vitest_tail": ((run.stdout or "") + (run.stderr or ""))[-800:]}


if __name__ == "__main__":
    raise SystemExit(main())
