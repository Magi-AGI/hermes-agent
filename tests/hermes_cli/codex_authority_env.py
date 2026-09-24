"""Shared fixtures for the Codex credential recovery protocol tests (fake credentials only).

``FakeTokenEndpoint`` stands in for OpenAI's token endpoint at the ``refresh_codex_oauth_pure``
seam: refresh tokens are single-use, a reused one is rejected (``refresh_token_reused``), and
``calls`` is the ledger of every refresh token presented. Nothing here touches the network,
``~/.codex`` or a live install.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from hermes_cli import auth_codex as _auth_codex
from hermes_cli.auth_constants import AuthError, httpx

# The real token-endpoint function, captured before any fixture replaces the seams (tests that
# drive it use a mocked httpx transport; the conftest points its URL at a closed local port).
REAL_REFRESH_CODEX_OAUTH_PURE = _auth_codex.refresh_codex_oauth_pure


def fake_jwt(account: str = "acct-1", *, sub: str = "user-1", exp_in: int = 3600) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    payload = {"exp": int(time.time()) + exp_in, "sub": sub, "nonce": uuid.uuid4().hex,
               "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


class FakeTokenEndpoint:
    """Single-use refresh tokens. ``mode``: ok | lost | connect | status429 | same_refresh | reject."""

    def __init__(self) -> None:
        self.valid: Dict[str, bool] = {}
        self.calls: List[str] = []
        self.mode = "ok"
        self.exp_in = 3600

    def issue(self, account: str = "acct-1", *, exp_in: Optional[int] = None) -> Tuple[str, str]:
        refresh = f"rt-{uuid.uuid4().hex[:10]}"
        self.valid[refresh] = True
        return fake_jwt(account, exp_in=self.exp_in if exp_in is None else exp_in), refresh

    def __call__(self, access_token: str, refresh_token: str, *, timeout_seconds: float = 20.0) -> Dict[str, str]:
        self.calls.append(refresh_token)
        if self.mode == "connect":
            raise httpx.ConnectError("connection refused")
        if not self.valid.get(refresh_token):
            raise AuthError("Codex refresh token was already consumed", provider="openai-codex",
                            code="refresh_token_reused", relogin_required=True)
        if self.mode == "reject":
            raise AuthError("invalid_grant", provider="openai-codex", code="invalid_grant", relogin_required=True)
        self.valid[refresh_token] = False  # consumed by the server from here on
        if self.mode == "lost":
            raise httpx.ReadTimeout("response lost")
        if self.mode == "status429":
            err = AuthError("quota", provider="openai-codex", code="codex_rate_limited", relogin_required=False)
            err.status_code = 429
            raise err
        access, refresh = self.issue()
        if self.mode == "same_refresh":
            refresh = refresh_token
            self.valid[refresh_token] = True
        return {"access_token": access, "refresh_token": refresh, "last_refresh": "2026-09-24T00:00:00Z"}


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """A temp Hermes root (a custom root: its named profiles live under ``<root>/profiles``) with
    ``Path.home`` pinned inside tmp and the token endpoint faked."""
    home = tmp_path / "hroot"
    home.mkdir()
    (tmp_path / "userhome").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "userhome")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-cli"))
    endpoint = FakeTokenEndpoint()
    import hermes_cli.auth as auth_mod
    from hermes_cli import auth_codex
    monkeypatch.setattr(auth_mod, "refresh_codex_oauth_pure", endpoint)
    monkeypatch.setattr(auth_codex, "refresh_codex_oauth_pure", endpoint)
    monkeypatch.setattr(auth_mod, "_import_codex_cli_tokens", lambda: None)
    monkeypatch.setattr(auth_mod, "_probe_codex_quota_restored", lambda *a, **k: None)
    return home, endpoint


def make_profile(root: Path, name: str) -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    return home


def write_store(home: Path, *, singleton: Optional[Tuple[str, str]] = None, pool: Optional[list] = None,
                suppressed: Optional[list] = None, extra: Optional[dict] = None) -> Path:
    store: dict = {"version": 1, "providers": {}}
    if singleton:
        store["providers"]["openai-codex"] = {
            "tokens": {"access_token": singleton[0], "refresh_token": singleton[1]},
            "last_refresh": "2026-09-01T00:00:00Z", "auth_mode": "chatgpt"}
        store["active_provider"] = "openai-codex"
    if pool is not None:
        store["credential_pool"] = {"openai-codex": pool}
    if suppressed:
        store["suppressed_sources"] = {"openai-codex": list(suppressed)}
    if extra:
        store.update(extra)
    path = home / "auth.json"
    path.write_text(json.dumps(store, indent=2), encoding="utf-8")
    return path


def pool_row(entry_id: str, pair: Tuple[str, str], source: str = "device_code", **fields) -> dict:
    return {"id": entry_id, "label": fields.pop("label", entry_id), "auth_type": "oauth",
            "priority": fields.pop("priority", 0), "source": source, "access_token": pair[0],
            "refresh_token": pair[1], "base_url": "https://chatgpt.com/backend-api/codex", **fields}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def registry(root: Path) -> dict:
    return read_json(root / "auth.chains.json")
