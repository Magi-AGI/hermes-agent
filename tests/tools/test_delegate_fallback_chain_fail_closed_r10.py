"""R10/F2: delegation must not be an unvetted fallback-chain source for a child agent.

The five repaired constructors (gateway/API, cron, TUI, CLI init, one-shot) all read the chain
through ``gateway/run_config_loaders.load_fail_closed_fallback_chain``. Delegation does not go
through any of them: ``tools/delegate_tool_config._load_config`` reads the ``delegation`` section
through fail-OPEN ``load_config_readonly()`` (``:445``), a declared child chain overrides the
inherited parent policy (``:476``), enters the runtime kwargs (``:591``) and reaches child
construction in ``tools/delegate_tool.py:236``. A child primary failure then reaches the ungated
install path at ``agent/chat_completion_helpers.py``.

So with an unreadable managed ``config.yaml`` and nothing retained, a parent correctly receives NO
chain while a normal delegation hands its child the declared one — the same defect the constructor
conversion closed, through a different door.

The fix must stay narrow. A chain the clean configuration genuinely declares still installs, an
explicit ``[]`` still disables failover, and a pinned child still never borrows the parent's chain.

R11 note: the positive arm below originally handed ``_resolve_child_fallback_chain`` a declaration
that appeared NOWHERE in the home's configuration and asserted that a merely-successful read
authorized it. Both R10 reviewer lanes identified exactly that as defect R10-2 — the gate checked
readability, not the repaired policy — so the arm now declares its chain in the home's
``config.yaml``, which is the situation it was always meant to describe.

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no real AIAgent, no model call.
"""

from __future__ import annotations

import pytest

_DECLARED = [{"provider": "openrouter", "model": "declared/child"}]
_PARENT_CHAIN = [{"provider": "groq", "model": "parent/inherited"}]
_MANAGED_BROKEN = "fallback_providers: [unclosed\n"


def _clear_caches():
    from gateway import run_config_loaders
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()
    for attr in ("_LAST_CLEAN_FALLBACK_CHAIN", "_LAST_CLEAN_FALLBACK_CHAIN_AT",
                 "_LAST_CLEAN_FALLBACK_CHAIN_EPOCH"):
        slot = getattr(run_config_loaders, attr, None)
        if slot is not None:
            slot.clear()


class _Parent:
    def __init__(self, chain=None):
        self._fallback_chain = list(chain or [])


@pytest.fixture
def delegation_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (home / "config.yaml").write_text("model:\n  provider: anthropic\n  default: claude-sonnet-4\n",
                                      encoding="utf-8")
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


def _child_chain(parent, declared, *, pinned=False):
    from tools.delegate_tool_config import _resolve_child_fallback_chain
    return _resolve_child_fallback_chain(parent, {"fallback_providers": declared}, pinned)


def test_declared_child_chain_is_refused_when_the_managed_config_does_not_read(delegation_home):
    """F2: the unclean-read case the parent constructors already refuse.

    The parent here has no chain, exactly as its own fail-closed constructor would have left it, so
    the child must end with no chain either rather than the fail-open declared one.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()
    chain = _child_chain(_Parent(), _DECLARED)
    assert chain in ([], None), f"delegation installed an unvetted declared chain on the child: {chain}"


def test_a_failed_read_falls_back_to_the_parents_already_clean_chain(delegation_home):
    """Fail-closed here means "drop to the vetted default", not "destroy failover".

    The parent's chain already came from a clean fail-closed read, so inheriting it is the
    conservative answer for an unpinned child.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), _DECLARED) == _PARENT_CHAIN


def test_a_pinned_child_gets_no_chain_at_all_when_the_read_fails(delegation_home):
    """A pinned child never borrows the parent's chain, so the refusal leaves it with none."""
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), _DECLARED, pinned=True) in ([], None)


def test_a_declared_chain_still_works_on_a_clean_read(delegation_home):
    """Positive arm: the gate must not disable declared child chains wholesale.

    The declaration lives in the home's clean ``config.yaml``, so the fail-closed, bypass-aware
    read authorizes it and the child gets it (R11: previously this arm passed a declaration that
    was in no config at all, which is the R10-2 defect rather than the feature).
    """
    home, _managed = delegation_home
    (home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
        "delegation:\n  fallback_providers:\n    - provider: openrouter\n      model: declared/child\n",
        encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(), _DECLARED) == _DECLARED


def test_an_explicit_empty_declaration_still_disables_failover(delegation_home):
    """``[]`` only ever removes failover, so it needs no authorization and must not inherit."""
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), []) is None


def test_an_absent_declaration_still_inherits_the_parent_chain(delegation_home):
    """Absent/null is "inherit", unchanged by the gate."""
    assert _child_chain(_Parent(_PARENT_CHAIN), None) == _PARENT_CHAIN
