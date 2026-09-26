"""The desktop bug-campaign evals (persistence_live, hooks_live) start a throwaway ``hermes serve``. Built
from a copy of the caller's environment with only HOME overridden, it kept USERPROFILE / LOCALAPPDATA and
the temp and cache overrides pointing at the real user, so on Windows that backend resolved the live
install's root and the real host-lock folder, and wrote temp and cache files outside the eval (checklist
A18; A18 r1, Codex). ``sealed_env`` must not carry any inherited location: a child started with it
resolves every user-level, temp and cache location inside the eval's own OS home.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_PROBE = r"""
import json, os, tempfile
from pathlib import Path
import hermes_constants
from gateway.status import _get_lock_dir
print(json.dumps({"resolved": {"home": str(Path.home()), "lock_dir": str(_get_lock_dir()),
                               "native_hermes_home": str(hermes_constants._get_platform_default_hermes_home()),
                               "tempdir": tempfile.gettempdir()},
                  "env": dict(os.environ)}))
"""

# Locations a developer's shell commonly overrides; every one points at the "real" user here.
_INHERITED = ("USERPROFILE", "HOME", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
              "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMP", "TEMP", "TMPDIR", "HF_HOME", "HF_HUB_CACHE",
              "TRANSFORMERS_CACHE", "PIP_CACHE_DIR", "UV_CACHE_DIR", "npm_config_cache", "HERMES_SCRATCH_DIR",
              "HERMES_GATEWAY_LOCK_DIR", "HERMES_MANAGED_DIR")


def _sealed_env():
    spec = importlib.util.spec_from_file_location(
        "sealed_home_under_test", REPO / "evals" / "desktop_bug_campaign" / "sealed_home.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.sealed_env


def test_backend_env_carries_no_inherited_location_and_resolves_inside_the_eval_home(tmp_path):
    real = tmp_path / "real-user"
    real.mkdir()
    inherited = {**os.environ, **{name: str(real / name.lower()) for name in _INHERITED}, "HOMEDRIVE": "C:"}
    os_home = tmp_path / "eval" / "os-home"
    env = _sealed_env()(os_home, inherited)
    env.update(HERMES_HOME=str(tmp_path / "eval" / "hermes"), PYTHONPATH=str(REPO))
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO, env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    probe = json.loads(out.stdout.strip().splitlines()[-1])
    for name, path in probe["resolved"].items():
        assert Path(path).resolve().is_relative_to(os_home.resolve()), f"{name} escaped the eval home: {path}"
    leaked = {k: v for k, v in probe["env"].items() if str(real) in v}
    assert not leaked, f"inherited locations reached the backend: {sorted(leaked)}"
