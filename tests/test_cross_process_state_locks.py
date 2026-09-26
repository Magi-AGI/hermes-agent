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


@pytest.mark.parametrize("site", [_curator, _trajectory, _vault, _plugin_state, _install_identity],
                         ids=["curator", "trajectory", "vault", "plugin_state", "install_identity"])
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
