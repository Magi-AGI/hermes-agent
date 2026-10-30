"""R12/R11-1: an administrator's explicit child-failover disable must not become parent fallback.

R11 bound a declared child chain to what the declaration's own configuration owner authorizes
(``tools/delegate_tool_config._authorized_child_chain``), closing both mechanisms R10-2 named. Both
R11 reviewer lanes then independently found a residual of that fix (codex ``R11-1``, agy
``R11-N1``), adjudicated **P2, blocking**: the helper returns ``None`` for *four* different
situations —

1. the fail-closed read raised (unreadable managed file, torn user file);
2. the route named no configuration owner (a per-call ``credentials_cfg``);
3. the owner section declares no ``fallback_providers`` at all (absent/null);
4. the owner section declares ``fallback_providers: []`` — an **authoritative empty chain**;

— and the caller maps every one of them to its inheritance default, which for an unpinned child is
the parent's chain (``delegate_tool_config.py:545,551-553``). Only the first three mean "no answer,
use the vetted default". The fourth is an answer: the administrator explicitly disabled child
failover. Upgrading it to "inherit the parent's chain" hands the child the **root** chain — a
different key from the one the admin suppressed — so an explicit disable is defeated by a chain the
admin never authorized for children. That is the bug class this slice exists to close, which is
why it is a blocker rather than a carried note.

Reachability needs no race: the route only reaches authorization when ``routing_cfg`` carries a
*nonempty* declaration (``:547-550`` short-circuits ``None`` and ``[]``), i.e. exactly the
fail-open/stale-snapshot state R10-2 is about, while the authoritative read says ``[]``.

R12 then found the same defect on a second path (R12-1, P2, both reviewer lanes independently):
with NO declaration on the route at all, the resolver returned the inheritance default before
ever consulting ``_authorized_child_chain``, so an unpinned child inherited the parent ROOT
chain past the same authoritative disable — at child construction, not P3-a's mid-turn
activation seam. R13 repairs it: an absent/null route declaration now consults the owner too,
and only an authoritative ``[]`` changes its answer. Failed, unowned and no-answer reads still
inherit, a nonempty authorized chain is not installed on the inheritance path, and a route's
own explicit ``[]`` still short-circuits without authorization.

The nearest pre-R12 arms could not catch it, which is why this file exists:
``test_a_declaration_the_repaired_config_no_longer_authorizes_is_refused``
(``tests/hermes_cli/test_safe_mode_fallback_chain_lifecycle_r11.py:247``) passes a parent whose
chain is **empty**, so the unpinned default is already ``None`` and the assertion passes trivially;
the nonempty-parent arm at ``:278`` is **pinned**, whose default is also ``None``.

Every negative arm below is paired with a positive one, because the fix must keep distinguishing a
**failed** read (cold and warm), an **absent** declaration and a **malformed-only** one — all of
which still drop to the default — from an authoritative ``[]``.

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no real AIAgent construction, no model call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_PARENT_CHAIN = [{"provider": "groq", "model": "parent/inherited"}]
# The nonempty declaration the fail-open section carries while the authoritative read says []:
# a stale ``cli.CLI_CONFIG`` snapshot or a section read before the admin's edit landed.
_STALE_DECLARED = [{"provider": "openrouter", "model": "stale/declared"}]
_USER_CLEAN = "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
_USER_DECLARING = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "delegation:\n  fallback_providers:\n    - provider: openrouter\n      model: stale/declared\n"
)
# The administrator's explicit disable of CHILD failover. The root chain is a different key.
_MANAGED_DISABLES_CHILD_FAILOVER = "delegation:\n  fallback_providers: []\n"
_MANAGED_BROKEN = "delegation: [unclosed\n"
# R15 fixtures: a managed MANDATORY child chain, and a managed ROOT revocation (no delegation key).
_MANAGED_CHAIN = [{"provider": "groq", "model": "managed/child"}]
_MANAGED_DECLARES_CHILD_CHAIN = (
    "delegation:\n  fallback_providers:\n    - provider: groq\n      model: managed/child\n")
_MANAGED_REVOKES_ROOT = "fallback_providers: []\n"
_USER_ROOT_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n  - provider: groq\n    model: parent/inherited\n")


def _clear_caches():
    """Every config cache AND the shared per-home chain retention — a fresh process's state."""
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


def _clear_config_caches_only():
    """Drop the config caches but KEEP the retention record, for the warm-failure arm."""
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


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
    (home / "config.yaml").write_text(_USER_CLEAN, encoding="utf-8")
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


def _child_chain(parent, declared, *, pinned=False):
    from tools.delegate_tool_config import _resolve_child_fallback_chain
    return _resolve_child_fallback_chain(parent, {"fallback_providers": declared}, pinned)


# ------------------------------------------------- the authoritative [] must disable, not inherit


def test_an_authoritative_empty_chain_is_not_replaced_by_the_parents_chain(delegation_home):
    """R11-1: a clean MANAGED ``delegation.fallback_providers: []`` disables child failover.

    The admin's answer is "this child gets no failover". Returning the parent's chain substitutes a
    chain read from a *different* key (the root one) for the key the admin suppressed.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_DISABLES_CHILD_FAILOVER, encoding="utf-8")
    _clear_caches()
    chain = _child_chain(_Parent(_PARENT_CHAIN), _STALE_DECLARED)
    assert chain is None, (
        "an administrator's explicit child-failover disable was upgraded to the parent's "
        f"inherited chain: {chain}")


def test_an_authoritative_empty_chain_suppressing_a_user_chain_is_not_replaced(delegation_home):
    """The same defect where the admin REVOKES a chain the user declared.

    Here the suppressed declaration is genuinely the user's own (it is in the user file), so the
    inherited parent chain can hand the child exactly the entries the admin disabled for children.
    """
    home, managed = delegation_home
    (home / "config.yaml").write_text(_USER_DECLARING, encoding="utf-8")
    (managed / "config.yaml").write_text(_MANAGED_DISABLES_CHILD_FAILOVER, encoding="utf-8")
    _clear_caches()
    chain = _child_chain(_Parent(_PARENT_CHAIN), _STALE_DECLARED)
    assert chain is None, f"a revoked user chain fell through to the parent's chain: {chain}"


def test_the_authoritative_empty_chain_reaches_child_construction_as_no_chain(delegation_home):
    """End of the real route: the child AGENT is constructed with no chain.

    ``_resolve_child_fallback_chain`` is reached through ``_resolve_child_runtime``, whose answer
    becomes the ``fallback_model`` kwarg of the child ``AIAgent`` (``delegate_tool.py:239``) and
    therefore the ``_fallback_chain`` slot the activation seam would install from. Asserting the
    constructor kwarg is what proves the disable survives the whole path, not just the resolver.
    """
    from tests.tools.test_delegate import _make_mock_parent
    from tools.delegate_tool import _build_child_agent

    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_DISABLES_CHILD_FAILOVER, encoding="utf-8")
    _clear_caches()
    parent = _make_mock_parent(depth=0)
    parent._fallback_chain = list(_PARENT_CHAIN)
    assert parent._fallback_chain, "premise: the parent must have a chain to leak"
    with patch("run_agent.AIAgent") as mock_agent:
        mock_agent.return_value = MagicMock()
        _build_child_agent(
            task_index=0, goal="authoritative disable", context=None, toolsets=None, model=None,
            max_iterations=10, task_count=1, parent_agent=parent,
            routing_cfg={"fallback_providers": list(_STALE_DECLARED)},
            routing_owner_path=("delegation",),
        )
    installed = mock_agent.call_args.kwargs["fallback_model"]
    assert installed is None, f"the child agent was constructed with an inherited chain: {installed}"


def test_inheritance_stands_when_nothing_is_declared_and_no_disable_exists(delegation_home):
    """Premise guard: the arms in this file would prove nothing if the default were already empty.

    With NO administrator disable anywhere and no declaration on the route, the unpinned default
    is the parent's nonempty chain — so every ``None`` above and below is the fix, not an
    artefact of an empty default.
    """
    assert _child_chain(_Parent(_PARENT_CHAIN), None) == _PARENT_CHAIN


def test_an_authoritative_empty_chain_also_overrides_plain_inheritance(delegation_home):
    """R12-1: the administrator's ``[]`` binds the absent/null route too.

    R12's repair honoured the disable only when the fail-open route carried a nonempty
    declaration. With no declaration at all, the resolver returned the inheritance default before
    consulting the owner, so the unpinned child inherited the parent ROOT chain — the same defeat
    of the explicit disable through a key the admin never authorized for children, at child
    construction (not P3-a's mid-turn activation seam).
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_DISABLES_CHILD_FAILOVER, encoding="utf-8")
    _clear_caches()
    chain = _child_chain(_Parent(_PARENT_CHAIN), None)
    assert chain is None, (
        f"an undeclared route inherited the parent chain past an authoritative disable: {chain}")


def test_the_authoritative_disable_reaches_child_construction_with_no_declaration(delegation_home):
    """End of the real route for the absent-declaration path: the child AGENT gets no chain.

    Same shape as the declared-route construction arm above: the resolver's answer becomes the
    ``fallback_model`` kwarg of the child ``AIAgent``, and the disable must survive the whole
    path with ``routing_cfg`` carrying no ``fallback_providers`` key at all.
    """
    from tests.tools.test_delegate import _make_mock_parent
    from tools.delegate_tool import _build_child_agent

    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_DISABLES_CHILD_FAILOVER, encoding="utf-8")
    _clear_caches()
    parent = _make_mock_parent(depth=0)
    parent._fallback_chain = list(_PARENT_CHAIN)
    assert parent._fallback_chain, "premise: the parent must have a chain to leak"
    with patch("run_agent.AIAgent") as mock_agent:
        mock_agent.return_value = MagicMock()
        _build_child_agent(
            task_index=0, goal="authoritative disable, nothing declared", context=None,
            toolsets=None, model=None, max_iterations=10, task_count=1, parent_agent=parent,
            routing_cfg={}, routing_owner_path=("delegation",),
        )
    installed = mock_agent.call_args.kwargs["fallback_model"]
    assert installed is None, f"the child agent inherited a chain past the disable: {installed}"


# ---------------------------------------------------- the inheritance probe is quiet and single-read


def test_plain_inheritance_is_silent_and_observes_config_once(delegation_home, caplog):
    """R14 (agy R13 findings 3+5): the absent-route consult is a PROBE, not a declared-chain audit.

    Nothing declared anywhere is the DEFAULT configuration of every normal home, so an unpinned
    child spawn must not emit the "authorizes no child fallback chain" warning (nothing is
    misconfigured) and must not observe the config twice. On the R13 tip (`1507d576eb`) every such
    spawn warned once and advanced this thread's observation epoch by 2 (the retention publication
    plus the effective read).
    """
    import logging

    from hermes_cli import config_effective

    before = config_effective.last_observation_epoch() or 0
    with caplog.at_level(logging.WARNING, logger="tools.delegate_tool"):
        chain = _child_chain(_Parent(_PARENT_CHAIN), None)
    assert chain == _PARENT_CHAIN
    delegate_warnings = [r.getMessage() for r in caplog.records if r.name == "tools.delegate_tool"]
    assert not delegate_warnings, delegate_warnings
    after = config_effective.last_observation_epoch() or 0
    assert after - before <= 1, f"the inheritance probe observed the config {after - before} times"


# ------------------------------------- R15: the authoritative answer binds the absent route wholesale


def test_a_managed_child_chain_is_installed_when_the_route_declares_nothing(delegation_home):
    """R14-1 (Codex P2, agy F1): a clean managed ``delegation.fallback_providers: [M]`` governs.

    The route's unvetted snapshot carrying NO declaration cannot override the owner's clean,
    nonempty declaration: the child gets M, never the parent's root chain.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_DECLARES_CHILD_CHAIN, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), None) == _MANAGED_CHAIN


def test_a_managed_child_chain_reaches_a_pinned_child_on_the_absent_route(delegation_home):
    """R14-1 pinned arm: the declared route already installs an authorized chain for pinned
    children; the absent route must not silently drop the same mandatory policy to ``None``."""
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_DECLARES_CHILD_CHAIN, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), None, pinned=True) == _MANAGED_CHAIN


def test_a_revoked_root_chain_is_not_inherited_by_a_new_child(delegation_home):
    """R14-2 (Codex P2, agy F4): construction-time adoption of a revoked chain.

    The admin revokes the ROOT chain (managed ``fallback_providers: []``) without touching the
    delegation key. The inherited default IS that root chain, and the construction-time clean read
    contains the revocation — a brand-new child must not receive it.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_REVOKES_ROOT, encoding="utf-8")
    _clear_caches()
    chain = _child_chain(_Parent(_PARENT_CHAIN), None)
    assert chain is None, f"a new child inherited a chain the clean read shows revoked: {chain}"


def test_the_inheritance_probe_publishes_its_observation_for_other_constructors(delegation_home):
    """R14-3 (Codex P2, introduced by R14): a probe that observes a suppression must publish it.

    Sequence from the review: retain clean root chain F; the admin revokes root AND delegation;
    an inheritance probe reads that clean config (and disables its own child); the managed file
    then breaks within the retention grace. A later constructor's fail-closed read must serve the
    SUPPRESSION, not the stale retained F — which is exactly what the R14 probe's skipped
    publication lost.
    """
    from gateway.run_config_loaders import load_fail_closed_fallback_chain

    home, managed = delegation_home
    (home / "config.yaml").write_text(_USER_ROOT_CHAIN, encoding="utf-8")
    _clear_caches()
    assert load_fail_closed_fallback_chain(home) == _PARENT_CHAIN, "premise: root F retained"
    (managed / "config.yaml").write_text(
        "fallback_providers: []\ndelegation:\n  fallback_providers: []\n", encoding="utf-8")
    _clear_config_caches_only()
    assert _child_chain(_Parent(_PARENT_CHAIN), None) is None, "premise: the probe sees the disable"
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches_only()
    served = load_fail_closed_fallback_chain(home)
    assert not served, (
        f"a constructor was served the stale retained chain past an observed suppression: {served}")


def test_a_legacy_root_revocation_binds_inheritance_too(delegation_home):
    """R15-1 (Codex P2): managed ``fallback_model: []`` is a valid root suppression.

    The canonical root reader supports the legacy key (``fallback_config.py``), so a revocation
    expressed through it is the same construction-time R14-2 defect through the other root source.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text("fallback_model: []\n", encoding="utf-8")
    _clear_caches()
    chain = _child_chain(_Parent(_PARENT_CHAIN), None)
    assert chain is None, f"a legacy-key root revocation was inherited past: {chain}"


def test_a_modern_root_revocation_beside_a_live_legacy_chain_is_not_total(delegation_home):
    """Codex's paired control: ``fallback_providers: []`` next to a VALID legacy chain.

    The canonical reader retains the legacy entries, so the child check must not declare total
    revocation — the clean root still resolves to a chain, and inheritance stands.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(
        "fallback_providers: []\nfallback_model:\n  - provider: groq\n    model: legacy/kept\n",
        encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), None) == _PARENT_CHAIN


def test_publication_lands_in_the_slot_of_its_own_read(delegation_home, monkeypatch):
    """R15-2 (Codex P2): the publisher carries the READ's bypass identity, never a re-sample.

    An in-process bypass transition between a read and its publication must not file a
    user-inclusive chain into the safe-mode retention slot. Pin: publishing with the read's
    ``ignore_user_config=False`` while the ambient flag already reads bypassed lands in the
    user-inclusive slot and leaves the bypassed slot empty. (On the pre-fix code the publisher has
    no such parameter and re-samples the ambient flag — the polluted-slot sequence.)
    """
    from gateway import run_config_loaders as rcl
    from hermes_cli import config_effective
    from hermes_constants import hermes_home_key

    home, _managed = delegation_home
    (home / "config.yaml").write_text(_USER_ROOT_CHAIN, encoding="utf-8")
    _clear_caches()
    cfg = config_effective.load_user_config_effective(
        home / "config.yaml", fail_closed=True, ignore_user_config=False)
    epoch = config_effective.last_observation_epoch()
    monkeypatch.setattr(config_effective, "user_config_bypassed", lambda: True)
    rcl.publish_clean_fallback_chain_observation(home, cfg, epoch=epoch, ignore_user_config=False)
    user_slot = rcl._retained_fallback_chain(rcl._chain_retention_key(hermes_home_key(home), False))
    bypassed_slot = rcl._retained_fallback_chain(rcl._chain_retention_key(hermes_home_key(home), True))
    assert user_slot == _PARENT_CHAIN, f"the read's own slot was not published: {user_slot}"
    assert bypassed_slot is rcl._NO_RETENTION, (
        f"a user-inclusive chain polluted the safe-mode slot: {bypassed_slot}")


def test_a_failed_inheritance_probe_never_consumes_retention_as_its_answer(delegation_home):
    """Codex's missing warm arm: retention holds a SUPPRESSION, the managed file breaks, and an
    absent-route child must still INHERIT — a failed probe has no authoritative answer and must
    not read the retained ``None`` as the admin's disable."""
    from gateway.run_config_loaders import load_fail_closed_fallback_chain

    home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_REVOKES_ROOT, encoding="utf-8")
    _clear_caches()
    assert load_fail_closed_fallback_chain(home) is None, "premise: a suppression is retained"
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches_only()
    assert _child_chain(_Parent(_PARENT_CHAIN), None) == _PARENT_CHAIN


# --------------------------------------------- failure, absence and malformation still take default


def test_a_cold_failed_managed_read_still_falls_back_to_the_parent_chain(delegation_home):
    """Fail-closed means "drop to the vetted default", not "destroy failover" (R10/F2).

    A tri-state answer must not collapse an unreadable managed file into the admin's explicit
    disable: nothing authoritative was observed here at all.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), _STALE_DECLARED) == _PARENT_CHAIN


def test_a_failed_read_after_a_clean_observation_still_falls_back_to_the_parent_chain(delegation_home):
    """The sharper failure arm: a clean observation happened FIRST, then the file broke.

    This is the state the retention record exists for, and it is the one a naive "treat an empty
    authorized answer as a disable" fix gets wrong: the second read raises, so there is still no
    authoritative ``[]`` — the child must inherit, not lose failover.
    """
    home, managed = delegation_home
    (managed / "config.yaml").write_text("delegation:\n  other_key: 1\n", encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), _STALE_DECLARED) == _PARENT_CHAIN, (
        "premise: the clean read must already be taking the default here")
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches_only()
    assert _child_chain(_Parent(_PARENT_CHAIN), _STALE_DECLARED) == _PARENT_CHAIN
    assert (home / "config.yaml").exists(), "premise: the user file was never the broken one"


def test_a_malformed_only_authoritative_declaration_still_inherits_the_parent_chain(delegation_home):
    """A declaration the normalizer empties is malformed input, NOT an administrator's disable.

    ``get_fallback_chain`` drops entries without both provider and model. An authorized answer that
    is empty *because every entry was malformed* must keep taking the default, or a typo in a chain
    entry would silently read as "failover disabled".
    """
    home, _managed = delegation_home
    (home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
        "delegation:\n  fallback_providers:\n    - provider: openrouter\n", encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), _STALE_DECLARED) == _PARENT_CHAIN


def test_a_chain_the_clean_config_authorizes_still_installs_unchanged(delegation_home):
    """Positive control: the arm that fails if the fix degenerates into "never install".
    """
    home, _managed = delegation_home
    (home / "config.yaml").write_text(_USER_DECLARING, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), _STALE_DECLARED) == _STALE_DECLARED


def test_a_pinned_child_still_gets_no_chain_under_an_authoritative_empty_chain(delegation_home):
    """A pinned child never borrows the parent's chain, so the disable leaves it with none (#80450)."""
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_DISABLES_CHILD_FAILOVER, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), _STALE_DECLARED, pinned=True) is None


def test_an_explicit_empty_declaration_still_short_circuits_without_authorization(delegation_home):
    """``[]`` on the route only ever removes failover, so it still needs no authorization at all.

    The absent case differs since R13: ``None`` DOES reach the authorization read now, and a
    failed read there must keep inheriting — no authoritative answer was observed — which is what
    the second assertion pins under a broken managed file.
    """
    _home, managed = delegation_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_caches()
    assert _child_chain(_Parent(_PARENT_CHAIN), []) is None
    assert _child_chain(_Parent(_PARENT_CHAIN), None) == _PARENT_CHAIN
