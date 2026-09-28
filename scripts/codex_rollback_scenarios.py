"""Rollback from the Codex chain authority to an OLDER build, proven with real processes (deploy gate F8).

The new build (this checkout) and the old build (a checkout of the deployed commit, ``--old-tree``)
run as separate processes against one throwaway Hermes root and the LOCAL recording token endpoint
of ``bench_codex_authority`` (127.0.0.1; single-use refresh tokens; every presented token is in its
ledger). No network, no live install, no ~/.codex (CODEX_HOME points into the sandbox).

  python scripts/codex_rollback_scenarios.py --old-tree <checkout of the old commit> [--only a,b] [--keep]

Scenarios (the endpoint ledger must show ZERO replays in every asserted step):
  b0, c0  CONTROLS: b and c WITHOUT the preparation; the old build must replay there (the oracle
     detects the hazard the preparation removes)
  a  active chain rotated by the new build -> prepare -> the old build refreshes it (singleton and
     pool path): it presents only the latest token and succeeds
  b  chain fenced indeterminate (token consumed, response lost) -> prepare -> the old build never
     presents it (asks for a sign-in)
  c  retired chain (replacement login; a consumed copy left in a profile) -> prepare -> the old build
     never presents the retired pair
  d  unfinished ownership op -> prepare refuses, nothing changes; resolved -> prepare succeeds
  e  a real process death at every step of the preparation -> the new build stays safe, re-running
     the preparation completes it, then the old build is safe; also REPORTED (not asserted): the old
     build started directly on the crashed state
  f  roll forward: the old build rotates once, then the new build starts -> no replay; what is fenced
  g  REPORTED baseline, old build only (no new build involved): two stores using one root login
  h  the multi-profile workload after the preparation (borrowers, a same-account profile login, a copy,
     another account): refreshes from different homes back to back, both paths — zero replays
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_codex_authority import (  # noqa: E402
    REPO, Ledger, _rows_of, make_profile, sandbox_env, start_endpoint, write_store)

ME = str(Path(__file__).resolve())
CRASH_POINTS = ["start", "settle", "settle-adopted", "remove", "op-payload", "op-decision", "op-apply",
                "op-commit", "between-ops", "fixes", "final", "receipt"]


# ── worker side (runs under ONE tree: PYTHONPATH decides new or old) ──────────────────────────────

def _worker_setup(tree: str):
    sys.path.insert(0, tree)
    import hermes_cli
    from hermes_cli import auth_codex
    here = Path(hermes_cli.__file__).resolve()
    if Path(tree).resolve() not in here.parents:
        raise SystemExit(f"worker imported {here}, not the tree {tree}")
    auth_codex.CODEX_OAUTH_TOKEN_URL = os.environ["BENCH_TOKEN_URL"] + "/token"


def _die(*_a, **_k):
    os._exit(137)


def _crash_ops(point: str) -> None:
    """Die at one step of the preparation's removal ops (and only those ops)."""
    from hermes_cli import auth_codex_ops as ops
    name = {"op-payload": "write_registry", "op-decision": "apply_payload",
            "op-apply": "_apply_record_action", "op-commit": "commit_op"}[point]
    real = getattr(ops, name)
    state = {"armed": False}

    def gate(*a, _real=real, **k):
        if state["armed"]:
            _die()
        return _real(*a, **k)
    setattr(ops, name, gate)
    real_run = ops.run_op

    def run_op(payload, view, _real=real_run):
        state["armed"] = payload.kind == "rollback-prepare"
        return _real(payload, view)
    ops.run_op = run_op


def _crash_second_op() -> None:
    from hermes_cli import auth_codex_ops as ops
    real_run = ops.run_op
    count = {"n": 0}

    def run_op(payload, view, _real=real_run):
        if payload.kind == "rollback-prepare":
            count["n"] += 1
            if count["n"] == 2:
                _die()
        return _real(payload, view)
    ops.run_op = run_op


def _crash_after_adopt() -> None:
    from hermes_cli import auth_codex_refresh_txn as txn
    real = txn._write_attempt

    def write_attempt(owner, record, attempt, **kw):
        if attempt is None and (record.get("history") or [{}])[-1].get("outcome") == "adopted-after-crash":
            _die()  # the rows hold the successor; the record still says pending
        return real(owner, record, attempt, **kw)
    txn._write_attempt = write_attempt


def _install_prepare_crash(point: str) -> None:
    from hermes_cli import auth_codex_authority as authority
    from hermes_cli import auth_codex_rollback as rb
    simple = {"start": (authority, "dispatch_locked"), "settle": (rb, "_settle_active_chains"),
              "remove": (rb, "_remove_rows"), "fixes": (rb, "_apply_fixes"), "final": (rb, "_final_assessment"),
              "receipt": (rb, "_write_receipt")}
    if point in simple:
        module, name = simple[point]
        setattr(module, name, _die)
    elif point == "settle-adopted":
        _crash_after_adopt()
    elif point == "between-ops":
        _crash_second_op()
    elif point.startswith("op-"):
        _crash_ops(point)
    else:
        raise SystemExit(f"unknown crash point {point}")


def _install_refresh_kill(boundary: str) -> None:
    import hermes_cli.auth as auth_mod
    from hermes_cli import auth_codex
    from hermes_cli import auth_codex_refresh_txn as txn
    if boundary == "after-response":
        for module in (auth_mod, auth_codex):
            real = module.refresh_codex_oauth_pure

            def refresh(*a, _real=real, **k):
                _real(*a, **k)  # the server consumed the token and issued a successor ...
                _die()          # ... that never reaches Hermes
            module.refresh_codex_oauth_pure = refresh
    elif boundary == "after-pending":
        real = txn._write_attempt

        def write_attempt(owner, record, attempt, **kw):
            out = real(owner, record, attempt, **kw)
            if attempt and attempt.get("state") == "pending":
                _die()
            return out
        txn._write_attempt = write_attempt


def _outcome(fn):
    from hermes_cli.auth_constants import AuthError
    try:
        return {"outcome": "ok", "value": fn()}
    except AuthError as exc:
        return {"outcome": exc.code or "auth-error", "message": str(exc)[:300]}


def _refresh(auth):
    return _outcome(lambda: auth.resolve_codex_runtime_credentials(force_refresh=True)["api_key"][-12:])


def _pool_refresh():
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    results = []
    for entry in list(pool.entries()):
        got = pool.try_refresh_matching(credential_id=entry.id)
        results.append({"id": entry.id, "refreshed": got is not None})
    return {"outcome": "ok", "pool": results}


def _prepare(arg):
    if arg:
        _install_prepare_crash(arg)
    from hermes_cli.auth_codex_rollback import prepare_rollback
    from hermes_cli.auth_constants import AuthError
    try:
        result, report = prepare_rollback()
    except AuthError as exc:
        return {"outcome": "refused", "code": exc.code, "message": str(exc)[:600]}
    return {"outcome": "safe" if result.safe else "not-safe", "removed": len(report["removed"]),
            "fenced_while_settling": report["fenced_while_settling"], "cli": result.codex_cli.get("status"),
            "kept": [(Path(v.store).parent.name, v.row) for v in result.kept],
            "codex_off": [Path(k).parent.name for k in result.codex_off], "refusal": result.refusal}


def worker(args) -> None:
    _worker_setup(args.tree)
    import hermes_cli.auth as auth
    arg = json.loads(args.arg) if args.arg else None
    op = args.op
    if op == "new-dispatch":
        from hermes_cli.auth_codex_authority import startup_dispatch
        out = {"dispatch": startup_dispatch()}
    elif op in ("new-refresh", "old-refresh"):
        out = _refresh(auth)
    elif op in ("new-pool", "old-pool"):
        out = _pool_refresh()
    elif op == "new-sign-in":
        auth._save_codex_tokens({"access_token": arg[0], "refresh_token": arg[1]})
        out = {"outcome": "ok"}
    elif op == "new-sign-in-undecided":
        from hermes_cli import auth_codex_ops as ops
        ops.apply_payload = _die  # the sign-in is DECIDED, never applied
        auth._save_codex_tokens({"access_token": arg[0], "refresh_token": arg[1]})
        out = {"outcome": "survived"}
    elif op == "new-kill":
        _install_refresh_kill(arg)
        out = {**_refresh(auth), "survived": True}
    elif op == "new-resolve":
        from hermes_cli.auth_codex_ops import resolve_unrecoverable
        out = {"outcome": resolve_unrecoverable(arg)}
    elif op == "prepare":
        out = _prepare(arg)
    else:
        raise SystemExit(f"unknown op {op}")
    print(json.dumps(out, default=str))


# ── parent side ──────────────────────────────────────────────────────────────────────────────────

class Sandbox:
    def __init__(self, name: str, old_tree: Path, keep: bool) -> None:
        self.ledger = Ledger()
        self.server, self.url = start_endpoint(self.ledger)
        self.dir = Path(tempfile.mkdtemp(prefix=f"codex-rollback-{name}-"))
        self.root = self.dir / "hroot"
        self.root.mkdir()
        self.old_tree = old_tree
        self.keep = keep
        self.log: list = []

    def home(self, profile: str = "") -> Path:
        return self.root / "profiles" / profile if profile else self.root

    def run(self, tree: str, op: str, arg=None, profile: str = "", timeout: int = 180) -> dict:
        tree_path = str(REPO) if tree == "new" else str(self.old_tree)
        env = sandbox_env(self.dir, self.home(profile), self.url, Path(tree_path))
        argv = [sys.executable, ME, "worker", "--tree", tree_path, "--op", op]
        if arg is not None:
            argv += ["--arg", json.dumps(arg)]
        before = len(self.ledger.presented)
        proc = subprocess.run(argv, env=env, cwd=str(self.dir), capture_output=True, text=True, timeout=timeout)
        try:
            result = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            result = {"crash": proc.returncode, "stderr": proc.stderr[-1500:]}
        result["exit"] = proc.returncode
        result["presented"] = [(p[1], p[3]) for p in self.ledger.presented[before:]]
        self.log.append({"tree": tree, "op": op, "arg": arg, "profile": profile or "root",
                         **{k: v for k, v in result.items() if k != "presented"},
                         "presented": [f"{self.ledger.chain_of.get(t)}:{t[-6:]}:{o}" for t, o in result["presented"]]})
        return result

    def replays(self) -> int:
        return sum(1 for p in self.ledger.presented if p[3] != "ok")

    def presented_tokens(self) -> list:
        return [p[1] for p in self.ledger.presented]

    def ledger_lines(self) -> list:
        return [f"{c}:{t[-6:]}:{o}" for _t, t, c, o in self.ledger.presented]

    def codex_material(self, profile: str = "") -> set:
        path = self.home(profile) / "auth.json"
        if not path.exists():
            return set()
        rows = _rows_of(json.loads(path.read_text(encoding="utf-8")))
        return {t for pair in rows.values() for t in pair if t}

    def registry(self) -> dict:
        path = self.root / "auth.chains.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def fences(self) -> list:
        return sorted(f"{c['state']}:{(c.get('fence') or {}).get('reason')}"
                      for c in self.registry().get("chains", {}).values())

    def close(self) -> None:
        self.server.shutdown()
        if not self.keep:
            shutil.rmtree(self.dir, ignore_errors=True)


class Checks:
    def __init__(self) -> None:
        self.failures: list = []

    def that(self, cond: bool, what: str) -> None:
        if not cond:
            self.failures.append(what)


def _no_replay(sb: Sandbox, ck: Checks, name: str) -> None:
    ck.that(sb.replays() == 0, f"{name}: the endpoint saw {sb.replays()} replayed token(s)")
    tokens = sb.presented_tokens()
    ck.that(len(tokens) == len(set(tokens)), f"{name}: a token was presented twice")


def scenario_a(sb: Sandbox, ck: Checks) -> dict:
    write_store(sb.root, singleton=sb.ledger.issue("a"))
    sb.run("new", "new-dispatch")
    ck.that(sb.run("new", "new-refresh")["outcome"] == "ok", "a: new build refresh failed")
    latest = sb.ledger.last_issued["a"]
    prep = sb.run("new", "prepare")
    ck.that(prep["outcome"] == "safe" and prep["removed"] == 0, f"a: prepare {prep}")
    old = sb.run("old", "old-refresh")
    ck.that(old["outcome"] == "ok", f"a: old build refresh failed {old}")
    ck.that([t for t, _o in old["presented"]] == [latest[1]],
            "a: the old build did not present exactly the latest token")
    pool = sb.run("old", "old-pool")
    ck.that(pool["outcome"] == "ok" and any(r["refreshed"] for r in pool["pool"]), f"a: old pool refresh {pool}")
    _no_replay(sb, ck, "a")
    return {"prepare": prep, "old_refresh": old["outcome"], "old_pool": pool.get("pool")}


def _indeterminate_state(sb: Sandbox, ck: Checks) -> str:
    write_store(sb.root, singleton=sb.ledger.issue("b"))
    sb.run("new", "new-dispatch")
    sb.run("new", "new-refresh")
    kill = sb.run("new", "new-kill", "after-response")
    ck.that(kill["exit"] == 137, f"b: the kill point was not reached {kill}")
    consumed = sb.presented_tokens()[-1]
    check = sb.run("new", "new-refresh")
    ck.that(check["outcome"] == "codex_refresh_indeterminate" and not check["presented"], f"b: new build {check}")
    ck.that(consumed in sb.codex_material(), "b: precondition — the rows keep the consumed predecessor")
    return consumed


def scenario_b0(sb: Sandbox, ck: Checks) -> dict:
    """CONTROL (must replay): scenario b WITHOUT the preparation — the oracle sees the hazard."""
    _indeterminate_state(sb, ck)
    old = sb.run("old", "old-refresh")
    ck.that(sb.replays() >= 1, "b0: control — the old build did not replay without the preparation")
    return {"old": old.get("outcome")}


def scenario_c0(sb: Sandbox, ck: Checks) -> dict:
    """CONTROL (must replay): scenario c WITHOUT the preparation."""
    pair = _retired_copy_state(sb, ck)
    old = sb.run("old", "old-refresh", profile="work")
    ck.that(sb.presented_tokens().count(pair[1]) >= 2, "c0: control — the old build did not present the retired pair")
    return {"old_work": old.get("outcome")}


def scenario_b(sb: Sandbox, ck: Checks) -> dict:
    consumed = _indeterminate_state(sb, ck)
    prep = sb.run("new", "prepare")
    ck.that(prep["outcome"] == "safe" and prep["removed"] >= 1, f"b: prepare {prep}")
    ck.that(not sb.codex_material(), "b: Codex material left for the old build")
    old = [sb.run("old", op).get("outcome") for op in ("old-refresh", "old-pool")]
    ck.that(sb.presented_tokens().count(consumed) == 1, "b: the old build presented the consumed token")
    _no_replay(sb, ck, "b")
    return {"prepare": prep, "old": old}


def _retired_copy_state(sb: Sandbox, ck: Checks):
    pair = sb.ledger.issue("c")
    write_store(sb.root, singleton=pair)
    make_profile(sb.root, "work")
    write_store(sb.home("work"), singleton=pair)  # one login in two stores: root owns it
    sb.run("new", "new-dispatch")
    sb.run("new", "new-refresh")                 # root rotates: work's copy is consumed
    sb.run("new", "new-sign-in", sb.ledger.issue("n"))  # replacement login: chain c retired
    ck.that(pair[1] in sb.codex_material("work"), "c: precondition — the retired copy is in work")
    return pair


def scenario_c(sb: Sandbox, ck: Checks) -> dict:
    pair = _retired_copy_state(sb, ck)
    prep = sb.run("new", "prepare")
    ck.that(prep["outcome"] == "safe", f"c: prepare {prep}")
    ck.that(not sb.codex_material("work"), "c: the retired copy is still in work")
    for name, op in (("work", "refresh"), ("", "refresh"), ("work", "pool"), ("", "pool"), ("work", "refresh"),
                     ("", "refresh")):
        sb.run("old", f"old-{op}", profile=name)
    ck.that(sb.presented_tokens().count(pair[1]) == 1, "c: the old build presented the retired (consumed) pair")
    _no_replay(sb, ck, "c")
    return {"prepare": prep}


def scenario_d(sb: Sandbox, ck: Checks) -> dict:
    write_store(sb.root, singleton=sb.ledger.issue("d"))
    sb.run("new", "new-dispatch")
    sb.run("new", "new-refresh")
    undecided = sb.run("new", "new-sign-in-undecided", sb.ledger.issue("n"))
    ck.that(undecided["exit"] == 137, f"d: the sign-in was not left decided {undecided}")
    (op_id,) = sb.registry()["ops"]
    (sb.root / "auth.ops" / f"{op_id}.json").unlink()  # its payload is lost: unrecoverable
    before = (sb.root / "auth.json").read_bytes()
    prep = sb.run("new", "prepare")
    ck.that(prep["outcome"] == "refused" and op_id[:8] in prep.get("message", ""), f"d: prepare {prep}")
    ck.that((sb.root / "auth.json").read_bytes() == before, "d: a refused preparation changed auth.json")
    ck.that(not (sb.root / "auth.rollback-receipt.json").exists(), "d: a refused preparation wrote a receipt")
    sb.run("new", "new-resolve", op_id[:8])
    again = sb.run("new", "prepare")
    ck.that(again["outcome"] == "safe", f"d: prepare after resolve {again}")
    old = [sb.run("old", op).get("outcome") for op in ("old-refresh", "old-pool")]
    _no_replay(sb, ck, "d")
    return {"refused": prep.get("message", "")[:300], "after_resolve": again, "old": old}


def _mixed_state(sb: Sandbox) -> None:
    """root: chain A rotated, then left with a PENDING successor not in its rows (settle adopts);
    side: a consumed copy of A; work: its own chain B fenced indeterminate."""
    a0 = sb.ledger.issue("A")
    write_store(sb.root, singleton=a0)
    make_profile(sb.root, "side")
    write_store(sb.home("side"), singleton=a0)
    make_profile(sb.root, "work")
    sb.run("new", "new-dispatch")
    sb.run("new", "new-refresh")
    sb.run("new", "new-kill", "after-pending")
    sb.run("new", "new-sign-in", sb.ledger.issue("B"), profile="work")
    sb.run("new", "new-kill", "after-response", profile="work")
    sb.run("new", "new-refresh", profile="work")  # fences B indeterminate


def _old_round(sb: Sandbox) -> list:
    """The old build across the homes, refreshes from different homes back to back, both paths."""
    steps = [("", "refresh"), ("work", "refresh"), ("side", "refresh"), ("", "refresh"), ("", "pool"),
             ("work", "pool"), ("side", "pool"), ("", "refresh")]
    return [sb.run("old", f"old-{op}", profile=name)["outcome"] for name, op in steps]


def _crash_case(make_sandbox, ck: Checks, point: str) -> dict:
    sb = make_sandbox(f"e-{point}")
    try:
        _mixed_state(sb)
        crash = sb.run("new", "prepare", point)
        entry = {"crash_exit": crash["exit"]}
        ck.that(crash["exit"] == 137, f"e/{point}: the crash point was not reached ({crash})")
        ck.that(not (sb.root / "auth.rollback-receipt.json").exists(), f"e/{point}: a crashed run left a receipt")
        new = [sb.run("new", "new-dispatch").get("dispatch")]  # the new build keeps running
        new += [sb.run("new", "new-refresh", profile=p)["outcome"] for p in ("", "side", "work")]
        entry["new_after_crash"] = new
        before = len(sb.ledger.presented)
        again = sb.run("new", "prepare")
        ck.that(again["outcome"] == "safe", f"e/{point}: re-running prepare {again}")
        entry["rerun"] = again["outcome"]
        entry["old_after_rerun"] = _old_round(sb)
        entry["posts_after_rerun"] = len(sb.ledger.presented) - before
        _no_replay(sb, ck, f"e/{point}")
        entry["replays"] = sb.replays()
        entry["ledger"] = sb.ledger_lines()
    finally:
        sb.close()
    direct = make_sandbox(f"e-direct-{point}")
    try:  # REPORTED: the old build started on the crashed state, without the re-run
        _mixed_state(direct)
        direct.run("new", "prepare", point)
        entry["old_directly_on_crashed_state"] = {"outcomes": _old_round(direct), "replays": direct.replays()}
    finally:
        direct.close()
    return entry


def scenario_h(sb: Sandbox, ck: Checks) -> dict:
    """The multi-profile workload (review ruling): root's login; work signed in with the SAME account;
    side a copy of root's; notes none; other ANOTHER account. Refreshes from different homes back to
    back, both paths, repeatedly."""
    a0 = sb.ledger.issue("A")
    write_store(sb.root, singleton=a0)
    for name in ("work", "side", "notes", "other"):
        make_profile(sb.root, name)
    write_store(sb.home("side"), singleton=a0)
    sb.run("new", "new-dispatch")
    sb.run("new", "new-refresh")
    sb.run("new", "new-sign-in", sb.ledger.issue("A"), profile="work")
    sb.run("new", "new-sign-in", sb.ledger.issue("B"), profile="other")
    prep = sb.run("new", "prepare")
    ck.that(prep["outcome"] == "safe", f"h: prepare {prep}")
    steps = [("notes", "refresh"), ("", "refresh"), ("work", "refresh"), ("side", "refresh"), ("", "pool"),
             ("other", "refresh"), ("notes", "pool"), ("", "refresh"), ("other", "pool"), ("work", "pool"),
             ("side", "pool"), ("", "refresh"), ("notes", "refresh"), ("other", "refresh"), ("", "pool"),
             ("work", "refresh"), ("", "refresh"), ("other", "pool")]
    outcomes = [(name or "root", op, sb.run("old", f"old-{op}", profile=name).get("outcome")) for name, op in steps]
    _no_replay(sb, ck, "h")
    ok = {p[2] for p in sb.ledger.presented if p[3] == "ok"}
    ck.that({"A", "B"} <= ok, "h: root's and the other account's logins must keep working under the old build")
    return {"prepare": prep, "old_steps": outcomes}


def scenario_e(make_sandbox, ck: Checks, points) -> dict:
    return {point: _crash_case(make_sandbox, ck, point) for point in points}


def scenario_f(sb: Sandbox, ck: Checks) -> dict:
    write_store(sb.root, singleton=sb.ledger.issue("f"))
    sb.run("new", "new-dispatch")
    sb.run("new", "new-refresh")
    ck.that(sb.run("new", "prepare")["outcome"] == "safe", "f: prepare")
    old = sb.run("old", "old-refresh")                         # the old build rotates once
    ck.that(old["outcome"] == "ok", f"f: old refresh {old}")
    before = len(sb.ledger.presented)
    start = sb.run("new", "new-dispatch")                      # roll forward: the new build starts
    new = sb.run("new", "new-refresh")
    pool = sb.run("new", "new-pool")
    ck.that(len(sb.ledger.presented) == before, "f: the new build refreshed material the old build rotated")
    ck.that(new["outcome"] != "ok", f"f: the new build used an unverified pair {new}")
    _no_replay(sb, ck, "f")
    return {"dispatch": start.get("dispatch"), "new_refresh": new.get("outcome"), "new_pool": pool,
            "fences": sb.fences(), "message": new.get("message")}


def scenario_g(sb: Sandbox, ck: Checks) -> dict:
    """REPORTED only: the old build alone, one root login, two profiles without their own."""
    write_store(sb.root, singleton=sb.ledger.issue("g"))
    make_profile(sb.root, "p1")
    make_profile(sb.root, "p2")
    steps = [("p1", sb.run("old", "old-refresh", profile="p1")["outcome"]),
             ("root", sb.run("old", "old-refresh")["outcome"]),
             ("p2", sb.run("old", "old-refresh", profile="p2")["outcome"])]
    return {"steps": steps, "root_holds": sorted(t[-6:] for t in sb.codex_material()),
            "p1_holds": sorted(t[-6:] for t in sb.codex_material("p1"))}


SCENARIOS = {"a": scenario_a, "b0": scenario_b0, "b": scenario_b, "c0": scenario_c0, "c": scenario_c,
             "d": scenario_d, "f": scenario_f, "g": scenario_g, "h": scenario_h}


def run(args) -> int:
    old_tree = Path(args.old_tree).resolve()
    if not (old_tree / "hermes_cli" / "auth.py").is_file():
        raise SystemExit(f"--old-tree {old_tree} is not a Hermes checkout")
    only = [s for s in (args.only or "b0,c0,a,b,c,d,e,f,g,h").split(",") if s]
    points = [p for p in (args.crash_points or ",".join(CRASH_POINTS)).split(",") if p]
    ck = Checks()
    report = {"old_tree": str(old_tree), "new_tree": str(REPO)}
    started = time.time()
    for name in only:
        if name == "e":
            report["e"] = scenario_e(lambda n: Sandbox(n, old_tree, args.keep), ck, points)
            continue
        sb = Sandbox(name, old_tree, args.keep)
        try:
            report[name] = SCENARIOS[name](sb, ck)
            report[name]["replays"] = sb.replays()
            report[name]["ledger"] = sb.ledger_lines()
            if args.verbose:
                report[name]["log"] = sb.log
        finally:
            sb.close()
    report["seconds"] = round(time.time() - started, 1)
    report["failures"] = ck.failures
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(json.dumps({"failures": ck.failures, "seconds": report["seconds"], "report": args.out}, indent=2))
    else:
        print(text)
    return 1 if ck.failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("worker")
    w.add_argument("--tree", required=True)
    w.add_argument("--op", required=True)
    w.add_argument("--arg")
    ap.add_argument("--old-tree")
    ap.add_argument("--only")
    ap.add_argument("--crash-points", help="scenario e: a comma-separated subset of " + ",".join(CRASH_POINTS))
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out", help="write the full JSON report here (keep it outside the checkout)")
    args = ap.parse_args()
    if args.cmd == "worker":
        worker(args)
        return 0
    if not args.old_tree:
        ap.error("--old-tree is required")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
