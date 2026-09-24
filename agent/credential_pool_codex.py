"""Codex rows in the credential pool under the chain authority (credential recovery protocol, P3).

The pool never refreshes, adopts, seeds, prunes or removes Codex rows on its own any more:

* a refresh runs the shared refresh transaction for the row (``auth_codex_refresh_txn``) OUTSIDE
  the pool mutex, then publishes the result version-checked (the in-memory entry must still hold
  the pair the refresh started from);
* a verdict (fenced / indeterminate / rejected) only marks the entry DEAD — metadata; the tokens
  stay on disk for the owner's record and a fresh sign-in is the exit;
* an exhausted row whose token expired is refreshed through the same deferred path, so the quota
  probe gets a live answer without a refresh under the pool mutex;
* re-reading a row after another process refreshed or re-authorized it reads the row's own disk
  copy (its chain rows are written together by the owner), never the singleton.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential

logger = logging.getLogger("agent.credential_pool")

PROVIDER = "openai-codex"


def _probe_refresh_fn():
    from hermes_cli import auth_codex
    return lambda access, refresh: auth_codex.refresh_codex_oauth_pure(access, refresh)


class CodexAuthorityPoolMixin:
    def _codex_row_store(self, entry: "PooledCredential") -> Path:
        import hermes_cli.auth as auth_mod
        if entry.id in getattr(self, "_borrowed_root_ids", ()):
            return auth_mod._global_auth_file_path() or auth_mod._auth_file_path()
        return auth_mod._auth_file_path()

    def _refresh_codex_entry(self, entry: "PooledCredential", *, force: bool,
                             mark_ok: bool = True, probe: bool = False) -> Optional["PooledCredential"]:
        """(P3) Top-level refresh of one Codex pool row: never called under the pool mutex's
        own acquisition in this module (the transaction takes the store locks itself).

        ``probe`` marks the refresh an exhausted row gets so its quota can be probed (P2): it keeps
        the row's exhaustion status and calls the token endpoint through the probe's seam."""
        import hermes_cli.auth as auth_mod
        from agent.credential_pool import _MARK_OK, _REFRESH_TIMEOUT_ENV_VARS
        from hermes_cli.auth_codex_refresh_txn import refresh_row
        from hermes_cli.auth_codex_registry import CodexAuthorityBusy
        snapshot = (entry.access_token or "", entry.refresh_token or "")
        try:
            result = refresh_row(
                self._codex_row_store(entry), f"pool:{entry.id}", snapshot, force=force,
                needs_refresh=lambda token: auth_mod._codex_access_token_is_expiring(
                    token, auth_mod.CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS),
                timeout_seconds=auth_mod.env_float(_REFRESH_TIMEOUT_ENV_VARS[PROVIDER], 20), pass_timeout=False,
                refresh_fn=_probe_refresh_fn() if probe else None)
        except CodexAuthorityBusy as exc:
            logger.info("Codex pool entry %s: %s", entry.label or entry.id, exc)
            return entry
        except auth_mod.AuthError as exc:
            if exc.relogin_required:
                logger.warning("Codex pool entry %s cannot refresh: %s", entry.label or entry.id, exc)
                self._mark_dead_refresh_grant(entry, exc)
            else:
                self._mark_exhausted(entry, None)
            return None
        except Exception as exc:
            logger.debug("Codex pool entry %s refresh failed: %s", entry.id, exc)
            self._mark_exhausted(entry, None)
            return None
        with self._lock:
            current = self._find(lambda e: e.id == entry.id)
            if current is None:
                return None
            held = (current.access_token or "", current.refresh_token or "")
            if held not in (snapshot, (result.access_token, result.refresh_token)):
                return current  # replaced meanwhile (reload / re-auth): keep the newer entry
            updates = {"access_token": result.access_token, "refresh_token": result.refresh_token,
                       "last_refresh": result.last_refresh or current.last_refresh,
                       "extra": {**current.extra, "chain_id": result.chain_id or current.extra.get("chain_id")}}
            if mark_ok:
                updates.update(_MARK_OK)
            updated = replace(current, **updates)
            self._replace_entry(current, updated)
        try:
            self._persist(status_cleared_ids=[updated.id] if mark_ok else None)
        except Exception:
            logger.warning("Codex pool entry %s: status could not be saved", updated.id, exc_info=True)
        return updated

    def _refresh_codex_selected(self, entry: Optional["PooledCredential"]) -> Optional["PooledCredential"]:
        if entry is None:
            return None
        refreshed = self._refresh_entry(entry, force=True)
        if refreshed is not None:
            with self._lock:
                self._current_id = refreshed.id
        return refreshed

    def _sync_codex_entry_from_disk(self, entry: "PooledCredential") -> "PooledCredential":
        """Adopt the row's own disk copy when another process refreshed or re-authorized it."""
        import hermes_cli.auth as auth_mod
        from agent.credential_pool import PooledCredential
        from hermes_cli.auth_codex_rows import get_row
        try:
            disk = get_row(auth_mod._load_auth_store(self._codex_row_store(entry)), f"pool:{entry.id}")
        except Exception:
            logger.debug("Codex pool entry %s: disk re-read failed", entry.id, exc_info=True)
            return entry
        if not isinstance(disk, dict):
            return entry
        same_pair = (disk.get("access_token"), disk.get("refresh_token")) == (entry.access_token, entry.refresh_token)
        if same_pair and disk.get("chain_id") == entry.extra.get("chain_id"):
            return entry
        stored = PooledCredential.from_dict(PROVIDER, disk)
        self._replace_entry(entry, stored)
        return stored

    def _codex_probe_needs_refresh(self, entry: "PooledCredential") -> bool:
        """An exhausted, rate-limited Codex row whose token expired: refresh it (deferred, outside
        the pool mutex) so the quota probe can answer. Reserves the probe slot so a refresh that
        keeps failing is not retried on every selection (#89415 throttle)."""
        import hermes_cli.auth as auth_mod
        from agent.credential_pool import STATUS_EXHAUSTED
        from hermes_cli import auth_codex as codex
        if (self.provider != PROVIDER or entry.last_status != STATUS_EXHAUSTED or not entry.refresh_token
                or not auth_mod._is_codex_rate_limit_shaped(
                    entry.last_error_code, entry.last_error_reason, entry.last_error_message)
                or not auth_mod._codex_access_token_is_expiring(entry.access_token, 0)):
            return False
        key = codex._codex_quota_probe_cache_key(entry.access_token or "")
        now = time.monotonic()
        with codex._codex_quota_probe_lock:
            cached = codex._codex_quota_probe_cache.get(key)
            if cached is not None and (now - cached[0]) < codex.CODEX_QUOTA_PROBE_MIN_INTERVAL_SECONDS:
                return False
            codex._codex_quota_probe_cache[key] = (now, None)
        return True
