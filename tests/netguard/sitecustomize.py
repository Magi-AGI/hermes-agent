"""Test-session child interpreters: refuse the real Codex auth host (see hermes_net_guard).

``tests/conftest.py`` prepends this directory to ``PYTHONPATH`` and sets
``HERMES_TEST_BLOCKED_AUTH_HOSTS`` for the whole run, so every Python a test spawns with the
inherited environment installs the guard before its own code runs. A ``sitecustomize`` that this
one shadows (later on ``sys.path``) is still executed.
"""

import importlib.util
import os
import sys

if os.environ.get("HERMES_TEST_BLOCKED_AUTH_HOSTS"):
    import hermes_net_guard

    hermes_net_guard.install()

_here = os.path.dirname(os.path.abspath(__file__))
for _entry in sys.path:
    _candidate = os.path.join(_entry or os.getcwd(), "sitecustomize.py")
    if os.path.abspath(os.path.dirname(_candidate)) == _here or not os.path.isfile(_candidate):
        continue
    _spec = importlib.util.spec_from_file_location("_shadowed_sitecustomize", _candidate)
    if _spec and _spec.loader:
        _spec.loader.exec_module(importlib.util.module_from_spec(_spec))
    break
