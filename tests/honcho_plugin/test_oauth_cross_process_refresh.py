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


def test_a_sibling_stuck_holding_the_lock_fails_open_without_an_exchange(tmp_path, monkeypatch):
    import utils

    monkeypatch.setattr(utils, "BOUNDED_LOCK_WAIT_S", 1.0)
    path = tmp_path / "honcho.json"
    path.write_text(json.dumps({"hosts": {"hermes": _block("hch-at-old", "hch-rt-old", 10_000)}}), encoding="utf-8")
    exchanged = []
    monkeypatch.setattr(oauth, "_http_post_form_status",
                        lambda url, data, timeout: exchanged.append(data.get("refresh_token")) or (400, {
                            "error": "invalid_grant"}))
    sibling = subprocess.Popen([sys.executable, "-c", _SIBLING, str(path), "60", "{}"],
                               stdout=subprocess.PIPE, text=True)
    try:
        assert sibling.stdout.readline().strip() == "locked"
        token, refreshed = oauth.ensure_fresh_token(path, "hermes")
    finally:
        sibling.kill()
        sibling.wait()
    assert exchanged == [], "the refresh token was spent while another process held the refresh lock"
    assert (token, refreshed) == ("hch-at-old", False)
