"""``scripts/checkout_scope.py`` for the whole-tree guards under ``tests/`` (see its docstring).

``scripts/`` is not a package, so the module is loaded from its path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "checkout_scope.py"
_spec = importlib.util.spec_from_file_location("checkout_scope", _PATH)
checkout_scope = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checkout_scope)

is_other_checkout = checkout_scope.is_other_checkout
