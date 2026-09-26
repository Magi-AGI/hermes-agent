"""Cross-process state locks wait out a sibling that holds them past Windows' ten-second cliff.

``msvcrt.locking(LK_LOCK)`` retries once a second and raises after ten tries, so on Windows a slash
worker, the compute host or a second backend holding one of these locks for more than ~10 s made the
waiter fail: the curator refused its update, a trajectory sample was dropped, a vault add or plugin
state write raised, the install id came back ``None``. Every site now polls through
``utils.lock_file_polled`` up to ``utils.BOUNDED_LOCK_WAIT_S`` (checklist A18). The holder here is a
separate process locking byte 0 exactly as any peer does; each site's public operation must succeed
once it lets go.
"""

import json
import subprocess
import sys
import time

import pytest

HOLD_S = 11.5  # past the ten one-second LK_LOCK retries

_HOLDER = r"""
import os, sys, time
fh = open(sys.argv[1], "a+b")
if fh.seek(0, 2) == 0:  # a lock file an earlier version already padded (msvcrt once needed a byte)
    fh.write(b" ")
    fh.flush()
fh.seek(0)
if os.name == "nt":
    import msvcrt
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
open(sys.argv[1] + ".released", "w").close()
"""


def _curator(tmp_path):
    from agent import curator

    def op():
        curator.update_state(lambda state: state.__setitem__("paused", True))
        return curator.load_state()["paused"] is True

    return curator._state_file().with_name(".curator_state.lock"), op


def _trajectory(tmp_path):
    from agent.trajectory import save_trajectory

    path = tmp_path / "trajectory_samples.jsonl"

    def op():
        save_trajectory([{"from": "human", "value": "hi"}], "model-x", True, filename=str(path))
        return [json.loads(line)["model"] for line in path.read_text(encoding="utf-8").splitlines()] == ["model-x"]

    return path, op


def _vault(tmp_path):
    from agent.vault_store import VaultStore

    base = tmp_path / "vault"

    def op():
        store = VaultStore(base)
        store.add_item("address", "home", {"address_line1": "1 Main St", "city": "Town", "postal_code": "1",
                                           "country": "US"})
        return [item.label for item in store.list_items()] == ["home"]

    return base / ".vault.lock", op


def _plugin_state(tmp_path):
    from hermes_cli.plugins_state import PluginState

    state = PluginState("lock-probe")

    def op():
        state.set("k", 1)
        return state.get("k") == 1

    return state.path.with_name(f".{state.path.name}.lock"), op


def _install_identity(tmp_path):
    from hermes_cli.install_identity import read_or_create_install_id

    root = tmp_path / "root"

    def op():
        install_id = read_or_create_install_id(root)
        return install_id is not None and len(install_id) == 32

    return root / ".install_id.lock", op


def _cron_jobs(tmp_path):
    from cron import jobs

    lock = jobs._jobs_lock_file()

    def op():
        # Degrading to in-process-only locking would run the section while the holder still has it.
        with jobs._jobs_lock():
            return (lock.parent / (lock.name + ".released")).exists()

    return lock, op


@pytest.mark.parametrize("site", [_curator, _trajectory, _vault, _plugin_state, _install_identity, _cron_jobs],
                         ids=["curator", "trajectory", "vault", "plugin_state", "install_identity", "cron_jobs"])
def test_state_lock_outwaits_a_sibling_process_holding_it_past_ten_seconds(site, tmp_path):
    lock_path, op = site(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock_path), str(HOLD_S)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        started = time.monotonic()
        assert op() is True
        waited = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait()
    assert waited >= HOLD_S - 1.0, f"the operation did not wait for the holder ({waited:.1f} s)"


# The previous release's lock recipes, verbatim in effect: pad an EMPTY lock file (on Windows), THEN lock.
_OLDER_PEER = r"""
import os, sys
from pathlib import Path
kind, path = sys.argv[1], Path(sys.argv[2])
nt = os.name == "nt"
try:
    if kind == "vault":
        if nt and (not path.exists() or path.stat().st_size == 0):
            path.write_text(" ", encoding="utf-8")
        fh = open(path, "r+" if nt else "a+", encoding="utf-8")
        fd = fh.fileno()
    elif kind == "plugin_state":
        fh = open(path, "a+b")
        if nt and fh.seek(0, os.SEEK_END) == 0:
            fh.write(b"\0")
            fh.flush()
        fd = fh.fileno()
    else:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        if nt and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
except OSError as exc:
    print("pad-failed", exc, flush=True)
    raise SystemExit(0)
print("padded", flush=True)
os.lseek(fd, 0, os.SEEK_SET)
if nt:
    import msvcrt
    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
"""


def _vault_lock(tmp_path):
    from agent.vault_store import VaultStore

    store = VaultStore(tmp_path / "vault")
    return tmp_path / "vault" / ".vault.lock", store._locked


def _plugin_state_lock(tmp_path):
    from hermes_cli.plugins_state import PluginState, _locked_plugin_state

    state = PluginState("lock-probe")
    return state.path.with_name(f".{state.path.name}.lock"), lambda: _locked_plugin_state(state.path)


def _install_identity_lock(tmp_path):
    from hermes_cli.install_identity import _install_id_file_lock

    root = tmp_path / "root"
    root.mkdir()
    return root / ".install_id.lock", lambda: _install_id_file_lock(root)


@pytest.mark.parametrize("kind,site", [("vault", _vault_lock), ("plugin_state", _plugin_state_lock),
                                       ("install_identity", _install_identity_lock)])
def test_a_lock_created_and_held_by_this_version_never_breaks_an_older_peer(kind, site, tmp_path):
    """Mixed versions during an update (A18 r1, Codex): an older peer pads an empty lock file before its lock
    attempt, and that write fails outright on a byte a holder has locked. A lock file this version creates
    must therefore carry its sentinel byte while held, so the older peer simply waits its turn."""
    lock_path, hold = site(tmp_path)
    with hold():
        peer = subprocess.Popen([sys.executable, "-c", _OLDER_PEER, kind, str(lock_path)],
                                stdout=subprocess.PIPE, text=True)
        first = peer.stdout.readline().strip()
    try:
        rest = peer.communicate(timeout=30)[0].split()
    finally:
        peer.kill()
    assert first == "padded", first
    assert rest == ["locked"]
