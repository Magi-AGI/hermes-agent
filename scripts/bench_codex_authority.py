"""Benchmark + fault matrix for the Codex credential recovery protocol (design rev 9 §13, stage 1).

Everything runs against a throwaway Hermes root and a LOCAL token endpoint (127.0.0.1): no network,
no live install, no ~/.codex. The endpoint is the out-of-process oracle: refresh tokens are
single-use, a reused one is rejected, and every presented token is recorded in its ledger.

  python scripts/bench_codex_authority.py progress   # 24 processes x 4 threads, real entry points
  python scripts/bench_codex_authority.py kills      # a real process kill at every boundary
  python scripts/bench_codex_authority.py latency --tree <checkout>   # read-path p50/p99
  python scripts/bench_codex_authority.py startup --baseline <checkout>  # A/B, bench-reports/STARTUP-AB-PROTOCOL.md
  python scripts/bench_codex_authority.py rescue-scan  # per-refresh rescue-location scan vs profile count

Progress drives all three refresh entry points concurrently: (P1) singleton refresh, (P2) the
quota-probe pre-refresh of an exhausted row whose token expired, (P3) the pool refresh. Kills: every
refresh boundary through each of P1/P2/P3, every step of an ownership op, and a kill DURING the
recovery (the dispatcher's roll-forward) of a decided op.

Invariants checked (progress): no token presented twice (no automatic replay), every chain's
ledger is ONE linear sequence (no store forked a chain), no chain fenced, every owner row holds its
chain's last issued pair (no row moved backward), the registry validates (RULE-G2), every
auth.json parses, refusals <= 1 % of attempts. Kills: the END STATE of design §13 per boundary.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[1]


# ── the local token endpoint (parent process) ───────────────────────────────────────────────

def fake_jwt(chain: str, exp_in: int = 3600) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    payload = {"exp": int(time.time()) + exp_in, "sub": f"user-{chain}", "jti": uuid.uuid4().hex,
               "https://api.openai.com/auth": {"chatgpt_account_id": f"acct-{chain}"}}
    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


class Ledger:
    def __init__(self, expired=()) -> None:
        self.expired = set(expired)  # chains whose access tokens are issued already expired (P2)
        self.lock = threading.Lock()
        self.valid: dict = {}        # refresh token -> chain (unconsumed)
        self.chain_of: dict = {}     # every refresh token ever issued -> chain
        self.last_issued: dict = {}  # chain -> (access, refresh)
        self.presented: list = []    # (t, token, chain, outcome)
        self.drop_response_for: set = set()

    def issue(self, chain: str):
        access = fake_jwt(chain, -120 if chain in self.expired else 3600)
        refresh = f"rt-{chain}-{uuid.uuid4().hex[:12]}"
        self.valid[refresh] = chain
        self.chain_of[refresh] = chain
        self.last_issued[chain] = (access, refresh)
        return access, refresh

    def post(self, token: str):
        with self.lock:
            chain = self.chain_of.get(token)
            if token not in self.valid:
                self.presented.append((time.time(), token, chain, "reused"))
                return 400, {"error": "refresh_token_reused"}
            del self.valid[token]
            access, refresh = self.issue(chain)
            self.presented.append((time.time(), token, chain, "ok"))
            return 200, {"access_token": access, "refresh_token": refresh}


def start_endpoint(ledger: Ledger):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
            status, payload = ledger.post((body.get("refresh_token") or [""])[0])
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_GET(self):  # noqa: N802
            with ledger.lock:
                counts: dict = {}
                for _t, _tok, chain, outcome in ledger.presented:
                    if outcome == "ok":
                        counts[chain] = counts.get(chain, 0) + 1
                stats = {"ok": counts, "reused": sum(1 for p in ledger.presented if p[3] == "reused")}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(stats).encode())

        def log_message(self, *_a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


# ── sandbox ─────────────────────────────────────────────────────────────────────────────────────

def sandbox_env(sandbox: Path, hermes_home: Path, url: str, tree: Path = REPO) -> dict:
    """Whitelist env (like run_tests_isolated): every home-ish variable points into the sandbox."""
    env = {k: os.environ[k] for k in ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
                                      "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE") if k in os.environ}
    userhome = sandbox / "userhome"
    env.update(HERMES_HOME=str(hermes_home), HOME=str(userhome), USERPROFILE=str(userhome),
               LOCALAPPDATA=str(sandbox / "localappdata"), APPDATA=str(sandbox / "appdata"),
               TEMP=str(sandbox / "tmp"), TMP=str(sandbox / "tmp"), CODEX_HOME=str(sandbox / "no-codex"),
               PYTHONPATH=str(tree), PYTHONHASHSEED="0", TZ="UTC", BENCH_TOKEN_URL=url,
               HERMES_CODEX_REFRESH_TIMEOUT_SECONDS="20")
    for d in ("userhome", "localappdata", "appdata", "tmp"):
        (sandbox / d).mkdir(parents=True, exist_ok=True)
    return env


def write_store(home: Path, singleton=None, pool=None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    store = {"version": 1, "providers": {}}
    if singleton:
        store["providers"]["openai-codex"] = {"tokens": {"access_token": singleton[0], "refresh_token": singleton[1]},
                                              "auth_mode": "chatgpt", "last_refresh": "2026-09-24T00:00:00Z"}
        store["active_provider"] = "openai-codex"
    if pool:
        store["credential_pool"] = {"openai-codex": pool}
    (home / "auth.json").write_text(json.dumps(store, indent=2), encoding="utf-8")


def make_profile(root: Path, name: str) -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    return home


# ── worker side ─────────────────────────────────────────────────────────────────────────────────

def _worker_setup():
    sys.path.insert(0, os.environ["PYTHONPATH"])
    from hermes_cli import auth_codex
    auth_codex.CODEX_OAUTH_TOKEN_URL = os.environ["BENCH_TOKEN_URL"] + "/token"


class _ProbeFailures:
    """The P2 probe pre-refresh and the P3 pool refresh log and swallow their failures (the pool
    returns None); this handler keeps the last swallowed exception per thread so the worker can
    classify the outcome precisely (fenced vs a lock timeout vs anything else)."""

    def __init__(self) -> None:
        import logging
        self.last: dict = {}

        class Handler(logging.Handler):
            def emit(inner, record):  # noqa: N805
                message = str(record.msg)
                if record.exc_info and message.startswith("Codex pre-probe token refresh failed"):
                    self.last[record.thread] = record.exc_info[1]
                elif message.startswith(("Codex pool entry %s refresh failed", "Codex pool entry %s cannot refresh")) \
                        and record.args and isinstance(record.args[-1], BaseException):
                    self.last[record.thread] = record.args[-1]

        handler = Handler(level=logging.DEBUG)
        for name in ("hermes_cli.auth", "agent.credential_pool"):
            log = logging.getLogger(name)
            log.addHandler(handler)
            log.setLevel(logging.DEBUG)

    def take(self):
        return self.last.pop(threading.get_ident(), None)


def run_entry(kind: str, target: str, probe_failures=None):
    """One refresh through a REAL entry point: ``singleton`` (P1), ``pool`` (P3), ``probe`` (P2).
    Returns ``(outcome, error)``: ok / refused / fenced / <error name>."""
    import hermes_cli.auth as auth
    if kind == "singleton":
        auth.resolve_codex_runtime_credentials(force_refresh=True)
        return "ok", None
    if kind == "pool":
        from agent.credential_pool import load_pool
        if load_pool("openai-codex").try_refresh_matching(credential_id=target) is None:
            exc = probe_failures.take() if probe_failures else None
            if exc is not None:
                raise exc  # classified by the caller like a direct failure
            return "fenced", None
        return "ok", None
    from hermes_cli import auth_codex
    from hermes_cli.auth_codex_rows import get_row
    row = get_row(auth._load_auth_store(), f"pool:{target}") or {}
    fresh = auth_codex._refresh_expired_codex_probe_token(
        row.get("access_token"), row.get("refresh_token"), row_store=auth._auth_file_path(),
        row=f"pool:{target}", min_interval_seconds=0)
    if fresh is not None:
        return "ok", None
    exc = probe_failures.take() if probe_failures else None
    if exc is None:
        return "ok", None  # nothing to do: a peer already refreshed the expired token
    raise exc


def worker_progress(args) -> None:
    _worker_setup()
    import hermes_cli.auth as auth

    probe_failures = _ProbeFailures()
    targets = json.loads(args.targets)
    stats = {"attempts": 0, "ok": 0, "refused": 0, "fenced": 0, "lock_timeout": 0, "errors": {}, "lat": []}
    lock = threading.Lock()
    deadline = time.monotonic() + args.seconds

    def done() -> bool:
        with urlopen(os.environ["BENCH_TOKEN_URL"] + "/stats", timeout=5) as r:
            s = json.loads(r.read())
        counts = s["ok"]
        return sum(counts.values()) >= args.n and all(counts.get(c, 0) >= args.per_chain for c in args.chains.split(","))

    def run(thread_no: int) -> None:
        rng = random.Random(os.getpid() * 31 + thread_no)
        while time.monotonic() < deadline:
            if rng.random() < 0.1 and done():
                return
            kind, target = targets[rng.randrange(len(targets))]
            t0 = time.perf_counter()
            outcome = "ok"
            try:
                outcome, _ = run_entry(kind, target, probe_failures)
            except auth.AuthError as exc:
                outcome = "refused" if getattr(exc, "reason", None) == "in-progress" else (
                    "fenced" if exc.code in ("codex_recovery_fenced", "codex_refresh_indeterminate") else exc.code)
            except TimeoutError:
                outcome = "lock_timeout"  # the store lock stayed contended past its budget
            except Exception as exc:  # pragma: no cover - reported
                outcome = type(exc).__name__
                import traceback
                with lock:
                    stats.setdefault("tracebacks", []).append(traceback.format_exc()[-3000:])
            with lock:
                stats["attempts"] += 1
                stats["lat"].append(time.perf_counter() - t0)
                if outcome in ("ok", "refused", "fenced", "lock_timeout"):
                    stats[outcome] += 1
                else:
                    stats["errors"][outcome] = stats["errors"].get(outcome, 0) + 1
            time.sleep(rng.uniform(0, 0.02))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(args.threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(json.dumps(stats))


def _install_kill(boundary: str) -> None:
    """os._exit at *boundary*: no finally, no handler, no flush — a real process death."""
    from hermes_cli import auth as auth_mod
    from hermes_cli import auth_codex_ops as ops
    from hermes_cli import auth_codex_refresh_txn as txn

    def die():
        os._exit(137)

    real_write = txn._write_attempt

    def write_attempt(owner, record, attempt, **kw):
        if boundary == "before-completion" and attempt is None and not kw and \
                (record.get("history") or [{}])[-1].get("outcome") == "refreshed":
            die()
        out = real_write(owner, record, attempt, **kw)
        if attempt and attempt.get("state") == {"after-intent": "intent", "after-pending": "pending"}.get(boundary):
            die()
        return out

    txn._write_attempt = write_attempt
    if boundary == "after-response":
        from hermes_cli import auth_codex
        for module in (auth_mod, auth_codex):  # P1/P3 call auth's seam, P2 auth_codex's own
            real = module.refresh_codex_oauth_pure

            def refresh(*a, _real=real, **k):
                _real(*a, **k)
                die()
            module.refresh_codex_oauth_pure = refresh
    op_hooks = {"op-payload": "write_registry", "op-decision": "apply_payload",
                "op-apply": "_apply_record_action", "op-commit": "commit_op",
                "recovery-apply": "_apply_record_action", "recovery-commit": "commit_op"}
    if boundary in op_hooks:
        setattr(ops, op_hooks[boundary], lambda *a, **k: die())


def worker_kill(args) -> None:
    _worker_setup()
    import hermes_cli.auth as auth
    _install_kill(args.boundary)
    if args.boundary.startswith("recovery-"):
        from hermes_cli.auth_codex_authority import startup_dispatch
        startup_dispatch()  # the dispatcher rolls the decided op forward and dies midway
    elif args.boundary.startswith("op-"):
        pair = json.loads(args.pair)
        auth._save_codex_tokens({"access_token": pair[0], "refresh_token": pair[1]})
    else:
        run_entry(args.path, "poolaa")
    print("SURVIVED")  # the kill point was never reached


def worker_check(args) -> None:
    _worker_setup()
    import hermes_cli.auth as auth
    from hermes_cli.auth_codex_authority import startup_dispatch
    out = {}
    if args.job:
        out["dispatch"] = startup_dispatch()
    try:
        if args.path == "singleton" and not args.force:
            out["key"] = auth.resolve_codex_runtime_credentials()["api_key"]
        else:
            out["outcome"] = run_entry(args.path, "poolaa", _ProbeFailures())[0]
    except auth.AuthError as exc:
        out["error"] = exc.code
    print(json.dumps(out))


def worker_latency(args) -> None:
    sys.path.insert(0, args.tree)
    import hermes_cli.auth as auth
    from agent.credential_pool import load_pool
    auth.resolve_codex_runtime_credentials()  # warm
    lat_resolve, lat_pool = [], []
    for _ in range(args.iterations):
        t0 = time.perf_counter()
        auth.resolve_codex_runtime_credentials()
        lat_resolve.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        load_pool("openai-codex").select()
        lat_pool.append(time.perf_counter() - t0)
    print(json.dumps({"resolve": lat_resolve, "pool": lat_pool}))


# ── parent side ─────────────────────────────────────────────────────────────────────────────────

def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))] if values else None


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows_of(store: dict):
    rows = {}
    state = (store.get("providers") or {}).get("openai-codex")
    if isinstance(state, dict) and isinstance(state.get("tokens"), dict):
        rows["singleton"] = (state["tokens"].get("access_token"), state["tokens"].get("refresh_token"))
    for e in (store.get("credential_pool") or {}).get("openai-codex") or ():
        rows[f"pool:{e.get('id')}"] = (e.get("access_token"), e.get("refresh_token"))
    return rows


def run_progress(args) -> int:
    ledger = Ledger()
    server, url = start_endpoint(ledger)
    sandbox = Path(tempfile.mkdtemp(prefix="bench-codex-"))
    root = sandbox / "hroot"
    chains = ["root", "poolA", "poolB", "probeC", "work1", "work2"]
    ledger.expired.add("probeC")
    s_root, s_a, s_b = ledger.issue("root"), ledger.issue("poolA"), ledger.issue("poolB")
    s_c = ledger.issue("probeC")
    write_store(root, singleton=s_root, pool=[
        {"id": "poolaa", "label": "A", "auth_type": "oauth", "priority": 1, "source": "manual:device_code",
         "access_token": s_a[0], "refresh_token": s_a[1]},
        {"id": "poolbb", "label": "B", "auth_type": "oauth", "priority": 2, "source": "manual:device_code",
         "access_token": s_b[0], "refresh_token": s_b[1]},
        {"id": "probecc", "label": "C", "auth_type": "oauth", "priority": 3, "source": "manual:device_code",
         "access_token": s_c[0], "refresh_token": s_c[1], "last_status": "exhausted",
         "last_error_code": 429, "last_error_reason": "usage_limit_reached"}])
    homes = {"root": root}
    for name in ("work1", "work2"):
        homes[name] = make_profile(root, name)
        write_store(homes[name], singleton=ledger.issue(name))
    homes["borrower"] = make_profile(root, "borrower")
    write_store(homes["borrower"])
    plans = {  # (HERMES_HOME, targets)
        "root": [["singleton", "root"]], "pools": [["pool", "poolaa"], ["pool", "poolbb"]],
        "probe": [["probe", "probecc"], ["pool", "probecc"]],
        "work1": [["singleton", "work1"]], "work2": [["singleton", "work2"]],
        "borrower": [["singleton", "root"]],
    }
    order = ["root", "pools", "work1", "probe", "work2", "borrower", "pools", "probe"]
    procs = []
    t0 = time.monotonic()
    for i in range(args.processes):
        plan = order[i % len(order)]
        home = homes["root"] if plan in ("pools", "root", "probe") else homes[plan]
        env = sandbox_env(sandbox, home, url)
        procs.append(subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker-progress", "--targets", json.dumps(plans[plan]),
             "--threads", str(args.threads), "--seconds", str(args.seconds), "--n", str(args.n),
             "--per-chain", str(args.per_chain), "--chains", ",".join(chains)],
            env=env, cwd=str(sandbox), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    results = []
    for p in procs:
        out, err = p.communicate(timeout=args.seconds + 300)
        try:
            results.append(json.loads(out.strip().splitlines()[-1]))
        except Exception:
            results.append({"crash": (err or out)[-2000:]})
    wall = time.monotonic() - t0
    server.shutdown()

    report = {"wall_s": round(wall, 1), "processes": args.processes, "threads": args.threads}
    ok_by_chain: dict = {}
    for _t, token, chain, outcome in ledger.presented:
        if outcome == "ok":
            ok_by_chain[chain] = ok_by_chain.get(chain, 0) + 1
    report["refreshes"] = ok_by_chain
    report["refresh_total"] = sum(ok_by_chain.values())
    report["replays_presented"] = sum(1 for p in ledger.presented if p[3] == "reused")
    attempts = sum(r.get("attempts", 0) for r in results)
    refused = sum(r.get("refused", 0) for r in results)
    report["attempts"], report["refused"] = attempts, refused
    report["refusal_rate"] = round(refused / attempts, 4) if attempts else None
    report["fenced_outcomes"] = sum(r.get("fenced", 0) for r in results)
    report["lock_timeouts"] = sum(r.get("lock_timeout", 0) for r in results)
    report["worker_errors"] = [r.get("errors") for r in results if r.get("errors")]
    report["worker_crashes"] = [r["crash"] for r in results if "crash" in r]
    report["error_tracebacks"] = [t for r in results for t in r.get("tracebacks", [])][:5]
    lat = [x for r in results for x in r.get("lat", [])]
    report["entry_latency_ms"] = {"p50": round(_pct(lat, .5) * 1000, 1), "p99": round(_pct(lat, .99) * 1000, 1)} if lat else None

    # Linear lineage per chain: each presented token is the previous response's refresh token.
    failures = []
    presented_ok = [p for p in ledger.presented if p[3] == "ok"]
    seen_tokens = [p[1] for p in ledger.presented]
    if len(seen_tokens) != len(set(seen_tokens)):
        failures.append("a refresh token was presented twice")
    registry = _load(root / "auth.chains.json")
    fenced = [c for c in registry["chains"].values() if c["state"] != "active"]
    if fenced:
        failures.append(f"{len(fenced)} chain(s) fenced: {[c.get('fence') for c in fenced]}")
    rows_by_store = {}
    for home in {root, homes["work1"], homes["work2"], homes["borrower"]}:
        try:
            rows_by_store[str(home)] = _rows_of(_load(home / "auth.json"))
        except Exception as exc:
            failures.append(f"{home}/auth.json does not parse: {exc}")
    for cid, chain in registry["chains"].items():
        owner_rows = [r for r in chain["rows"] if os.path.normcase(r["store"]) == os.path.normcase(chain["owner"])]
        store_dir = str(Path(chain["owner"]).parent)
        match = next((k for k in rows_by_store if os.path.normcase(k) == os.path.normcase(store_dir)), None)
        held = {rows_by_store[match][r["row"]] for r in owner_rows} if match else set()
        chain_name = next((ledger.chain_of.get(v[1]) for v in held), None)
        if chain_name and held != {ledger.last_issued[chain_name]}:
            failures.append(f"chain {chain_name}: rows {held} are not the last issued pair (moved backward?)")
    del presented_ok
    seen_pairs = set()
    for c in registry["chains"].values():
        for r in c["rows"]:
            key = (r["store"], r["row"])
            if key in seen_pairs:
                failures.append(f"row {key} in two chains")
            seen_pairs.add(key)
    if report["replays_presented"]:
        failures.append(f"{report['replays_presented']} replayed refresh token(s)")
    if report["refresh_total"] < args.n or any(ok_by_chain.get(c, 0) < args.per_chain for c in chains):
        failures.append("progress target not reached")
    if report["refusal_rate"] is not None and report["refusal_rate"] > 0.01:
        failures.append(f"refusal rate {report['refusal_rate']} > 1%")
    if report["fenced_outcomes"] or report["worker_errors"] or report["worker_crashes"]:
        failures.append("fences / errors / crashes in workers")
    report["failures"] = failures
    print(json.dumps(report, indent=2))
    if not args.keep:
        shutil.rmtree(sandbox, ignore_errors=True)
    return 1 if failures else 0


KILL_EXPECT = {
    # boundary: (fence reason expected after the next refresh through the same path, posts in it)
    "after-intent": ("indeterminate", 0),
    "after-response": ("indeterminate", 0),
    "after-pending": (None, 0),
    "before-completion": (None, 1),
}
OP_BOUNDARIES = ["op-payload", "op-decision", "op-apply", "op-commit"]
RECOVERY_BOUNDARIES = ["recovery-apply", "recovery-commit"]


def _kill_sandbox(path: str):
    ledger = Ledger(expired={"c"} if path == "probe" else ())
    server, url = start_endpoint(ledger)
    sandbox = Path(tempfile.mkdtemp(prefix="bench-kill-"))
    root = sandbox / "hroot"
    first = ledger.issue("c")
    if path == "singleton":
        write_store(root, singleton=first)
    else:
        write_store(root, pool=[{"id": "poolaa", "label": "A", "auth_type": "oauth", "priority": 0,
                                 "source": "manual:device_code", "access_token": first[0],
                                 "refresh_token": first[1], "last_status": "exhausted",
                                 "last_error_code": 429, "last_error_reason": "usage_limit_reached"}])
    return ledger, server, url, sandbox, root


def _chain_c(root: Path):
    reg = _load(root / "auth.chains.json")
    for chain in reg["chains"].values():
        if chain["state"] != "retired":
            return reg, chain
    return reg, None


def run_kills(args) -> int:
    failures, report = [], {}
    me = str(Path(__file__).resolve())
    cases = [(p, b) for p in ("singleton", "pool", "probe") for b in KILL_EXPECT]
    cases += [("singleton", b) for b in OP_BOUNDARIES + RECOVERY_BOUNDARIES]
    for path, boundary in cases:
        name = f"{path}:{boundary}"
        ledger, server, url, sandbox, root = _kill_sandbox(path)
        env = sandbox_env(sandbox, root, url)

        def run(*argv, timeout=120):
            return subprocess.run([sys.executable, me, *argv], env=env, cwd=str(sandbox),
                                  capture_output=True, text=True, timeout=timeout)

        run("worker-check", "--job")  # bootstrap (cold-start dispatch)
        new = ledger.issue("n")
        first_kill = "op-decision" if boundary in RECOVERY_BOUNDARIES else boundary
        kill = run("worker-kill", "--boundary", first_kill, "--path", path, "--pair", json.dumps(new))
        kills = [kill.returncode]
        if boundary in RECOVERY_BOUNDARIES:  # the recovery of that decided op dies midway too
            kills.append(run("worker-kill", "--boundary", boundary, "--path", path).returncode)
        posts_before = len(ledger.presented)
        job = boundary.startswith(("op-", "recovery-"))
        check = run("worker-check", "--path", path, "--force", *(["--job"] if job else []))
        server.shutdown()
        try:
            outcome = json.loads(check.stdout.strip().splitlines()[-1])
        except Exception:
            outcome = {"crash": check.stderr[-1500:]}
        posts = ledger.presented[posts_before:]
        entry = {"kill_exits": kills, "check": outcome, "posts_after": [p[3] for p in posts],
                 "replays": sum(1 for p in ledger.presented if p[3] == "reused")}
        if any(code != 137 for code in kills):
            failures.append(f"{name}: a kill point was not reached ({kill.stdout[-200:]} {kill.stderr[-500:]})")
        if entry["replays"]:
            failures.append(f"{name}: a refresh token was replayed")
        reg, chain = _chain_c(root)
        rows = _rows_of(_load(root / "auth.json"))
        if boundary in KILL_EXPECT:
            want_fence, want_posts = KILL_EXPECT[boundary]
            fence = (chain or {}).get("fence") or {}
            got_fence = fence.get("reason") if chain and chain["state"] == "fenced" else None
            held = {rows[r["row"]] for r in (chain or {}).get("rows", ()) if r["row"] in rows}
            entry.update(fence=got_fence, rows_hold_last_issued=held == {ledger.last_issued["c"]})
            if got_fence != want_fence or len(posts) != want_posts:
                failures.append(f"{name}: expected fence={want_fence} posts={want_posts}, got {entry}")
            if want_fence is None and held != {ledger.last_issued["c"]}:
                failures.append(f"{name}: rows do not hold the chain's last issued pair (moved backward?)")
        else:
            decided = boundary != "op-payload"
            tokens = _load(root / "auth.json")["providers"]["openai-codex"]["tokens"]
            lineage = ledger.chain_of.get(tokens["refresh_token"])
            if reg["ops"] or (decided and lineage != "n") or (not decided and lineage != "c"):
                failures.append(f"{name}: end state wrong (ops={list(reg['ops'])}, lineage={lineage})")
            entry["lineage_after"] = lineage
        report[name] = entry
        if not args.keep:
            shutil.rmtree(sandbox, ignore_errors=True)
    report["cases"] = len(cases)
    report["failures"] = failures
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


def run_latency(args) -> int:
    tree = Path(args.tree).resolve()
    ledger = Ledger()
    sandbox = Path(tempfile.mkdtemp(prefix="bench-lat-"))
    root = sandbox / "hroot"
    s = ledger.issue("root")
    write_store(root, singleton=s, pool=[{"id": "devcod", "label": "d", "auth_type": "oauth", "priority": 0,
                                          "source": "device_code", "access_token": s[0], "refresh_token": s[1]}])
    env = sandbox_env(sandbox, root, "http://127.0.0.1:9", tree)
    if (tree / "hermes_cli" / "auth_codex_authority.py").exists():
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker-check", "--job"], env=env,
                       cwd=str(sandbox), capture_output=True, text=True, timeout=120)
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker-latency", "--tree", str(tree),
                           "--iterations", str(args.iterations)], env=env, cwd=str(sandbox),
                          capture_output=True, text=True, timeout=900)
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    out = {k: {"p50_ms": round(_pct(v, .5) * 1000, 3), "p99_ms": round(_pct(v, .99) * 1000, 3),
               "mean_ms": round(statistics.mean(v) * 1000, 3)} for k, v in data.items()}
    print(json.dumps({"tree": str(tree), **out}, indent=2))
    shutil.rmtree(sandbox, ignore_errors=True)
    return 0


def worker_startup(args) -> None:
    """One process starting up the way a runtime CLI / gateway does: the authority dispatch (when
    the tree has it), then credential resolution (resume) and a pool selection. Times in ms."""
    t0 = time.perf_counter()
    sys.path.insert(0, args.tree)
    import hermes_cli.auth as auth
    from agent.credential_pool import load_pool
    t_import = time.perf_counter()
    dispatch = None
    try:
        from hermes_cli.auth_codex_authority import startup_dispatch
    except ImportError:
        startup_dispatch = None
    if startup_dispatch is not None:
        dispatch = startup_dispatch()
    t_dispatch = time.perf_counter()
    error = None
    try:
        auth.resolve_codex_runtime_credentials()
        load_pool("openai-codex").select()
    except Exception as exc:  # reported, never hidden
        error = f"{type(exc).__name__}: {exc}"[:300]
    t_done = time.perf_counter()
    print(json.dumps({"import_ms": (t_import - t0) * 1000, "dispatch_ms": (t_dispatch - t_import) * 1000,
                      "resume_ms": (t_done - t_dispatch) * 1000, "auth_ms": (t_done - t_import) * 1000,
                      "dispatch": dispatch, "error": error}))


def _startup_wave(tree: Path, sandbox: Path, homes: dict, url: str, n: int) -> dict:
    """N processes started at once (a fleet restart); every per-process sample is kept."""
    me = str(Path(__file__).resolve())
    names = list(homes)
    procs = []
    for i in range(n):  # all started before any is waited for: they contend like a fleet restart
        env = sandbox_env(sandbox, homes[names[i % len(names)]], url, tree)
        procs.append((time.perf_counter(), subprocess.Popen(
            [sys.executable, me, "worker-startup", "--tree", str(tree)], env=env, cwd=str(sandbox),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
    samples, errors = [], []
    for started, proc in procs:
        out, err = proc.communicate(timeout=600)
        wall = (time.perf_counter() - started) * 1000
        try:
            data = json.loads(out.strip().splitlines()[-1])
            data["process_wall_ms"] = wall
            samples.append(data)
            if data.get("error"):
                errors.append(data["error"])
        except Exception:
            errors.append((err or out)[-300:])
    return {"samples": samples, "errors": errors}


def _startup_sandbox():
    ledger = Ledger()
    sandbox = Path(tempfile.mkdtemp(prefix="bench-start-"))
    root = sandbox / "hroot"
    s = ledger.issue("root")
    write_store(root, singleton=s, pool=[{"id": "devcod", "label": "d", "auth_type": "oauth", "priority": 0,
                                          "source": "device_code", "access_token": s[0], "refresh_token": s[1]}])
    homes = {"root": root}
    for name in ("work1", "work2"):
        homes[name] = make_profile(root, name)
        write_store(homes[name], singleton=ledger.issue(name))
    return sandbox, homes


def _stats(values):
    if not values:
        return None
    return {"n": len(values), "p50": round(_pct(values, .5), 1), "p95": round(_pct(values, .95), 1),
            "p99": round(_pct(values, .99), 1), "max": round(max(values), 1)}


def run_startup(args) -> int:
    """The A/B protocol of bench-reports/STARTUP-AB-PROTOCOL.md: one discarded warm-up round per
    tree, then ``--runs`` measured runs, alternating which tree goes first; each run is a fresh
    sandbox per tree with a COLD wave (no registry: one process bootstraps while the rest start)
    then a WARM wave (registry present). Every per-process sample is kept; the gate is on the
    POOLED p99 of ``auth_ms`` (dispatch + resume), branch <= baseline + ``--gate-ms``, cold and warm."""
    trees = {"branch": REPO, "baseline": Path(args.baseline).resolve()}
    url = "http://127.0.0.1:9"
    runs = []
    for index in range(-1, args.runs):  # -1: the discarded warm-up round
        order = ["branch", "baseline"] if index % 2 == 0 else ["baseline", "branch"]
        run = {"index": index, "order": order, "warmup": index < 0}
        for label in order:
            sandbox, homes = _startup_sandbox()
            run[label] = {"cold": _startup_wave(trees[label], sandbox, homes, url, args.processes),
                          "warm": _startup_wave(trees[label], sandbox, homes, url, args.processes)}
            shutil.rmtree(sandbox, ignore_errors=True)
        runs.append(run)
    measured = [r for r in runs if not r["warmup"]]
    summary, failures = {}, []
    for label in trees:
        summary[label] = {}
        for wave in ("cold", "warm"):
            pooled = [x for r in measured for x in r[label][wave]["samples"]]
            summary[label][wave] = {
                metric: _stats([x[metric] for x in pooled])
                for metric in ("auth_ms", "dispatch_ms", "resume_ms", "process_wall_ms")}
            summary[label][wave]["per_run_p99_auth_ms"] = [
                round(_pct([x["auth_ms"] for x in r[label][wave]["samples"]], .99), 1) for r in measured]
            summary[label][wave]["errors"] = [e for r in measured for e in r[label][wave]["errors"]][:5]
    for wave in ("cold", "warm"):
        branch, baseline = summary["branch"][wave]["auth_ms"]["p99"], summary["baseline"][wave]["auth_ms"]["p99"]
        summary[f"gate_{wave}"] = {"branch_p99": branch, "baseline_p99": baseline, "limit": baseline + args.gate_ms,
                                   "pass": branch <= baseline + args.gate_ms}
        if not summary[f"gate_{wave}"]["pass"]:
            failures.append(f"{wave}: pooled p99 {branch} > baseline {baseline} + {args.gate_ms}")
        if summary["branch"][wave]["errors"]:
            failures.append(f"{wave}: branch worker errors")
    out = {"protocol": "bench-reports/STARTUP-AB-PROTOCOL.md", "processes": args.processes, "runs": args.runs,
           "gate_ms": args.gate_ms, "summary": summary, "failures": failures, "raw_runs": runs}
    print(json.dumps(out, indent=1))
    return 1 if failures else 0


def worker_rescue_scan(args) -> None:
    """The per-refresh rescue-location scan (``unrecorded_legacy_rescues``) with N named profiles."""
    import statistics as st
    from hermes_cli.auth_codex_authority import authority_dispatch
    from hermes_cli.auth_codex_registry import read_registry, unrecorded_legacy_rescues
    authority_dispatch()
    view = read_registry()
    lat = []
    for _ in range(args.iterations):
        t0 = time.perf_counter()
        unrecorded_legacy_rescues(view)
        lat.append((time.perf_counter() - t0) * 1000)
    print(json.dumps({"profiles": args.profiles, "median_ms": round(st.median(lat), 3),
                      "p99_ms": round(_pct(lat, .99), 3)}))


def run_rescue_scan(args) -> int:
    me = str(Path(__file__).resolve())
    results = []
    for n in (0, 10, 50, 200):
        ledger = Ledger()
        sandbox = Path(tempfile.mkdtemp(prefix="bench-rescue-"))
        root = sandbox / "hroot"
        write_store(root, singleton=ledger.issue("root"))
        for i in range(n):
            make_profile(root, f"p{i:03d}")
        env = sandbox_env(sandbox, root, "http://127.0.0.1:9")
        proc = subprocess.run([sys.executable, me, "worker-rescue-scan", "--profiles", str(n),
                               "--iterations", str(args.iterations)], env=env, cwd=str(sandbox),
                              capture_output=True, text=True, timeout=600)
        results.append(json.loads(proc.stdout.strip().splitlines()[-1]))
        shutil.rmtree(sandbox, ignore_errors=True)
    print(json.dumps({"rescue_scan": results}, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("progress")
    p.add_argument("--processes", type=int, default=24)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--per-chain", type=int, default=40)
    p.add_argument("--seconds", type=int, default=600)
    p.add_argument("--keep", action="store_true")
    k = sub.add_parser("kills")
    k.add_argument("--keep", action="store_true")
    st = sub.add_parser("startup")
    st.add_argument("--baseline", required=True)
    st.add_argument("--processes", type=int, default=24)
    st.add_argument("--runs", type=int, default=10)
    st.add_argument("--gate-ms", type=float, default=20.0)
    rs = sub.add_parser("rescue-scan")
    rs.add_argument("--iterations", type=int, default=200)
    wr = sub.add_parser("worker-rescue-scan")
    wr.add_argument("--profiles", type=int)
    wr.add_argument("--iterations", type=int)
    ws = sub.add_parser("worker-startup")
    ws.add_argument("--tree")
    lt = sub.add_parser("latency")
    lt.add_argument("--tree", default=str(REPO))
    lt.add_argument("--iterations", type=int, default=500)
    w = sub.add_parser("worker-progress")
    w.add_argument("--targets")
    w.add_argument("--threads", type=int)
    w.add_argument("--seconds", type=int)
    w.add_argument("--n", type=int)
    w.add_argument("--per-chain", type=int)
    w.add_argument("--chains")
    wk = sub.add_parser("worker-kill")
    wk.add_argument("--boundary")
    wk.add_argument("--pair")
    wk.add_argument("--path", default="singleton")
    wc = sub.add_parser("worker-check")
    wc.add_argument("--force", action="store_true")
    wc.add_argument("--job", action="store_true")
    wc.add_argument("--path", default="singleton")
    wl = sub.add_parser("worker-latency")
    wl.add_argument("--tree")
    wl.add_argument("--iterations", type=int)
    args = ap.parse_args()
    handlers = {"progress": run_progress, "kills": run_kills, "latency": run_latency, "startup": run_startup, "rescue-scan": run_rescue_scan,
                "worker-rescue-scan": worker_rescue_scan,
                "worker-progress": worker_progress, "worker-kill": worker_kill, "worker-check": worker_check,
                "worker-latency": worker_latency, "worker-startup": worker_startup}
    return handlers[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
