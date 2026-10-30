"""R10: ``--safe-mode`` / ``--ignore-user-config`` must suppress the user chain THROUGH USE.

R9 converted the two CLI constructors onto the shared fail-closed primitive
(``gateway/run_config_loaders.load_fail_closed_fallback_chain``) and added a safe-mode arm to
``hermes_cli/cli_init_mixin._fail_closed_fallback_chain_for_cli``. Both R9 reviewer lanes then found
that the repair stops at construction, and that the bypass itself was implemented in the wrong
layer:

- **F1 / D-1** — ``CLIChatTurnMixin.chat()`` calls ``_sync_fallback_chain_with_config`` at
  ``cli_chat_turn_mixin.py:80``, BEFORE the agent thread starts, i.e. before the first model call of
  the FIRST turn. That sync read ``load_user_config_effective(fail_closed=True)``
  (``cli_chat_turn_mixin.py:41``), which has no concept of the bypass, so it re-installed the user's
  full ``fallback_providers`` onto both ``self._fallback_model`` and ``agent._fallback_chain``.
  ``hermes_cli/oneshot.py`` had the sibling gap: ``_fail_closed_fallback_chain_for_oneshot`` (``:526``)
  and the resolution-time ``resolve_runtime_with_fallback(load_config(), ...)`` walk (``:550``) both
  ignored the flag. Constructor-only coverage is why R9's three flag arms passed while the defect
  was reachable: they stop at ``_init_prompt_and_reasoning``.
- **D-2** — the R9 safe-mode arm returned ``get_fallback_chain(CLI_CONFIG)``. ``cli.CLI_CONFIG`` is
  an IMPORT-TIME snapshot (``cli.py:326``) built by fail-open ``load_cli_config``
  (``cli_config_load.py:266,294``), so a process that imported ``cli`` before the flag was applied
  got the user's chain back, and an in-process managed revocation could not reach it at all. The
  bypass has to narrow, never re-open a fail-open read.

What a correct bypass means here, taken from the repo's existing policy rather than invented: the
flag suppresses the USER layer only (``cli_config_load.py:266`` substitutes the packaged
``cli-config.yaml`` for ``~/.hermes/config.yaml``; ``main.py:3023,3030`` set it for ``--safe-mode``
and ``--ignore-user-config``). The MANAGED (administrator) layer is mandatory policy and must STILL
apply, and must still be read fail-closed — safe mode is not an admin-policy escape hatch. So the
answer under the flag is "the managed layer's chain, or none", never "the user's chain" and never
"whatever a fail-open loader last cached".

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no real model call.
"""

from __future__ import annotations

import pytest

_USER_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
    "  - provider: deepinfra\n    model: other/model\n"
)
_MANAGED_CHAIN = "fallback_providers:\n  - provider: groq\n    model: managed/model\n"
_MANAGED_SUPPRESSES = "fallback_providers: []\n"
_MANAGED_BROKEN = "fallback_providers: [unclosed\n"

_USER_FULL = [{"provider": "openrouter", "model": "some/model"},
              {"provider": "deepinfra", "model": "other/model"}]
_MANAGED_ONLY = [{"provider": "groq", "model": "managed/model"}]


def _clear_caches():
    """Every config + retention slot, i.e. a fresh process's state.

    Tolerant of absent slots so a red-first run fails on the BEHAVIOUR under test rather than on a
    missing symbol.
    """
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


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


def _apply_the_real_flag(monkeypatch, *, safe_mode=False, ignore_user_config=False):
    """Set the bypass through the production statements in ``hermes_cli/main.py``.

    Never ``monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")`` directly: that would pass even if
    the flags stopped setting it. ``--safe-mode`` goes through ``_apply_safe_mode`` and
    ``--ignore-user-config`` through ``_apply_user_config_bypass``, the two real entry-point
    statements. The companion variables are pinned to ``"0"`` first so an implementation keyed on
    ``HERMES_SAFE_MODE``/``HERMES_IGNORE_RULES`` instead of the real one cannot pass the standalone
    ``--ignore-user-config`` arm.
    """
    from argparse import Namespace

    from hermes_cli.main import _apply_safe_mode, _apply_user_config_bypass
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "0")
    monkeypatch.setenv("HERMES_SAFE_MODE", "0")
    monkeypatch.setenv("HERMES_IGNORE_RULES", "0")
    args = Namespace(safe_mode=safe_mode, ignore_user_config=ignore_user_config)
    _apply_safe_mode(args)
    _apply_user_config_bypass(args)
    import os
    assert os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1", (
        "the flag did not set the config bypass; this arm would assert nothing")
    _clear_caches()


# --------------------------------------------------------- F1/D-1: the actual first turn's sync


class _TurnSurface:
    """The real ``_sync_fallback_chain_with_config`` bound onto a minimal CLI-shaped object.

    ``chat()`` itself needs a terminal, a session store and a live agent thread; the statement under
    test is the one ``chat()`` reaches at ``cli_chat_turn_mixin.py:80`` before ``agent_thread.start()``,
    so this drives exactly that statement against a stand-in agent. The agent stand-in carries the
    attribute ``_apply_fallback_chain_to_agent`` writes.
    """

    def __init__(self):
        from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
        self._sync = CLIChatTurnMixin._sync_fallback_chain_with_config.__get__(self)
        # What the (already fail-closed, already bypass-aware) constructor handed this CLI.
        self._fallback_model = []

    class _Agent:
        def __init__(self):
            self._fallback_chain = []

    def first_turn_sync(self):
        agent = self._Agent()
        self._sync(agent)
        return self._fallback_model, agent._fallback_chain


def test_first_turn_sync_keeps_the_user_chain_suppressed_under_safe_mode(cli_home, monkeypatch):
    """F1/D-1, ``--safe-mode``: the first turn must not re-install the ignored user chain."""
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    cli_chain, agent_chain = _TurnSurface().first_turn_sync()
    assert cli_chain in ([], None), f"--safe-mode leaked the user chain into the CLI: {cli_chain}"
    assert agent_chain in ([], None), f"--safe-mode leaked the user chain onto the agent: {agent_chain}"


def test_first_turn_sync_keeps_the_user_chain_suppressed_under_ignore_user_config(cli_home, monkeypatch):
    """F1/D-1, standalone ``--ignore-user-config`` (no ``HERMES_SAFE_MODE``, no ``HERMES_IGNORE_RULES``)."""
    _apply_the_real_flag(monkeypatch, ignore_user_config=True)
    cli_chain, agent_chain = _TurnSurface().first_turn_sync()
    assert cli_chain in ([], None), f"--ignore-user-config leaked the user chain into the CLI: {cli_chain}"
    assert agent_chain in ([], None), f"--ignore-user-config leaked the user chain onto the agent: {agent_chain}"


def test_first_turn_sync_still_applies_mandatory_managed_policy_under_safe_mode(cli_home, monkeypatch):
    """Safe mode suppresses the USER layer, NOT the administrator layer.

    The negative arms above are satisfiable by "return nothing, always". This one is not: the
    managed chain is mandatory policy and must survive the bypass, so the fix cannot be
    "disable failover wholesale under the flag".
    """
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_CHAIN, encoding="utf-8")
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    cli_chain, agent_chain = _TurnSurface().first_turn_sync()
    assert cli_chain == _MANAGED_ONLY, f"safe mode dropped mandatory managed policy: {cli_chain}"
    assert agent_chain == _MANAGED_ONLY


def test_first_turn_sync_without_the_flag_still_adopts_a_user_chain_edit(cli_home, monkeypatch):
    """Positive control: the bypass must not break the ordinary #95066 per-turn adoption."""
    cli_chain, agent_chain = _TurnSurface().first_turn_sync()
    assert cli_chain == _USER_FULL
    assert agent_chain == _USER_FULL


def test_first_turn_sync_under_the_flag_fails_closed_on_an_unreadable_managed_file(cli_home, monkeypatch):
    """A failed MANAGED read is a failed read under the bypass too (A44).

    The bypass must not become a path that skips the fail-closed managed check and hands back an
    unconstrained answer; and the #95066 contract still holds, so a failed read leaves the chain the
    caller already had rather than reading as "chain removed".
    """
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    surface = _TurnSurface()
    surface._fallback_model = [{"provider": "groq", "model": "managed/model"}]
    cli_chain, agent_chain = surface.first_turn_sync()
    assert cli_chain == [{"provider": "groq", "model": "managed/model"}], (
        f"a failed managed read under the bypass changed the chain: {cli_chain}")
    assert agent_chain == [], "a failed read must not publish anything onto the agent"


# ------------------------------------------------------------------ F1/D-1: the one-shot surface


def test_oneshot_constructor_chain_is_suppressed_under_safe_mode(cli_home, monkeypatch):
    """``hermes --safe-mode -z "..."``: the one-shot agent is born with no user chain.

    A one-shot run has no later turn, so the constructor's answer is the agent's whole life.
    """
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    from hermes_cli.oneshot import _fail_closed_fallback_chain_for_oneshot
    chain = _fail_closed_fallback_chain_for_oneshot()
    assert chain in ([], None), f"--safe-mode -z leaked the user chain: {chain}"


def test_oneshot_constructor_still_applies_managed_policy_under_safe_mode(cli_home, monkeypatch):
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_CHAIN, encoding="utf-8")
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    from hermes_cli.oneshot import _fail_closed_fallback_chain_for_oneshot
    assert _fail_closed_fallback_chain_for_oneshot() == _MANAGED_ONLY


def test_oneshot_resolution_time_walk_is_suppressed_under_safe_mode(cli_home, monkeypatch):
    """``oneshot.py:550`` hands ``load_config()`` to ``resolve_runtime_with_fallback``.

    That resolution-time walk (#81209) runs BEFORE ``AIAgent`` exists, so suppressing only the
    constructor leaves a second, earlier route onto the ignored user chain.
    """
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.oneshot import _oneshot_resolution_config
    assert get_fallback_chain(_oneshot_resolution_config()) in ([], None)


# ------------------------------------------------------------------------- D-2: no fail-open read


def test_the_bypass_does_not_read_the_import_time_cli_config_snapshot(cli_home, monkeypatch):
    """D-2: a stale/fail-open ``cli.CLI_CONFIG`` must not be able to supply a chain.

    ``cli.CLI_CONFIG`` is built once at import. Poisoning it here stands in for every process that
    imported ``cli`` before the flag was applied, and for a managed chain frozen across an
    in-process revocation. The bypass must answer from a live fail-closed read, so the poison must
    not appear.
    """
    import cli as cli_mod
    poison = {"fallback_providers": [{"provider": "poisoned", "model": "stale/snapshot"}]}
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", poison, raising=False)
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    from hermes_cli.cli_init_mixin import _fail_closed_fallback_chain_for_cli
    chain = _fail_closed_fallback_chain_for_cli()
    assert chain in ([], None), f"the bypass served the import-time CLI_CONFIG snapshot: {chain}"


def test_the_bypass_observes_an_in_process_managed_revocation(cli_home, monkeypatch):
    """The live read is what makes the revocation visible; a frozen snapshot could not see it."""
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_CHAIN, encoding="utf-8")
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    from hermes_cli.cli_init_mixin import _fail_closed_fallback_chain_for_cli
    assert _fail_closed_fallback_chain_for_cli() == _MANAGED_ONLY
    (managed / "config.yaml").write_text(_MANAGED_SUPPRESSES, encoding="utf-8")
    from hermes_cli import managed_scope
    managed_scope.invalidate_managed_cache()
    assert _fail_closed_fallback_chain_for_cli() in ([], None)


# ----------------------------------------------- the bypassed read must not poison a normal read


def test_a_bypassed_observation_does_not_poison_the_normal_per_home_retention(cli_home, monkeypatch):
    """The per-home retention is shared by every constructor; the bypass adds a second question.

    "What chain may a new agent have for home H?" now has two answers — with and without the user
    layer — and one slot cannot hold both. A bypassed observation recording ``None`` under the plain
    home key would make a later NON-bypassed read (another thread, the gateway in the same process)
    serve "no chain" on a failed managed read when the user's clean chain was the right answer.
    """
    from gateway.run_config_loaders import record_clean_fallback_chain_observation
    from hermes_constants import get_hermes_home

    _apply_the_real_flag(monkeypatch, safe_mode=True)
    assert record_clean_fallback_chain_observation(get_hermes_home()) in ([], None)

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    from hermes_cli import config_effective
    config_effective._EFFECTIVE_CACHE.clear()
    assert record_clean_fallback_chain_observation(get_hermes_home()) == _USER_FULL
