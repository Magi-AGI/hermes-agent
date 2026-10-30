"""R9/R5-6 (option 2): the two remaining CLI chain constructors must fail closed.

R5-2 closed the gateway, cron and TUI constructors onto the one shared primitive
``gateway/run_config_loaders.load_fail_closed_fallback_chain``. Two construction sites outside that
slice still read the chain through fail-OPEN loaders, and both feed the same ungated activation seam
(``agent/chat_completion_helpers.try_activate_fallback``, reachable from
``agent/turn_api_error.py:366``):

- ``hermes_cli/cli_init_mixin.py`` — ``get_fallback_chain(CLI_CONFIG)`` at CLI startup. ``CLI_CONFIG``
  comes from ``cli_config_load.load_cli_config``, whose managed overlay is
  ``managed_scope.apply_managed_overlay`` — documented **fail-open** ("returns ``config`` unchanged
  ... on any error"). The per-turn resync ``cli_chat_turn_mixin._sync_fallback_chain_with_config``
  IS fail-closed, but it runs AFTER ``_init_agent``, so the first agent is born with the startup
  chain and ``_resolve_runtime_with_fallback`` already walks it during resolution
  (``cli_agent_setup_mixin.py:369``).
- ``hermes_cli/oneshot.py`` — ``get_fallback_chain(load_config())``. On a cold managed-read failure
  with no merged last-known-good record, ``load_config`` returns the user configuration
  (``hermes_cli/config.py:2592``), and a one-shot run has no later turn to resync.

With a managed ``fallback_providers: []`` that then stops parsing, both hand the caller the user's
**suppressed** chain. Verified by execution: ``load_config()`` and ``load_cli_config()`` both return
the full two-entry user chain under exactly that condition.

Each surface carries a positive arm (the managed narrowing IS applied on a clean read), so the fix
cannot be "disable failover wholesale", and a retention arm, so fail-closed does not mean "destroy
the chain the admin layer last allowed".

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no real AIAgent, no model call.
"""

from __future__ import annotations

import os

import pytest

_USER_CHAIN = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: openrouter\n    model: some/model\n"
    "  - provider: deepinfra\n    model: other/model\n"
)
_MANAGED_NARROWS = "fallback_providers:\n  - provider: openrouter\n    model: some/model\n"
_MANAGED_BROKEN = "fallback_providers: [unclosed\n"

_NARROWED = [{"provider": "openrouter", "model": "some/model"}]
_USER_FULL = [{"provider": "openrouter", "model": "some/model"},
              {"provider": "deepinfra", "model": "other/model"}]


def _clear_config_caches():
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def _clear_caches():
    """``_clear_config_caches`` plus every retention slot (a fresh process's state).

    Tolerant of the slots being absent so a red-first run fails on the BEHAVIOUR under test rather
    than on a missing symbol.
    """
    from gateway import run_config_loaders
    _clear_config_caches()
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
    (home / "config.yaml").write_text(_USER_CHAIN, encoding="utf-8")
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


# --------------------------------------------------------------------------- classic CLI startup


def _cli_startup_chain(monkeypatch):
    """The chain ``HermesCLI.__init__`` captures, via the real production statement.

    ``_init_prompt_and_reasoning`` is the ``__init__`` phase that owns the assignment
    (``self._fallback_model = ...``); the rest of ``__init__`` (session store, prompt_toolkit, the
    agent) is irrelevant here and would need a terminal. ``CLI_CONFIG`` is replaced with a freshly
    loaded one so each arm reads the config files as written, not the import-time snapshot.
    """
    import cli as cli_mod
    from hermes_cli.cli_config_load import load_cli_config
    from hermes_cli.cli_init_mixin import CLIInitMixin
    from hermes_constants import get_hermes_home

    _clear_config_caches()

    class _Surface(CLIInitMixin):
        def __init__(self):
            self.model = "claude-sonnet-4"

    # ``cli._hermes_home`` is an import-time constant and ``load_cli_config`` reads it
    # (``cli_config_load.py:264``), so without this the legacy loader would answer from whichever
    # home was live when ``cli`` first imported rather than this test's. Re-point it at the live
    # ``HERMES_HOME`` so BOTH loaders in play — the legacy one the safe-mode bypass keeps
    # authoritative, and the fail-closed primitive, which resolves the home per call — read the
    # same files.
    monkeypatch.setattr(cli_mod, "_hermes_home", get_hermes_home())
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", load_cli_config())
    surface = _Surface()
    surface._init_prompt_and_reasoning(None)
    return surface._fallback_model


def test_cli_startup_applies_the_managed_narrowing_when_the_config_reads_cleanly(cli_home, monkeypatch):
    """Positive arm: the fix must honour the admin layer, not disable failover wholesale."""
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")

    chain = _cli_startup_chain(monkeypatch)
    assert chain == _NARROWED, f"CLI startup did not apply the managed narrowing: {chain!r}"
    assert chain != _USER_FULL, "CLI startup served the user's unconstrained chain"


def test_a_cold_cli_startup_gets_no_chain_when_the_managed_config_does_not_read(cli_home, monkeypatch):
    """R5-6(cli_init_mixin): a failed managed read must not publish the suppressed user chain.

    Nothing is retained for this home (a cold process), so the only honest answer is "no chain" —
    the same direction every constructor R5-2 converted fails in.
    """
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    chain = _cli_startup_chain(monkeypatch)
    assert chain != _USER_FULL, (
        f"the CLI startup chain is the user's unconstrained chain ({chain!r}) while the managed "
        "config did not read — apply_managed_overlay's fail-open path applied the suppression as "
        "an empty overlay")
    assert not chain, f"a cold CLI startup must get no chain on a failed read, got {chain!r}"


def test_cli_startup_serves_the_last_clean_managed_answer_across_a_later_failure(cli_home, monkeypatch):
    """Fail-closed must not mean "destroy the chain": the retained clean answer is served.

    Also pins that this surface shares the ONE per-home retention rather than adding a cache of its
    own — the record written by the clean startup above is what answers here.
    """
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")
    assert _cli_startup_chain(monkeypatch) == _NARROWED

    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")
    _clear_config_caches()  # only the config caches: the retention IS the state under test

    chain = _cli_startup_chain(monkeypatch)
    assert chain == _NARROWED, (
        f"the clean managed answer was not retained for the next CLI startup: {chain!r}")
    assert chain != _USER_FULL, "the user's unconstrained chain was served instead of the retained one"


def test_the_cli_per_turn_resync_cannot_repair_the_startup_chain_before_the_first_turn(cli_home, monkeypatch):
    """Why "the per-turn resync is fail-closed" does not close this item.

    ``cli_chat_turn_mixin._sync_fallback_chain_with_config`` runs AFTER ``_init_agent``, and on a
    FAILED read it deliberately keeps the current chain (#95066). So when startup captured a
    suppressed chain, the resync preserves it rather than repairing it: the constructor has to fail
    closed itself. This arm pins that reasoning so a later reader does not re-credit the resync.
    """
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin

    class _Surface(CLIChatTurnMixin):
        def __init__(self, chain):
            self._fallback_model = chain

    class _Agent:
        def __init__(self, chain):
            self._fallback_chain = list(chain)
            self._fallback_model = chain[0] if chain else None
            self._fallback_activated = False
            self._fallback_index = 0
            self._rate_limited_until = 0

    # The state the unfixed startup leaves behind.
    surface, agent = _Surface(list(_USER_FULL)), _Agent(_USER_FULL)
    _clear_config_caches()
    surface._sync_fallback_chain_with_config(agent)

    assert agent._fallback_chain == _USER_FULL, (
        "the per-turn resync changed the chain on a failed read; that is a DIFFERENT contract than "
        "the one documented (#95066 keeps the last known-good chain) and this arm's premise")
    startup_chain = _cli_startup_chain(monkeypatch)
    assert not startup_chain, (
        f"the startup constructor served {startup_chain!r}, a chain the resync cannot repair "
        "because a failed read makes it keep whatever startup captured")


# --------------------------------------------------------------- --safe-mode / --ignore-user-config


def _apply_the_real_flag(monkeypatch, **flag):
    """Set ``HERMES_IGNORE_USER_CONFIG`` the way the real CLI entry point does.

    Driving ``hermes_cli.main._apply_safe_mode`` / ``_apply_user_config_bypass`` rather than
    setting the variable by hand keeps this pinned to the FLAGS (``--safe-mode``,
    ``--ignore-user-config``, ``hermes_cli/_parser.py:185,189``); a rename of the internal variable
    cannot make these arms silently vacuous. Both write ``os.environ`` directly, so the key is
    registered with monkeypatch first to get it restored.
    """
    from argparse import Namespace

    from hermes_cli.main import _apply_safe_mode, _apply_user_config_bypass

    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "0")
    monkeypatch.setenv("HERMES_SAFE_MODE", "0")
    monkeypatch.setenv("HERMES_IGNORE_RULES", "0")
    args = Namespace(**{"safe_mode": False, "ignore_user_config": False, **flag})
    _apply_safe_mode(args)
    _apply_user_config_bypass(args)
    assert os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1", (
        f"{flag} did not set the config bypass; this arm would assert nothing")


def test_safe_mode_still_suppresses_the_startup_fallback_chain(cli_home, monkeypatch):
    """``--safe-mode`` means "run with NO user customization" — including the fallback chain.

    Regression for the bug the R5-6 conversion introduced and nothing caught: ``--safe-mode`` is
    implemented ONLY by ``load_cli_config``, which swaps the user ``config.yaml`` out entirely
    (``hermes_cli/cli_config_load.py:266``). The fail-closed primitive reads ``config.yaml``
    directly and has no such bypass, so routing startup through it unconditionally turned safe mode
    from "no chain" into "the user's full chain" — i.e. a troubleshooting mode that still failed
    over to user-configured providers, and the one mode whose entire purpose is to prove the user's
    config is not involved.
    """
    _apply_the_real_flag(monkeypatch, safe_mode=True)

    chain = _cli_startup_chain(monkeypatch)
    assert not chain, (
        f"--safe-mode served the fallback chain {chain!r} from the user's config.yaml; safe mode "
        "must disable user customization, and only load_cli_config implements that bypass")


def test_ignore_user_config_still_suppresses_the_startup_fallback_chain(cli_home, monkeypatch):
    """``--ignore-user-config`` alone carries the same contract, without the other safe-mode flags.

    Separate arm because ``_apply_safe_mode`` also sets ``HERMES_SAFE_MODE`` and
    ``HERMES_IGNORE_RULES``: a fix that keyed on either of those would leave the standalone flag
    regressed while the safe-mode arm above went green.
    """
    _apply_the_real_flag(monkeypatch, ignore_user_config=True)

    chain = _cli_startup_chain(monkeypatch)
    assert not chain, (
        f"--ignore-user-config served the fallback chain {chain!r} from the user's config.yaml")


def test_without_the_bypass_the_same_user_config_still_yields_its_chain(cli_home, monkeypatch):
    """Positive control: the suppression above is the FLAG, not a chain that never loads.

    Same fixture, same ``config.yaml``, no flag. Without this arm both tests above would pass
    against a startup path that had simply stopped reading the chain at all.
    """
    assert os.environ.get("HERMES_IGNORE_USER_CONFIG") != "1"

    chain = _cli_startup_chain(monkeypatch)
    assert chain == _USER_FULL, (
        f"the unflagged startup chain is {chain!r}, not the user's configured chain — the "
        "safe-mode arms above would then be asserting nothing")


# --------------------------------------------------------------------------- one-shot (`hermes -q`)


def _oneshot_chain(monkeypatch):
    """The ``fallback_model`` kwarg ``hermes_cli.oneshot._run_agent`` hands to ``AIAgent``.

    The real ``_run_agent`` is driven with every out-of-process collaborator stubbed (no session
    store, no MCP discovery, no credential resolution, no model call), so the assertion is on the
    production construction statement itself (``oneshot.py:591``).
    """
    import hermes_cli.oneshot as oneshot_mod

    captured: dict = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __setattr__(self, name, _value):
            pass

        def run_conversation(self, _prompt, conversation_history=None):
            return {"final_response": "ok", "session_id": "s"}

        def close(self):
            pass

    _clear_config_caches()
    monkeypatch.setattr(oneshot_mod, "_create_session_db_for_oneshot", lambda: None)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda _cfg, _p: [])
    monkeypatch.setattr("hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
                        lambda **_kw: None)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider",
                        lambda **kw: {"api_key": "k", "base_url": None,
                                      "provider": kw.get("requested") or "anthropic",
                                      "api_mode": "chat", "credential_pool": None})
    monkeypatch.setattr("run_agent.AIAgent", _FakeAgent)

    oneshot_mod._run_agent("ping")
    return captured["fallback_model"]


def test_oneshot_applies_the_managed_narrowing_when_the_config_reads_cleanly(cli_home, monkeypatch):
    """Positive arm for the one-shot surface."""
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")

    chain = _oneshot_chain(monkeypatch)
    assert chain == _NARROWED, f"one-shot did not apply the managed narrowing: {chain!r}"
    assert chain != _USER_FULL, "one-shot served the user's unconstrained chain"


def test_a_cold_oneshot_gets_no_chain_when_the_managed_config_does_not_read(cli_home, monkeypatch):
    """R5-6(oneshot): ``load_config()`` serves the user config on a cold managed failure.

    ``hermes_cli/config.py:2592`` keeps the last merged config when one exists; on a cold process
    there is none, so the user's suppressed chain was handed straight into ``AIAgent``. A one-shot
    run has no later turn to resync, so this is the whole lifetime of that agent.
    """
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    chain = _oneshot_chain(monkeypatch)
    assert chain != _USER_FULL, (
        f"one-shot constructed its agent with the user's unconstrained chain ({chain!r}) while the "
        "managed config did not read")
    assert not chain, f"a cold one-shot must get no chain on a failed read, got {chain!r}"


def test_oneshot_serves_the_last_clean_managed_answer_across_a_later_failure(cli_home, monkeypatch):
    """The one-shot surface reads the SAME per-home retention, not a fourth cache."""
    home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_NARROWS, encoding="utf-8")

    # Record the clean answer through the shared primitive (what any earlier surface in this
    # process would have done), then break the managed file.
    from gateway.run_config_loaders import load_fail_closed_fallback_chain

    assert load_fail_closed_fallback_chain(home) == _NARROWED
    (managed / "config.yaml").write_text(_MANAGED_BROKEN, encoding="utf-8")

    chain = _oneshot_chain(monkeypatch)
    assert chain == _NARROWED, (
        f"one-shot did not read the shared retention: {chain!r}. Fail-closed must serve the last "
        "clean managed answer, not drop failover entirely while a record is live")
    assert chain != _USER_FULL, "the user's unconstrained chain was served instead of the retained one"
