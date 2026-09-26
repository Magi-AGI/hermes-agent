"""The desktop bug-campaign evals (persistence_live, hooks_live) start a throwaway ``hermes serve`` from a
copy of the caller's environment. Overriding HOME alone left USERPROFILE / LOCALAPPDATA pointing at the
real user, so on Windows that backend resolved the live install's root and the real host-lock folder
(checklist A18). ``seal_user_dirs`` must win over whatever is inherited: a child started with the sealed
environment resolves every user-level location inside the eval's own OS home.
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
import json
from pathlib import Path
import hermes_constants
from gateway.status import _get_lock_dir
print(json.dumps({"home": str(Path.home()), "lock_dir": str(_get_lock_dir()),
                  "native_hermes_home": str(hermes_constants._get_platform_default_hermes_home())}))
"""


def _seal_user_dirs():
    spec = importlib.util.spec_from_file_location(
        "sealed_home_under_test", REPO / "evals" / "desktop_bug_campaign" / "sealed_home.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.seal_user_dirs


def test_backend_env_resolves_every_user_location_inside_the_eval_home(tmp_path):
    real = tmp_path / "real-user"
    inherited = {**os.environ, "USERPROFILE": str(real), "HOME": str(real), "HOMEDRIVE": "C:",
                 "HOMEPATH": str(real), "LOCALAPPDATA": str(real / "AppData" / "Local"),
                 "APPDATA": str(real / "AppData" / "Roaming"), "XDG_STATE_HOME": str(real / "state")}
    inherited.pop("HERMES_GATEWAY_LOCK_DIR", None)
    os_home = tmp_path / "eval" / "os-home"
    env = _seal_user_dirs()(inherited, os_home)
    env.update(HERMES_HOME=str(tmp_path / "eval" / "hermes"), PYTHONPATH=str(REPO))
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO, env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    resolved = json.loads(out.stdout.strip().splitlines()[-1])
    for name, path in resolved.items():
        assert Path(path).resolve().is_relative_to(os_home.resolve()), f"{name} escaped the eval home: {path}"
