"""A throwaway backend's environment: the OS basics only, every user-level location inside the eval's home.

Copying the caller's environment and overriding HOME left the real user reachable. On Windows ``Path.home()``
reads ``USERPROFILE``, the platform default Hermes home reads ``LOCALAPPDATA`` (the live install), and the
host-backend lock + rendezvous record sit under ``Path.home()``, so an eval backend could resolve the live
root and claim the host lock the live desktop finds its backend through. Inherited temp and cache overrides
(``TMP``/``TEMP``/``TMPDIR``, ``HF_HOME``, ``HF_HUB_CACHE``, pip/uv caches, ...) wrote outside it too
(checklist A18). Like the concurrency harness, start from an allow-list instead of patching a deny-list.
This covers the environment only: the harness additionally refuses a checkout ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

_OS_BASICS = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "NUMBER_OF_PROCESSORS", "OS",
              "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
              "COMMONPROGRAMFILES", "LANG", "LC_ALL", "USER", "USERNAME", "LOGNAME"}


def sealed_env(os_home: Path, base: "Mapping[str, str] | None" = None) -> dict:
    """The OS basics from *base* (default ``os.environ``) plus home, app-data, XDG, temp, cache and
    gateway-lock locations inside *os_home*, and an empty managed scope. Callers add HERMES_HOME etc."""
    os_home = Path(os_home)
    dirs = {"HOME": os_home, "USERPROFILE": os_home,
            "LOCALAPPDATA": os_home / "AppData" / "Local", "APPDATA": os_home / "AppData" / "Roaming",
            "XDG_STATE_HOME": os_home / "state", "XDG_CONFIG_HOME": os_home / "config",
            "XDG_DATA_HOME": os_home / "data", "XDG_CACHE_HOME": os_home / "cache",
            "HF_HOME": os_home / "cache" / "hf", "TEMP": os_home / "tmp", "TMP": os_home / "tmp",
            "TMPDIR": os_home / "tmp", "HERMES_GATEWAY_LOCK_DIR": os_home / "gateway-locks",
            # An EMPTY managed scope: otherwise a system one could override the eval's config.
            "HERMES_MANAGED_DIR": os_home / "managed"}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in (os.environ if base is None else base).items() if k.upper() in _OS_BASICS}
    env.update({key: str(path) for key, path in dirs.items()}, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    return env
