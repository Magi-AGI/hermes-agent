"""Real child processes for the Codex restore tests: a restorer that can pause or die at a named
point, and a separate process that recovers afterwards (fake credentials only; the children inherit
the test's temp HERMES_HOME / home and the session's network guard)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The restorer: run_import with its gateway/wrapper side effects stubbed. Every destination it
# extracts is logged; at --stop-at it either waits for a gate file (pause) or dies with os._exit
# (a real death: no finally, no handler, no lock release beyond the OS's).
RESTORER = r'''
import os, sys, time
from pathlib import Path
from types import SimpleNamespace
archive, log, point, mode, ready, gate = sys.argv[1:7]
from hermes_cli import backup, auth_codex_ops, auth_codex_registry, auth_codex_restore
import hermes_cli.auth_credential_io as io
backup._revive_gateway_after_import = lambda root: None
backup._restore_profile_wrappers = lambda root: []

def stop():
    if mode == "die":
        os._exit(137)
    Path(ready).write_text("ready")
    while not Path(gate).exists():
        time.sleep(0.05)

real_extract = backup._extract_member_atomically
def extract(zf, member, target, *a, **k):
    with open(log, "a", encoding="utf-8") as f:
        f.write(str(target) + "\n")
    real_extract(zf, member, target, *a, **k)
    if point == "after-auth-json" and member == "auth.json":
        stop()
backup._extract_member_atomically = extract
if point == "before-fence":
    auth_codex_restore.fence_restored = lambda: stop()
if point == "mid-fence":
    for module in (auth_codex_registry, auth_codex_ops):
        module.write_registry = lambda data: stop()
if point == "before-barrier-clear":
    real_unlink = io.unlink_retrying
    io.unlink_retrying = lambda p: stop() if str(p).endswith("auth.restore-barrier.json") else real_unlink(p)
backup.run_import(SimpleNamespace(zipfile=archive, force=True))
print("RESTORED")
'''

# A separate process that recovers: one dispatcher pass (bounded lock wait), then a forced refresh
# whose token-endpoint seam only RECORDS what it would have presented.
RECOVERER = r'''
import json, sys
from pathlib import Path
presented = Path(sys.argv[1])
import hermes_cli.auth as auth
from hermes_cli import auth_codex
def record(access, refresh, **kw):
    with open(presented, "a", encoding="utf-8") as f:
        f.write(refresh + "\n")
    raise auth.AuthError("recorded only", provider="openai-codex", code="invalid_grant", relogin_required=True)
auth.refresh_codex_oauth_pure = record
auth_codex.refresh_codex_oauth_pure = record
from hermes_cli.auth_codex_authority import authority_dispatch
out = {"dispatch": authority_dispatch(budget_seconds=float(sys.argv[2]))}
if sys.argv[3] == "refresh":
    try:
        auth.resolve_codex_runtime_credentials(force_refresh=True)
        out["refresh"] = "ok"
    except auth.AuthError as exc:
        out["refresh"] = exc.code
print(json.dumps(out))
'''


def child_env(userhome: Path) -> dict:
    env = dict(os.environ)
    env.update(HOME=str(userhome), USERPROFILE=str(userhome))
    return env


def spawn(code: str, args, userhome: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], cwd=str(REPO), env=child_env(userhome),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def restore(archive: Path, tmp: Path, userhome: Path, *, point: str = "none", mode: str = "die",
            wait_ready: bool = False):
    """Start the restorer. With ``mode='pause'`` returns once it reached *point* (it waits for
    ``tmp/gate``); ``release()`` lets it finish."""
    log, ready, gate = tmp / "extracted.log", tmp / "ready", tmp / "gate"
    proc = spawn(RESTORER, [archive, log, point, mode, ready, gate], userhome)
    if wait_ready:
        deadline = time.monotonic() + 120
        while not ready.exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f"restorer never paused: {proc.communicate()}")
            time.sleep(0.05)
    return proc, log, gate


def recover(tmp: Path, userhome: Path, *, budget: float = 5.0, refresh: bool = True) -> dict:
    presented = tmp / "presented.log"
    proc = spawn(RECOVERER, [presented, budget, "refresh" if refresh else "none"], userhome)
    out, err = proc.communicate(timeout=180)
    try:
        result = json.loads(out.strip().splitlines()[-1])
    except Exception as exc:  # pragma: no cover - diagnostic
        raise AssertionError(f"recoverer failed: {err[-2000:]}") from exc
    result["presented"] = presented.read_text(encoding="utf-8").split() if presented.exists() else []
    return result


# A quick-snapshot restore into an EXPLICIT hermes_home that pauses at a file barrier right after
# the guard validated, before the first file is written.
SNAPSHOT_RESTORER = r'''
import sys, time
from pathlib import Path
snapshot_id, home, ready, gate = sys.argv[1:5]
from hermes_cli import backup
real = backup._restore_quick_snapshot_files
def paused(*a, **k):
    Path(ready).write_text("ready")
    while not Path(gate).exists():
        time.sleep(0.05)
    return real(*a, **k)
backup._restore_quick_snapshot_files = paused
print("RESTORED" if backup.restore_quick_snapshot(snapshot_id, hermes_home=Path(home)) else "REFUSED")
'''

# A fresh Codex sign-in into whatever HERMES_HOME the child runs under.
SIGNER = r'''
import sys
import hermes_cli.auth as auth
auth._save_codex_tokens({"access_token": sys.argv[1], "refresh_token": sys.argv[2]})
print("SIGNED-IN")
'''


def spawn_in(code: str, args, userhome: Path, hermes_home: Path) -> subprocess.Popen:
    env = child_env(userhome)
    env["HERMES_HOME"] = str(hermes_home)
    return subprocess.Popen([sys.executable, "-c", code, *map(str, args)], cwd=str(REPO), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
