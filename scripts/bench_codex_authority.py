"""Benchmark + fault matrix for the Codex credential recovery protocol (design rev 9 §13, stage 1).

Everything runs against a throwaway Hermes root and a LOCAL token endpoint (127.0.0.1): no network,
no live install, no ~/.codex. The endpoint is the out-of-process oracle: refresh tokens are
single-use, a reused one is rejected, and every presented token is recorded in its ledger.

  python scripts/bench_codex_authority.py progress   # 24 processes x 4 threads, real entry points
  python scripts/bench_codex_authority.py kills      # a real process kill at every boundary
  python scripts/bench_codex_authority.py kills2     # stage 2: kills at every rename / abandon / transfer / delete step
                                                     # (incl. the rename's profile-state finalization)
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
            if self.path.startswith("/issue"):  # a fresh sign-in's pair for a worker (stage 2 races)
                chain = (parse_qs(self.path.partition("?")[2]).get("chain") or ["x"])[0]
                with ledger.lock:
                    pair = ledger.issue(chain)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(pair).encode())
                return
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
                elif message.startswith(("Codex pool entry %s refresh failed", "Codex pool entry %s cannot refresh",
                                         "Codex pool entry %s: %s")) \
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
        result = load_pool("openai-codex").try_refresh_matching(credential_id=target)
        exc = probe_failures.take() if probe_failures else None
        if exc is not None:
            raise exc  # a failure / busy the pool swallowed: classified like a direct one
        return ("fenced" if result is None else "ok"), None
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
    stats = {"attempts": 0, "ok": 0, "refused": 0, "fenced": 0, "lock_timeout": 0, "moved": 0, "errors": {},
             "lat": [], "fence_reasons": {}}
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
            outcome, fence_reason = "ok", None
            try:
                outcome, _ = run_entry(kind, target, probe_failures)
            except auth.AuthError as exc:
                from hermes_cli.auth_codex_registry import CodexStoreLockBusy
                fence_reason = str(getattr(exc, "reason", None) or exc.code)
                if isinstance(exc, CodexStoreLockBusy):
                    outcome = "lock_timeout"  # local store-lock contention: retryable busy
                elif exc.code in ("profile_store_missing", "profile_being_moved"):
                    outcome = "moved"  # this worker's profile is being / was renamed away (profile-ops race)
                else:
                    outcome = "refused" if getattr(exc, "reason", None) == "in-progress" else (
                        "fenced" if exc.code in ("codex_recovery_fenced", "codex_refresh_indeterminate")
                        else exc.code)
            except TimeoutError:
                outcome = "lock_timeout"  # the store lock stayed contended past its budget
            except Exception as exc:  # pragma: no cover - reported
                outcome = type(exc).__name__
                import traceback
                with lock:
                    stats.setdefault("tracebacks", []).append(traceback.format_exc()[-3000:])
            if outcome not in ("ok", "moved") and args.moving and not Path(os.environ["HERMES_HOME"]).is_dir():
                outcome = "moved"  # the profile was renamed away while this attempt ran
            with lock:
                stats["attempts"] += 1
                stats["lat"].append(time.perf_counter() - t0)
                if outcome == "fenced":
                    stats["fence_reasons"][fence_reason] = stats["fence_reasons"].get(fence_reason, 0) + 1
                if outcome in ("ok", "refused", "fenced", "lock_timeout", "moved"):
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


def worker_mover(args) -> None:
    """Stage-2 profile ops racing the refresh workers: rename ``mover`` <-> ``moverb`` (a profile
    holding its own Codex chain) and create / sign in / delete scratch profiles, until the progress
    target is reached."""
    _worker_setup()
    _quiet_profile_commands()
    import hermes_cli.auth as auth
    from hermes_cli import profiles
    root = Path(os.environ["HERMES_HOME"])
    stats: dict = {"renames": 0, "rename_refused": 0, "rename_errors": {}, "deletes": 0, "sign_ins": 0,
                   "scratch_errors": {}, "rename_ms": []}
    deadline = time.monotonic() + args.seconds

    def done() -> bool:
        with urlopen(os.environ["BENCH_TOKEN_URL"] + "/stats", timeout=5) as r:
            counts = json.loads(r.read())["ok"]
        return sum(counts.values()) >= args.n and all(counts.get(c, 0) >= args.per_chain for c in args.chains.split(","))

    if args.stick:  # the first rename's move fails as if a handle held the directory: a stuck op
        real = profiles._rename_checked_profile

        def failing_once(old_canon, new_canon, old_dir, new_dir, move):
            profiles._rename_checked_profile = real

            def held():
                raise PermissionError(13, "held open (bench --stick)")
            return real(old_canon, new_canon, old_dir, new_dir, lambda fn: move(held))
        profiles._rename_checked_profile = failing_once
    # The profile commands print progress lines; the parent reads this pipe only at the end, so a
    # full pipe would block the mover mid-operation (measured: a delete "stalled" 279 s in a print).
    real_stdout, sys.stdout = sys.stdout, open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    i = 0
    while time.monotonic() < deadline and not done():
        i += 1
        current = "mover" if (root / "profiles" / "mover").is_dir() else "moverb"
        target = "moverb" if current == "mover" else "mover"
        t0 = time.perf_counter()
        try:
            profiles.rename_profile(current, target)
            stats["renames"] += 1
            stats["rename_ms"].append(round((time.perf_counter() - t0) * 1000, 1))
        except (ValueError, auth.AuthError, TimeoutError) as exc:
            key = type(exc).__name__
            stats["rename_refused"] += 1
            stats["rename_errors"][key] = stats["rename_errors"].get(key, 0) + 1
            messages = stats.setdefault("rename_error_samples", [])
            if len(messages) < 5 and str(exc)[:300] not in messages:
                messages.append(str(exc)[:300])
        if i % 4:
            time.sleep(0.05)
            continue  # a scratch sign-in + delete every 4th round; renames every round
        name = f"scratch{i % 3}"
        t0 = time.perf_counter()
        steps = {}
        try:
            profiles.create_profile(name, no_alias=True, no_skills=True)
            steps["create"] = time.perf_counter()
            with urlopen(os.environ["BENCH_TOKEN_URL"] + "/issue?chain=scratch", timeout=5) as r:
                pair = json.loads(r.read())
            os.environ["HERMES_HOME"] = str(root / "profiles" / name)
            try:
                auth._save_codex_tokens({"access_token": pair[0], "refresh_token": pair[1]})
                stats["sign_ins"] += 1
            finally:
                os.environ["HERMES_HOME"] = str(root)
            steps["sign_in"] = time.perf_counter()
            profiles.delete_profile(name, yes=True)
            steps["delete"] = time.perf_counter()
            stats["deletes"] += 1
            stats.setdefault("scratch_cycle_ms", []).append(round((time.perf_counter() - t0) * 1000, 1))
            if time.perf_counter() - t0 > 5:
                prev, split = t0, {}
                for step, at in steps.items():
                    split[step] = round((at - prev) * 1000, 1)
                    prev = at
                stats.setdefault("slow_cycles", []).append(split)
        except Exception as exc:
            key = f"{type(exc).__name__}: {str(exc)[:120]}"
            stats["scratch_errors"][key] = stats["scratch_errors"].get(key, 0) + 1
        time.sleep(0.2)
    sys.stdout.close()
    sys.stdout = real_stdout
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


def _lock_files(root: Path) -> list:
    return sorted(str(p) for p in root.rglob("*.lock"))


def worker_latency(args) -> None:
    sys.path.insert(0, args.tree)
    import hermes_cli.auth as auth
    from agent.credential_pool import load_pool
    root = Path(args.root) if args.root else None
    out: dict = {}
    if root is not None:
        # The read-only diagnostic path FIRST, in a process that has taken no lock yet: it must stay
        # lock-free (no lock taken, no lock file created) and is timed on its own (design §13).
        before = _lock_files(root)
        lat_ro = []
        for _ in range(args.iterations):
            t0 = time.perf_counter()
            auth.resolve_codex_runtime_credentials(read_only=True)
            lat_ro.append(time.perf_counter() - t0)
        out["read_only"] = lat_ro
        out["lock_files_created_by_read_only"] = [p for p in _lock_files(root) if p not in before]
    auth.resolve_codex_runtime_credentials()  # warm
    lat_resolve, lat_pool = [], []
    for _ in range(args.iterations):
        t0 = time.perf_counter()
        auth.resolve_codex_runtime_credentials()
        lat_resolve.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        load_pool("openai-codex").select()
        lat_pool.append(time.perf_counter() - t0)
    out.update(resolve=lat_resolve, pool=lat_pool)
    print(json.dumps(out))


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
    if args.profile_ops:
        chains.append("mover")
        homes["mover"] = make_profile(root, "mover")
        write_store(homes["mover"], singleton=ledger.issue("mover"))
        homes["moverb"] = root / "profiles" / "moverb"  # its other name: exists half of the time
    plans = {  # (HERMES_HOME, targets)
        "root": [["singleton", "root"]], "pools": [["pool", "poolaa"], ["pool", "poolbb"]],
        "probe": [["probe", "probecc"], ["pool", "probecc"]],
        "work1": [["singleton", "work1"]], "work2": [["singleton", "work2"]],
        "borrower": [["singleton", "root"]],
        "mover": [["singleton", "mover"]], "moverb": [["singleton", "mover"]],
    }
    order = ["root", "pools", "work1", "probe", "work2", "borrower", "pools", "probe"]
    if args.profile_ops:
        order = ["root", "pools", "work1", "mover", "probe", "work2", "moverb", "borrower", "pools", "probe"]
        # bootstrap before anything races (the cold start is measured by the stage-1 runs)
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker-check", "--job"],
                       env=sandbox_env(sandbox, root, url), cwd=str(sandbox), capture_output=True, text=True,
                       timeout=180)
    procs = []
    t0 = time.monotonic()
    for i in range(args.processes):
        plan = order[i % len(order)]
        home = homes["root"] if plan in ("pools", "root", "probe") else homes[plan]
        env = sandbox_env(sandbox, home, url)
        procs.append(subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker-progress", "--targets", json.dumps(plans[plan]),
             "--threads", str(args.threads), "--seconds", str(args.seconds), "--n", str(args.n),
             "--per-chain", str(args.per_chain), "--chains", ",".join(chains),
             *(["--moving"] if plan in ("mover", "moverb") else [])],
            env=env, cwd=str(sandbox), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    plan_of = [order[i % len(order)] for i in range(args.processes)]
    mover = None
    if args.profile_ops:
        mover = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker-mover", "--seconds", str(args.seconds),
             "--n", str(args.n), "--per-chain", str(args.per_chain), "--chains", ",".join(chains),
             *(["--stick"] if args.stick else [])],
            env=sandbox_env(sandbox, root, url), cwd=str(sandbox), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
    results = []
    for p in procs:
        out, err = p.communicate(timeout=args.seconds + 300)
        try:
            results.append(json.loads(out.strip().splitlines()[-1]))
        except Exception:
            results.append({"crash": (err or out)[-2000:]})
    mover_stats = None
    if mover is not None:
        out, err = mover.communicate(timeout=args.seconds + 300)
        try:
            mover_stats = json.loads(out.strip().splitlines()[-1])
        except Exception:
            mover_stats = {"crash": (err or out)[-2000:]}
    wall = time.monotonic() - t0
    server.shutdown()

    report = {"wall_s": round(wall, 1), "processes": args.processes, "threads": args.threads}
    if args.profile_ops:
        # The gate's refusal rate is over the workers of profiles that do NOT move; the moving
        # profile's own workers are reported separately (half of them point at the name it is not
        # at, by construction), and must show no fence, error or replay.
        moving = [r for r, plan in zip(results, plan_of) if plan in ("mover", "moverb")]
        results = [r for r, plan in zip(results, plan_of) if plan not in ("mover", "moverb")]
        report["profile_ops"] = {
            "mover": mover_stats,
            "moving_profile_workers": {k: sum(r.get(k, 0) for r in moving)
                                       for k in ("attempts", "ok", "refused", "fenced", "lock_timeout", "moved")},
            "moving_profile_errors": [r.get("errors") for r in moving if r.get("errors")],
            "moving_profile_fence_reasons": [r.get("fence_reasons") for r in moving if r.get("fence_reasons")],
            "moving_profile_crashes": [r["crash"] for r in moving if "crash" in r]}
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
    unsuccessful = refused + report["fenced_outcomes"] + report["lock_timeouts"] + sum(
        n for r in results for n in (r.get("errors") or {}).values())
    report["unsuccessful_attempts"] = unsuccessful  # refused + fenced + lock timeouts + errors
    report["unsuccessful_rate"] = round(unsuccessful / attempts, 4) if attempts else None
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
    fenced = [c for c in registry["chains"].values() if c["state"] == "fenced"]
    if fenced:
        failures.append(f"{len(fenced)} chain(s) fenced: {[c.get('fence') for c in fenced]}")
    rows_by_store = {}
    for home in {root, *[d for d in (root / "profiles").iterdir() if d.is_dir() and not d.name.startswith(".")]}:
        if not (home / "auth.json").exists():
            continue
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
    if report["unsuccessful_rate"] is not None and report["unsuccessful_rate"] > 0.01:
        failures.append(f"unsuccessful-attempt rate {report['unsuccessful_rate']} > 1% "
                        "(refused + fenced + lock timeouts + errors)")
    if report["fenced_outcomes"] or report["worker_errors"] or report["worker_crashes"]:
        failures.append("fences / errors / crashes in workers")
    if args.profile_ops:
        ops_report = report["profile_ops"]
        if ops_report["moving_profile_workers"]["fenced"] or ops_report["moving_profile_errors"] \
                or ops_report["moving_profile_crashes"]:
            failures.append("fences / errors / crashes in the moving profile's workers")
        if not (mover_stats or {}).get("renames") or (mover_stats or {}).get("scratch_errors") or "crash" in (mover_stats or {}):
            failures.append(f"the mover did not rename / sign in / delete cleanly: {mover_stats}")
        if registry["ops"]:
            failures.append(f"ops left unfinished: {list(registry['ops'])}")
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


# ── stage 2: profile lifecycle / transfer / abandonment fault matrix (design rev 9.1 §13) ────────

S2_CASES = {
    "rename": ["rename-payload", "rename-decision", "rename-moving", "rename-moved-dir", "rename-moved",
               "rename-apply", "rename-finalize", "rename-commit"],
    "abandon": ["abandon-payload2", "abandon-decision2", "abandon-apply2"],
    "transfer": ["transfer-payload", "transfer-decision", "transfer-apply-mid", "transfer-apply", "transfer-commit"],
    "delete": ["delete-payload", "delete-decision", "delete-apply", "delete-commit", "delete-before-rmtree"],
    # a rename killed after its decision, then the RECOVERING dispatcher is killed too
    "recovery": ["recovery-rename-move", "recovery-rename-apply"],
}


def _quiet_profile_commands() -> None:
    """The profile commands' service / process side effects are stubbed: a worker never looks for,
    signals or stops a real process."""
    from hermes_cli import profiles
    for name, value in (("_cleanup_gateway_service", None), ("_maybe_unregister_gateway_service", None),
                        ("_stop_gateway_process", None), ("_stop_profile_backends", None),
                        ("_notify_multiplexer", None), ("_check_gateway_running", False),
                        ("_purge_identity", True), ("_live_default_multiplexer", False),
                        ("create_wrapper_script", Path("wrapper-stub")), ("remove_wrapper_script", None)):
        setattr(profiles, name, lambda *a, _v=value, **k: _v)
    import hermes_cli.profile_identity as identity
    identity._migrate_profile_identity = lambda *a, **k: None


def _install_s2_kill(boundary: str) -> None:
    """os._exit at a stage-2 step boundary (no finally, no handler: a real process death)."""
    from hermes_cli import auth_codex_ops as ops
    from hermes_cli import auth_codex_profile_ops as pops
    from hermes_cli import profiles

    def die(*_a, **_k):
        os._exit(137)

    family, _, step = boundary.partition("-")
    kind = {"rename": pops.RENAME, "transfer": pops.TRANSFER, "delete": pops.DELETE}.get(family)
    if step == "payload":  # PAYLOAD written, DECISION not: die at the decision's registry write
        real_write = ops.write_registry
        ops.write_registry = lambda data: die() if any(
            o.get("kind") == kind for o in data.get("ops", {}).values()) else real_write(data)
    elif boundary == "rename-decision":
        real_decide = pops.decide_op
        pops.decide_op = lambda *a, **k: (real_decide(*a, **k), die())[0]
    elif boundary in ("rename-moving", "rename-moved"):
        real_update = pops._update_op
        phase = pops.PHASE_MOVING if boundary == "rename-moving" else pops.PHASE_MOVED

        def update(op_id, **fields):
            real_update(op_id, **fields)
            if fields.get("phase") == phase:
                die()
        pops._update_op = update
    elif boundary == "rename-finalize":  # credentials applied, profile state not yet finalized
        pops._finalize_profile_state = die
    elif boundary == "rename-moved-dir":
        real_move = pops.ProfileRename.move
        pops.ProfileRename.move = lambda self, fn: real_move(self, lambda: (fn(), die()))
    elif boundary in ("rename-apply", "recovery-rename-apply"):
        real_apply = pops.apply_payload
        pops.apply_payload = (lambda body: (real_apply(body), die())) if boundary == "rename-apply" else die
    elif step == "commit":  # the COMMIT registry write landed; the payload is not archived yet
        real_archive = ops._archive_payload
        ops._archive_payload = lambda path, label: die() if label == "committed" else real_archive(path, label)
    elif boundary in ("transfer-decision", "delete-decision"):
        ops.apply_payload = die
        pops.apply_payload = die
    elif boundary == "transfer-apply-mid":  # the destination is written, the source not yet
        real_edits, calls = ops._apply_store_edits, []

        def edits(path, items):
            calls.append(path)
            if len(calls) == 2:
                die()
            return real_edits(path, items)
        ops._apply_store_edits = edits
    elif boundary == "transfer-apply":
        ops.commit_op = die
    elif boundary == "delete-apply":
        pops.commit_op = die
    elif boundary == "delete-before-rmtree":
        profiles._rmtree_with_retry = die
    elif boundary == "abandon-payload2":  # PAYLOAD-2 written; DECISION-2 is its next registry write
        pops.write_registry = die
    elif boundary == "abandon-decision2":
        pops.apply_payload = die
    elif boundary == "abandon-apply2":
        pops.commit_op = die
    elif boundary == "recovery-rename-move":
        real_rename = pops.os.rename
        pops.os.rename = lambda src, dst: (real_rename(src, dst), die())
    else:  # pragma: no cover - a typo in the matrix
        raise SystemExit(f"unknown boundary {boundary}")


def worker_s2(args) -> None:
    _worker_setup()
    _quiet_profile_commands()
    from hermes_cli import profiles
    from hermes_cli.auth_codex_registry import read_registry
    if args.boundary:
        _install_s2_kill(args.boundary)
    root = Path(os.environ["HERMES_HOME"])
    out: dict = {}

    def only_op():
        ops = list(read_registry().ops)
        return ops[0] if len(ops) == 1 else None

    try:
        if args.action == "rename":
            profiles.rename_profile("work", "workb")
        elif args.action == "rename-stuck":
            real = profiles._rename_checked_profile

            def failing(old_canon, new_canon, old_dir, new_dir, move):
                def locked():
                    raise PermissionError(13, "The process cannot access the file (bench: held open)")
                return real(old_canon, new_canon, old_dir, new_dir, lambda fn: move(locked))
            profiles._rename_checked_profile = failing
            try:
                profiles.rename_profile("work", "workb")
            except ValueError as exc:
                out["stuck"] = "recover --op" in str(exc)
        elif args.action in ("abandon", "recover"):
            from hermes_cli.auth_codex_profile_ops import recover_op
            out["outcome"] = recover_op(only_op(), abandon=args.action == "abandon")
        elif args.action == "transfer":
            from hermes_cli.auth_codex_profile_ops import transfer_chain
            os.environ["HERMES_HOME"] = str(root / "profiles" / "work")
            out["transfer"] = transfer_chain(root)["chain_id"]
        elif args.action == "delete":
            profiles.delete_profile("work", yes=True)
        elif args.action == "dispatch":
            from hermes_cli.auth_codex_authority import authority_dispatch
            out["dispatch"] = authority_dispatch(budget_seconds=5)
        elif args.action == "tokens":  # RULE-G5 negative continuation probes
            from hermes_cli.auth_codex_lifecycle import continuation
            from hermes_cli.auth_codex_registry import CodexAuthorityRefused, migration_lock
            for direction in ("forward", "abandon"):
                with migration_lock():
                    try:
                        with continuation(args.op or only_op() or "", direction):
                            out[direction] = "accepted"
                    except CodexAuthorityRefused:
                        out[direction] = "refused"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    print(json.dumps(out))


def _s2_sandbox(family: str):
    ledger = Ledger()
    server, url = start_endpoint(ledger)
    sandbox = Path(tempfile.mkdtemp(prefix="bench-s2-"))
    root = sandbox / "hroot"
    write_store(root, singleton=None if family == "transfer" else ledger.issue("r"))
    for name, chain in (("other", "o"), ("work", "c")):
        write_store(make_profile(root, name), singleton=ledger.issue(chain))
    return ledger, server, url, sandbox, root


def _registry_of(root: Path) -> dict:
    return _load(root / "auth.chains.json")


def _chain_of(reg: dict, ledger: Ledger, name: str, root: Path):
    """The live registry entry whose owner rows hold chain *name*'s material (by the ledger)."""
    for cid, entry in reg["chains"].items():
        if entry["state"] == "retired":
            continue
        owner_dir = Path(entry["owner"]).parent
        for home in [root, *[p for p in (root / "profiles").iterdir() if p.is_dir()]]:
            if os.path.normcase(str(home.resolve())) == os.path.normcase(str(owner_dir)):
                rows = _rows_of(_load(home / "auth.json")) if (home / "auth.json").exists() else {}
                held = {rows.get(r["row"]) for r in entry["rows"] if os.path.normcase(r["store"]) == os.path.normcase(entry["owner"])}
                if any(pair and ledger.chain_of.get(pair[1]) == name for pair in held):
                    return cid, entry, home
    return None, None, None


def run_kills2(args) -> int:
    me = str(Path(__file__).resolve())
    failures, report = [], {}
    cases = [(family, b) for family, bs in S2_CASES.items() for b in bs]
    for family, boundary in cases:
        ledger, server, url, sandbox, root = _s2_sandbox(family)
        env_root = sandbox_env(sandbox, root, url)
        work, workb = root / "profiles" / "work", root / "profiles" / "workb"

        def run(*argv, home=root, timeout=180):
            proc = subprocess.run([sys.executable, me, *argv], env={**env_root, "HERMES_HOME": str(home)},
                                  cwd=str(sandbox), capture_output=True, text=True, timeout=timeout)
            try:
                data = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
            except Exception:
                data = {"raw": proc.stdout[-300:]}
            return proc.returncode, data, proc.stderr[-800:]

        entry: dict = {"steps": []}
        run("worker-check", "--job")                                     # bootstrap
        run("worker-check", "--path", "singleton", "--force", home=work)  # c: R0 -> R1 (history)
        before_reg = _registry_of(root)
        c_before = _chain_of(before_reg, ledger, "c", root)
        rotated = ledger.last_issued["c"]
        if family == "abandon":
            rc, data, _err = run("worker-s2", "--action", "rename-stuck")
            entry["steps"].append(("rename-stuck", rc, data))
        stuck_reg = _registry_of(root)
        action = {"rename": "rename", "recovery": "rename", "abandon": "abandon", "transfer": "transfer",
                  "delete": "delete"}[family]
        first_kill = "rename-decision" if family == "recovery" else boundary
        rc, data, err = run("worker-s2", "--action", action, "--boundary", first_kill)
        entry["steps"].append((first_kill, rc, data))
        kills = [rc]
        if family == "recovery":
            rc, data, err = run("worker-s2", "--action", "dispatch", "--boundary", boundary)
            entry["steps"].append((boundary, rc, data))
            kills.append(rc)
        if any(k != 137 for k in kills):
            failures.append(f"{boundary}: a kill point was not reached ({entry['steps']} {err})")
        if boundary == "abandon-decision2":
            _rc, probe, _e = run("worker-s2", "--action", "tokens")
            entry["tokens_after_decision2"] = probe
            if probe.get("forward") != "refused" or probe.get("abandon") != "accepted":
                failures.append(f"{boundary}: continuation after DECISION-2 {probe} (forward must be refused)")
            op_id = next(iter(_registry_of(root)["ops"]), "")
        posts_before = len(ledger.presented)
        rc, data, _err = run("worker-check", "--job")  # recovery by the job alone
        entry["job"] = data
        reg = _registry_of(root)
        if family == "abandon" and boundary == "abandon-payload2":
            unchanged = reg == stuck_reg
            archived = list((root / "auth.ops" / "archive").glob("*.abandon.abandoned-compensation.*.json"))
            entry.update(op_unchanged=unchanged, compensation_archived=bool(archived))
            if not unchanged or not archived:
                failures.append(f"{boundary}: the original op/epochs changed or PAYLOAD-2 was not archived")
            rc, data, _err = run("worker-s2", "--action", "abandon")  # the explicit retry
            entry["steps"].append(("abandon-retry", rc, data))
            reg = _registry_of(root)
        if boundary == "abandon-decision2":
            _rc, probe, _e = run("worker-s2", "--action", "tokens", "--op", op_id)
            entry["tokens_after_commit2"] = probe
            if probe != {"forward": "refused", "abandon": "refused"}:
                failures.append(f"{boundary}: a continuation token was accepted after the entry was removed: {probe}")
        # END STATE
        if reg["ops"]:
            failures.append(f"{boundary}: ops left unfinished: {list(reg['ops'])}")
        cid, chain, home = _chain_of(reg, ledger, "c", root)
        moved = family in ("rename", "recovery") and boundary != "rename-payload"
        expect_home = {"transfer": root if boundary != "transfer-payload" else work}.get(family, workb if moved else work)
        # A delete is settled by what the directory removal left (review r1-3): killed before the
        # removal the login is KEPT (the tombstoned profile keeps its credential, no refresh from a
        # tombstoned home); killed after it, the recovery retires it.
        kept_by_delete = boundary == "delete-before-rmtree"
        retired = family == "delete" and boundary not in ("delete-payload", "delete-before-rmtree")
        if kept_by_delete:
            chain_now = reg["chains"].get(c_before[0]) or {}
            if chain_now.get("state") != "active" or chain_now.get("lifecycle") or rotated[1] not in (
                    work / "auth.json").read_text(encoding="utf-8"):
                failures.append(f"{boundary}: a delete killed before the removal changed the login")
        elif retired:
            gone = [c for c, e in reg["chains"].items() if c == c_before[0]]
            if not gone or reg["chains"][gone[0]]["state"] != "retired":
                failures.append(f"{boundary}: the deleted profile's login was not retired")
            if any(os.path.normcase(r["store"]).startswith(os.path.normcase(str(work.resolve()))) for e in
                   reg["chains"].values() for r in e["rows"]):
                failures.append(f"{boundary}: a row still names the deleted store")
        else:
            if chain is None or os.path.normcase(str(home.resolve())) != os.path.normcase(str(expect_home.resolve())):
                failures.append(f"{boundary}: chain c not owned by {expect_home} (got {home})")
            elif chain.get("lifecycle"):
                failures.append(f"{boundary}: lifecycle exclusion left set")
            elif chain["epoch"] < c_before[1]["epoch"]:
                failures.append(f"{boundary}: epoch moved backward")
            else:
                record = _load(Path(chain["owner"]).parent / "auth.recovery" / f"{cid}.json")
                if record["epoch"] != chain["epoch"]:
                    failures.append(f"{boundary}: record epoch {record['epoch']} != registry {chain['epoch']} (S1)")
        # a SEPARATE process: normal startup + dispatch + a forced refresh where the login lives now
        final_home = expect_home if not (retired or kept_by_delete) else (work if work.exists() else root)
        if kept_by_delete:
            final_home = root  # the tombstoned profile is not resumable; root's own login refreshes
        rc, data, _err = run("worker-check", "--job", "--path", "singleton", "--force", home=final_home)
        entry["final_refresh"] = data
        _rc, other, _e = run("worker-check", "--path", "singleton", "--force", home=root / "profiles" / "other")
        entry["unrelated_refresh"] = other
        server.shutdown()
        posts = ledger.presented[posts_before:]
        entry["posts_after"] = [(p[2], p[3]) for p in posts]
        entry["replays"] = sum(1 for p in ledger.presented if p[3] == "reused")
        if entry["replays"]:
            failures.append(f"{boundary}: {entry['replays']} replayed refresh token(s)")
        c_posts = [p for p in posts if p[2] == "c"]
        if retired or kept_by_delete:
            if c_posts:
                failures.append(f"{boundary}: the deleted login was refreshed")
        elif data.get("outcome") != "ok" or not c_posts or c_posts[0][1] != rotated[1]:
            failures.append(f"{boundary}: the recovered login did not continue from its current token ({data})")
        if other.get("outcome") != "ok":
            failures.append(f"{boundary}: the unrelated profile stopped working ({other})")
        try:
            json.loads((final_home / "auth.json").read_text(encoding="utf-8")) if (final_home / "auth.json").exists() else None
        except Exception as exc:
            failures.append(f"{boundary}: auth.json does not parse: {exc}")
        seen = set()
        for c in reg["chains"].values():
            for r in c["rows"]:
                if (r["store"], r["row"]) in seen:
                    failures.append(f"{boundary}: row {r} in two chains")
                seen.add((r["store"], r["row"]))
        report[boundary] = entry
        if not args.keep:
            shutil.rmtree(sandbox, ignore_errors=True)
    report["cases"] = len(cases)
    report["failures"] = failures
    print(json.dumps(report, indent=2, default=str))
    return 1 if failures else 0


def run_latency(args) -> int:
    tree = Path(args.tree).resolve()
    ledger = Ledger()
    sandbox = Path(tempfile.mkdtemp(prefix="bench-lat-"))
    root = sandbox / "hroot"
    s = ledger.issue("root")
    write_store(root, singleton=s, pool=[{"id": "devcod", "label": "d", "auth_type": "oauth", "priority": 0,
                                          "source": "device_code", "access_token": s[0], "refresh_token": s[1]}])
    home = root
    if args.profile:  # a PROFILE store: every store-lock acquisition takes its lifecycle lock (stage 2)
        home = make_profile(root, "work")
        p = ledger.issue("work")
        write_store(home, singleton=p, pool=[{"id": "devcow", "label": "w", "auth_type": "oauth", "priority": 0,
                                              "source": "device_code", "access_token": p[0], "refresh_token": p[1]}])
    env = sandbox_env(sandbox, home, "http://127.0.0.1:9", tree)
    if (tree / "hermes_cli" / "auth_codex_authority.py").exists():
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker-check", "--job"], env=env,
                       cwd=str(sandbox), capture_output=True, text=True, timeout=120)
    for lock in root.rglob("*.lock"):  # the read-only probe starts with no lock file anywhere
        lock.unlink()
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker-latency", "--tree", str(tree),
                           "--iterations", str(args.iterations), "--root", str(root)], env=env, cwd=str(sandbox),
                          capture_output=True, text=True, timeout=900)
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    created = data.pop("lock_files_created_by_read_only", [])
    out = {k: {"p50_ms": round(_pct(v, .5) * 1000, 3), "p99_ms": round(_pct(v, .99) * 1000, 3),
               "mean_ms": round(statistics.mean(v) * 1000, 3)} for k, v in data.items()}
    print(json.dumps({"tree": str(tree), "profile_store": bool(args.profile), **out,
                      "lock_files_created_by_read_only": created}, indent=2))
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
    p.add_argument("--stick", action="store_true", help="with --profile-ops: the first move fails (a stuck op)")
    p.add_argument("--profile-ops", action="store_true",
                   help="stage 2: a mover process renames a Codex profile back and forth and signs scratch "
                        "profiles in / deletes them while every refresh worker runs")
    k = sub.add_parser("kills")
    k.add_argument("--keep", action="store_true")
    k2 = sub.add_parser("kills2")
    k2.add_argument("--keep", action="store_true")
    w2 = sub.add_parser("worker-s2")
    w2.add_argument("--action")
    w2.add_argument("--boundary")
    w2.add_argument("--op")
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
    lt.add_argument("--profile", action="store_true", help="measure a profile store (lifecycle lock path)")
    w = sub.add_parser("worker-progress")
    w.add_argument("--targets")
    w.add_argument("--threads", type=int)
    w.add_argument("--seconds", type=int)
    w.add_argument("--n", type=int)
    w.add_argument("--per-chain", type=int)
    w.add_argument("--chains")
    w.add_argument("--moving", action="store_true")
    wm = sub.add_parser("worker-mover")
    wm.add_argument("--seconds", type=int)
    wm.add_argument("--n", type=int)
    wm.add_argument("--per-chain", type=int)
    wm.add_argument("--chains")
    wm.add_argument("--stick", action="store_true")
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
    wl.add_argument("--root")
    args = ap.parse_args()
    handlers = {"progress": run_progress, "kills": run_kills, "kills2": run_kills2, "worker-s2": worker_s2, "latency": run_latency, "startup": run_startup, "rescue-scan": run_rescue_scan,
                "worker-rescue-scan": worker_rescue_scan,
                "worker-progress": worker_progress, "worker-kill": worker_kill, "worker-check": worker_check,
                "worker-latency": worker_latency, "worker-startup": worker_startup, "worker-mover": worker_mover}
    return handlers[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
