"""A sibling process mid-refresh holds ``honcho.json.lock`` longer than ten seconds (one exchange cycle
may take ``_REFRESH_TOTAL_BUDGET_SECONDS``). The waiter must adopt the grant the sibling persists, never
replay the single-use refresh token it read before: on Windows ``LK_LOCK`` gave up after ten tries and
the lock "degraded to in-process only", so the waiter exchanged the already-rotated token (checklist A18).
"""

import json
import subprocess
import sys
import time

from plugins.memory.honcho import oauth

HOLD_S = 11.5

_SIBLING = r"""
import json, os, sys, time
config, hold, rotated = sys.argv[1], float(sys.argv[2]), json.loads(sys.argv[3])
fh = open(config + ".lock", "a+b")
fh.seek(0)
if os.name == "nt":
    import msvcrt
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(hold)
with open(config, "w", encoding="utf-8") as out:
    json.dump({"hosts": {"hermes": rotated}}, out)
"""


def _block(access, refresh, expires_at):
    return {"apiKey": access, "oauth": {"refreshToken": refresh, "expiresAt": expires_at,
                                        "clientId": "hermes-desktop",
                                        "tokenEndpoint": "http://127.0.0.1:9/oauth/token",
                                        "scope": "write", "tokenType": "Bearer"}}


def test_waiter_adopts_the_siblings_rotation_instead_of_replaying_the_refresh_token(tmp_path, monkeypatch):
    path = tmp_path / "honcho.json"
    path.write_text(json.dumps({"hosts": {"hermes": _block("hch-at-old", "hch-rt-old", 10_000)}}), encoding="utf-8")
    rotated = _block("hch-at-sibling", "hch-rt-sibling", int(time.time()) + 3600)
    exchanged = []
    monkeypatch.setattr(oauth, "_http_post_form_status",
                        lambda url, data, timeout: exchanged.append(data.get("refresh_token")) or (400, {
                            "error": "invalid_grant"}))
    sibling = subprocess.Popen([sys.executable, "-c", _SIBLING, str(path), str(HOLD_S), json.dumps(rotated)],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert sibling.stdout.readline().strip() == "locked"
        token, refreshed = oauth.ensure_fresh_token(path, "hermes")
    finally:
        sibling.kill()
        sibling.wait()
    assert exchanged == [], "the single-use refresh token was replayed while a sibling held the lock"
    assert (token, refreshed) == ("hch-at-sibling", True)


def test_callers_queued_behind_a_stuck_sibling_wait_it_out_once_and_never_exchange(tmp_path, monkeypatch):
    """Past the lock budget the refresh fails open under the failure cooldown, and that cooldown is seen by
    every caller already queued on the process mutex and by a forced refresh (A18 r1, Codex): one
    cross-process wait in all, not one per caller while holding the process-wide mutex."""
    import threading

    import utils

    monkeypatch.setattr(utils, "BOUNDED_LOCK_WAIT_S", 2.0)
    waits = []
    monkeypatch.setattr(oauth, "lock_file_polled", lambda fh: waits.append(1) or utils.lock_file_polled(fh))
    path = tmp_path / "honcho.json"
    path.write_text(json.dumps({"hosts": {"hermes": _block("hch-at-old", "hch-rt-old", 10_000)}}), encoding="utf-8")
    exchanged = []
    monkeypatch.setattr(oauth, "_http_post_form_status",
                        lambda url, data, timeout: exchanged.append(data.get("refresh_token")) or (400, {
                            "error": "invalid_grant"}))
    sibling = subprocess.Popen([sys.executable, "-c", _SIBLING, str(path), "60", "{}"],
                               stdout=subprocess.PIPE, text=True)
    results = []
    try:
        assert sibling.stdout.readline().strip() == "locked"
        callers = [threading.Thread(target=lambda: results.append(oauth.ensure_fresh_token(path, "hermes")))
                   for _ in range(3)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(30)
        forced = oauth.force_refresh_token(path, "hermes", failed_access_token="hch-at-old")
    finally:
        sibling.kill()
        sibling.wait()
    assert exchanged == [], "the refresh token was spent while another process held the refresh lock"
    assert results == [("hch-at-old", False)] * 3 and forced is None
    assert len(waits) == 1, f"{len(waits)} cross-process waits on one stuck sibling"
