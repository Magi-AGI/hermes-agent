"""auth.json survives concurrent readers, torn reads and a failed post-rotation save (Magi sync, phase 2b).

Reproduced on v2026.9.21 (Windows, 2026-09-23):
- R1: CPython opens files without FILE_SHARE_DELETE, so ANY open reader makes the writer's
  ``os.replace`` fail (winerror 5); ``atomic_replace`` then retries ~300 ms and falls back to a
  NON-atomic in-place rewrite that a concurrent reader can catch half-written.
- R2/R3: a torn read was treated as corruption: ``_load_auth_store`` returned an EMPTY store and the
  caller's next read-modify-write erased every credential (the 2026-09-22 Codex credential loss).
- Codex refresh tokens are single-use: a save that fails AFTER the provider rotated the token used to
  lose the only copy of the new refresh token.
"""

from __future__ import annotations

import json
import os

import pytest

import hermes_cli.auth as auth
import hermes_cli.auth_codex as auth_codex

CODEX = {"access_token": "fake-access-1", "refresh_token": "fake-refresh-1"}


def _seed(tokens=CODEX):
    auth._save_auth_store({"providers": {"openai-codex": {"tokens": dict(tokens), "auth_mode": "chatgpt"}}})
    return auth._auth_file_path()


def _codex_tokens_on_disk():
    return json.loads(auth._auth_file_path().read_text(encoding="utf-8"))["providers"]["openai-codex"]["tokens"]


# --- R1: Hermes's own readers never force the non-atomic fallback ------------------------------------

@pytest.mark.windows_only
def test_a_hermes_reader_holding_auth_json_does_not_block_the_atomic_replace(monkeypatch):
    path = _seed()
    import utils

    def _no_fallback(*_a, **_k):
        raise AssertionError("the writer fell back to the NON-atomic in-place rewrite")

    monkeypatch.setattr(utils, "_rewrite_in_place", _no_fallback)
    with auth._open_auth_store_for_read(path):
        auth._save_auth_store({"providers": {"openai-codex": {"tokens": {**CODEX, "refresh_token": "fake-2"}}}})
    assert _codex_tokens_on_disk()["refresh_token"] == "fake-2"


# --- R2: a torn read is retried, briefly ----------------------------------------------------------------

def test_a_torn_read_is_retried_and_the_real_store_is_returned(monkeypatch):
    path = _seed()
    full = path.read_text(encoding="utf-8")
    reads = []

    def _torn_then_full(p):
        reads.append(p)
        return full[: len(full) // 2] if len(reads) == 1 else full

    monkeypatch.setattr(auth, "_read_auth_store_text", _torn_then_full)
    store = auth._load_auth_store()
    assert store["providers"]["openai-codex"]["tokens"]["refresh_token"] == "fake-refresh-1"
    assert len(reads) == 2


def test_the_torn_read_retry_is_bounded(monkeypatch):
    path = _seed()
    torn = path.read_text(encoding="utf-8")[:10]
    reads = []
    monkeypatch.setattr(auth, "_read_auth_store_text", lambda p: reads.append(p) or torn)
    auth._load_auth_store()
    assert len(reads) == auth._AUTH_TORN_READ_ATTEMPTS


# --- R3: a store derived from an unparseable read never overwrites a store that parses ----------------

def test_a_store_from_a_transient_torn_read_is_never_saved_over_the_real_one(monkeypatch):
    path = _seed()
    full = path.read_text(encoding="utf-8")
    path.write_text(full[: len(full) // 2], encoding="utf-8")   # torn, as a mid-rewrite reader sees it
    degraded = auth._load_auth_store()
    assert degraded["providers"] == {}
    path.write_text(full, encoding="utf-8")                      # the writer finishes
    degraded.setdefault("providers", {})["other"] = {"x": 1}
    with pytest.raises(auth.AuthStoreStaleReadError):
        auth._save_auth_store(degraded)
    assert _codex_tokens_on_disk()["refresh_token"] == "fake-refresh-1"


def test_genuine_persistent_corruption_can_still_be_recovered_by_a_new_login():
    path = _seed()
    path.write_text("{not json", encoding="utf-8")
    store = auth._load_auth_store()
    store.setdefault("providers", {})["openai-codex"] = {"tokens": {"access_token": "new", "refresh_token": "new"}}
    auth._save_auth_store(store)                                  # same corrupt bytes still on disk: allowed
    assert _codex_tokens_on_disk()["refresh_token"] == "new"
    assert "_hermes_degraded_read" not in json.loads(path.read_text(encoding="utf-8"))


def test_corrupt_copies_never_overwrite_an_earlier_one():
    path = _seed()
    path.write_text("{first corrupt", encoding="utf-8")
    auth._load_auth_store()
    path.write_text("{second corrupt", encoding="utf-8")
    auth._load_auth_store()
    copies = sorted(p.read_text(encoding="utf-8") for p in path.parent.glob("auth.json.corrupt*"))
    assert "{first corrupt" in copies and "{second corrupt" in copies
