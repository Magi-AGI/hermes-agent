"""Seal a throwaway backend's user-level locations inside the eval's own OS home.

Setting ``HOME`` is not enough on Windows: ``Path.home()`` reads ``USERPROFILE``, the platform default
Hermes home reads ``LOCALAPPDATA`` (the live install lives there), and the host-backend lock + rendezvous
record sit under ``Path.home()`` unless ``HERMES_GATEWAY_LOCK_DIR`` / ``XDG_STATE_HOME`` say otherwise. An
eval backend that inherits the real ones can resolve the live install's root and claim the host lock the
live desktop discovers its backend through (checklist A18; the concurrency harness seals the same set).
"""

from __future__ import annotations

from pathlib import Path


def seal_user_dirs(env: dict, os_home: Path) -> dict:
    """Point every user-level location in *env* (updated in place and returned) inside *os_home*."""
    os_home = Path(os_home)
    dirs = {"LOCALAPPDATA": os_home / "AppData" / "Local", "APPDATA": os_home / "AppData" / "Roaming",
            "XDG_STATE_HOME": os_home / "state", "XDG_CONFIG_HOME": os_home / "config",
            "XDG_DATA_HOME": os_home / "data", "XDG_CACHE_HOME": os_home / "cache",
            "HERMES_GATEWAY_LOCK_DIR": os_home / "gateway-locks"}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    for key in ("HOMEDRIVE", "HOMEPATH"):  # Windows' fallback when USERPROFILE is unset
        env.pop(key, None)
    env.update({key: str(path) for key, path in dirs.items()}, HOME=str(os_home), USERPROFILE=str(os_home))
    return env
