"""R11: the bypass must suppress the LEGACY chain key too, and a real ``chat()`` turn must honour it.

Two R10 reviewer lanes independently found that the R10 repair stops short in one place and was
only ever proven at a narrower surface than claimed:

- **R10-1** — ``hermes_cli/oneshot._oneshot_resolution_config`` replaces only
  ``cfg["fallback_providers"]`` and leaves ``cfg["fallback_model"]`` from fail-open
  ``load_config()`` in place. The canonical normalizer merges BOTH keys
  (``hermes_cli/fallback_config.py:111``), so the resolution-time walker (#81209,
  ``hermes_cli/runtime_provider.resolve_runtime_with_fallback``) can still return a
  deliberately-ignored user entry BEFORE ``AIAgent`` exists. The R10 arm only declared the chain
  under the new key, so it could not see this.
- **R9 item 5 / R10 §6.3** — the R10 lifecycle arms bind
  ``CLIChatTurnMixin._sync_fallback_chain_with_config`` onto a stand-in and call it directly.
  Production reaches that statement from ``chat()`` (``cli_chat_turn_mixin.py:94``), before
  ``agent_thread.start()``. This file drives the REAL ``chat()`` instead, so the turn's own
  ordering (credentials → route → ``_init_agent`` → sync → stage → agent thread) is what is
  asserted, with the model call and the terminal rendering as the only stubs.

Safe mode suppresses the USER layer only. The MANAGED (administrator) layer is mandatory policy,
still applies, and is still read fail-closed, so every negative arm here is paired with a positive
one that fails if the fix degenerates into "no failover under the flag".

Temp HERMES_HOME + HERMES_MANAGED_DIR, no network, no real model call, no real terminal.
"""

from __future__ import annotations

import threading

import pytest

# Legacy-key-only user chain: the shape R10-1 leaves live.
_USER_LEGACY_ONLY = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_model:\n"
    "  - provider: openrouter\n    model: legacy/user\n"
)
# Both keys, the realistic migrated-but-not-cleaned config.
_USER_BOTH_KEYS = (
    "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
    "fallback_providers:\n"
    "  - provider: deepinfra\n    model: new/user\n"
    "fallback_model:\n"
    "  - provider: openrouter\n    model: legacy/user\n"
)
_MANAGED_LEGACY_CHAIN = "fallback_model:\n  - provider: groq\n    model: managed/legacy\n"
_MANAGED_CHAIN = "fallback_providers:\n  - provider: groq\n    model: managed/model\n"

_MANAGED_LEGACY_ONLY = [{"provider": "groq", "model": "managed/legacy"}]
_MANAGED_ONLY = [{"provider": "groq", "model": "managed/model"}]
_USER_LEGACY_ENTRY = [{"provider": "openrouter", "model": "legacy/user"}]


def _clear_config_caches_only():
    """Every CONFIG cache, leaving the shared per-home retention record intact.

    The retention arm below has to make two clean observations (bypassed and not) and only then
    break the managed file, so it must drop the config caches between them WITHOUT erasing the
    record it is about to assert on.
    """
    from hermes_cli import config, config_effective, managed_scope
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()
    config_effective._EFFECTIVE_CACHE.clear()
    config_effective._LAST_GOOD_USER_RAW.clear()
    managed_scope.invalidate_managed_cache()


def _clear_caches():
    """Every config + retention slot, i.e. a fresh process's state."""
    from gateway import run_config_loaders
    _clear_config_caches_only()
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
    (home / "config.yaml").write_text(_USER_LEGACY_ONLY, encoding="utf-8")
    _clear_caches()
    try:
        yield home, managed
    finally:
        _clear_caches()


def _apply_the_real_flag(monkeypatch, *, safe_mode=False, ignore_user_config=False,
                         keep_retention=False):
    """Set the bypass through the production statements in ``hermes_cli/main.py``.

    Never ``monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")`` directly: that would pass even
    if the flags stopped setting it. The companion variables are pinned to ``"0"`` first so an
    implementation keyed on ``HERMES_SAFE_MODE``/``HERMES_IGNORE_RULES`` cannot pass the standalone
    ``--ignore-user-config`` arm. *keep_retention* drops only the config caches, for the arm that
    must switch question mid-test without erasing the record it is asserting on.
    """
    import os
    from argparse import Namespace

    from hermes_cli.main import _apply_safe_mode, _apply_user_config_bypass
    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "0")
    monkeypatch.setenv("HERMES_SAFE_MODE", "0")
    monkeypatch.setenv("HERMES_IGNORE_RULES", "0")
    args = Namespace(safe_mode=safe_mode, ignore_user_config=ignore_user_config)
    _apply_safe_mode(args)
    _apply_user_config_bypass(args)
    assert os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1", (
        "the flag did not set the config bypass; this arm would assert nothing")
    _clear_config_caches_only() if keep_retention else _clear_caches()


# ------------------------------------------- R10-1: the legacy key on the resolution-time route


def _resolution_chain():
    """The chain the resolution-time walker would actually walk for a one-shot run.

    ``_run_agent`` hands ``_oneshot_resolution_config()`` to ``resolve_runtime_with_fallback``
    (``oneshot.py:586``), which walks ``get_fallback_chain(config)`` — the canonical normalizer
    that merges ``fallback_providers`` AND legacy ``fallback_model``. Asserting on the normalizer's
    answer, not on one key, is what makes this arm see R10-1.
    """
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.oneshot import _oneshot_resolution_config
    return get_fallback_chain(_oneshot_resolution_config())


def test_the_resolution_route_suppresses_a_legacy_only_user_chain_under_safe_mode(cli_home, monkeypatch):
    """R10-1: ``hermes --safe-mode -z`` with a legacy-only user chain.

    The user file declares its chain under ``fallback_model`` alone. R10 replaced only
    ``fallback_providers``, so the normalizer still merged the ignored legacy entry and the
    resolution-time walker could return it before ``AIAgent`` was ever constructed.
    """
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    chain = _resolution_chain()
    assert chain in ([], None), f"--safe-mode -z leaked the legacy user chain into resolution: {chain}"


def test_the_resolution_route_suppresses_the_legacy_key_alongside_the_new_one(cli_home, monkeypatch):
    """Both keys present: suppressing one and leaving the other is the R10-1 defect exactly."""
    home, _managed = cli_home
    (home / "config.yaml").write_text(_USER_BOTH_KEYS, encoding="utf-8")
    _apply_the_real_flag(monkeypatch, ignore_user_config=True)
    chain = _resolution_chain()
    assert chain in ([], None), f"--ignore-user-config left a user entry in resolution: {chain}"


def test_the_resolution_route_still_serves_a_managed_legacy_chain_under_the_bypass(cli_home, monkeypatch):
    """Mandatory policy survives, including when the ADMIN declares it under the legacy key.

    The negative arms are satisfiable by "drop both keys, always". This one is not: the managed
    layer's own chain must still reach the resolution-time walker through the bypass.
    """
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_LEGACY_CHAIN, encoding="utf-8")
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    assert _resolution_chain() == _MANAGED_LEGACY_ONLY, "the bypass dropped mandatory managed policy"


def test_the_resolution_route_keeps_the_legacy_user_chain_without_the_flag(cli_home, monkeypatch):
    """Positive control: no flag, so the legacy key is a perfectly valid user chain."""
    assert _resolution_chain() == _USER_LEGACY_ENTRY


def test_the_resolution_config_still_carries_model_and_provider_resolution(cli_home, monkeypatch):
    """The bypass narrows the CHAIN, not the whole config.

    ``_run_agent`` uses this same dict for model/provider/credential resolution and reasoning, so
    returning ``{}`` under the flag would break one-shot runs instead of securing them.
    """
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    from hermes_cli.oneshot import _oneshot_resolution_config
    cfg = _oneshot_resolution_config()
    assert isinstance(cfg, dict) and cfg.get("model"), "the bypass emptied the resolution config"


# -------------------------------- R10-2: the declaration must be bound to the clean observation


class _Parent:
    def __init__(self, chain=None):
        self._fallback_chain = list(chain or [])


def _child_chain_from_production_selection(parent, *, pinned=False):
    """The two production statements ``delegate_task`` runs, in order.

    ``cfg = _load_config()`` then ``routing_cfg = cfg`` (``delegate_tool.py:480,492``) then
    ``_resolve_child_fallback_chain(parent, routing_cfg, pinned)`` via ``_resolve_child_runtime``
    (``delegate_tool_config.py:629``). Driving the real selection is what exposes the
    ``cli.CLI_CONFIG`` fall-through: ``_load_config`` skips ``load_config_readonly()`` under the
    bypass and reads the import-time fail-open snapshot instead.
    """
    from tools.delegate_tool_config import _load_config, _resolve_child_fallback_chain
    return _resolve_child_fallback_chain(parent, _load_config(), pinned)


def _child_chain(parent, declared, *, pinned=False):
    from tools.delegate_tool_config import _resolve_child_fallback_chain
    return _resolve_child_fallback_chain(parent, {"fallback_providers": declared}, pinned)


def test_a_declared_child_chain_from_the_import_time_snapshot_is_refused_under_the_bypass(cli_home, monkeypatch):
    """R10-2: the D-2 fail-open snapshot mechanism, still reachable on the delegation surface.

    ``cli.CLI_CONFIG`` is an import-time snapshot built by fail-open ``load_cli_config``. A process
    that imported ``cli`` before the flag was applied has the user's ``delegation`` section frozen
    in it, and under the bypass ``_load_config()`` prefers exactly that snapshot. The R10 gate then
    performed a clean ROOT observation, discarded it, and returned True — so the declaration was
    installed on a brand-new child agent although nothing in the clean, bypassed configuration
    authorized it.
    """
    import cli as cli_mod
    poison = {"delegation": {"fallback_providers": [{"provider": "poisoned", "model": "stale/snapshot"}]}}
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", poison, raising=False)
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    chain = _child_chain_from_production_selection(_Parent())
    assert chain in ([], None), f"delegation installed the import-time snapshot's chain: {chain}"


def test_the_snapshot_declaration_falls_back_to_the_parents_clean_chain(cli_home, monkeypatch):
    """Fail-closed here means "drop to the vetted default", not "destroy failover".

    The parent's chain already came from a fail-closed, bypass-aware read, so an unpinned child
    inherits it rather than the unauthorized declaration.
    """
    import cli as cli_mod
    poison = {"delegation": {"fallback_providers": [{"provider": "poisoned", "model": "stale/snapshot"}]}}
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", poison, raising=False)
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    assert _child_chain_from_production_selection(_Parent(_MANAGED_ONLY)) == _MANAGED_ONLY


def test_a_declaration_the_repaired_config_no_longer_authorizes_is_refused(cli_home, monkeypatch):
    """The gate must check the repaired POLICY, not merely that some read succeeded.

    A declaration obtained while the managed file was unreadable must not survive a later clean
    repair that suppresses ``delegation.fallback_providers``. R10's gate asked only whether a root
    read succeeded, so a clean-but-suppressing answer authorized the stale declaration.
    """
    _home, managed = cli_home
    (managed / "config.yaml").write_text("delegation:\n  fallback_providers: []\n", encoding="utf-8")
    _clear_caches()
    stale = [{"provider": "openrouter", "model": "revoked/entry"}]
    chain = _child_chain(_Parent(), stale)
    assert chain in ([], None), f"a declaration the clean config suppresses was installed: {chain}"


def test_a_declared_chain_the_clean_config_authorizes_still_installs(cli_home, monkeypatch):
    """Positive control: a legitimately declared child chain keeps working.

    This is the arm that fails if the fix degenerates into "never install a declared chain".
    """
    home, _managed = cli_home
    (home / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  default: claude-sonnet-4\n"
        "delegation:\n  fallback_providers:\n    - provider: openrouter\n      model: declared/child\n",
        encoding="utf-8")
    _clear_caches()
    declared = [{"provider": "openrouter", "model": "declared/child"}]
    assert _child_chain_from_production_selection(_Parent()) == declared
    assert _child_chain(_Parent(), declared) == declared


def test_a_pinned_child_still_gets_no_chain_when_the_declaration_is_refused(cli_home, monkeypatch):
    """A pinned child never borrows the parent chain, so a refusal leaves it with none."""
    _home, managed = cli_home
    (managed / "config.yaml").write_text("delegation:\n  fallback_providers: []\n", encoding="utf-8")
    _clear_caches()
    stale = [{"provider": "openrouter", "model": "revoked/entry"}]
    assert _child_chain(_Parent(_MANAGED_ONLY), stale, pinned=True) in ([], None)


def test_an_explicit_empty_declaration_and_an_absent_one_are_unchanged(cli_home, monkeypatch):
    """``[]`` on the route only ever removes failover and needs no gate. Since R13 the absent/null
    route DOES consult the owner's clean config (an authoritative answer binds it); with no managed
    disable or declaration in this fixture, the consult has no answer and inheritance stands."""
    assert _child_chain(_Parent(_MANAGED_ONLY), []) is None
    assert _child_chain(_Parent(_MANAGED_ONLY), None) == _MANAGED_ONLY


# -------------------------------------------------- R9 item 5: a real ``CLIChatTurnMixin.chat()``


class _FakeAgent:
    """The agent object ``chat()`` drives, with the model call as the only stub.

    ``_fallback_chain`` is the slot ``GatewayRunner._apply_fallback_chain_to_agent`` writes and the
    slot ``agent/turn_api_error`` would walk on a primary failure, so recording it at
    ``run_conversation`` entry is the observation point that matters: whatever is there when the
    model call begins is what the activation seam could install.
    """

    def __init__(self):
        self._fallback_chain = []
        self.chain_at_model_call = None
        self.provider = "anthropic"
        self.model = "claude-sonnet-4"
        self._session_messages = []
        self._session_persist_lock = threading.Lock()
        self._pending_cli_user_message = {}
        self._persist_user_message_idx = None
        self._persist_user_message_override = None
        self._persist_user_message_timestamp = None
        self._notification_config = {}
        self._active_children = []
        self._interrupt_requested = False
        self.ran = False

    def run_conversation(self, **kwargs):
        # The external API boundary. No network, no provider SDK, no tool execution.
        self.ran = True
        self.chain_at_model_call = list(self._fallback_chain or [])
        return {"final_response": "ok", "messages": [], "api_calls": 1,
                "completed": True, "failed": False}

    def interrupt(self, _msg):
        self._interrupt_requested = True

    def _summarize_api_error(self, exc):
        return str(exc)[:300]


class _CLITurn:
    """A ``HermesCLI``-shaped host for the REAL ``chat()``.

    Only the terminal/rendering surface and ``_init_agent`` are stubbed; ``chat()`` itself,
    ``_sync_fallback_chain_with_config``, ``_chat_route_images``,
    ``_chat_expand_context_references``, ``_chat_stage_user_message``, the notification policy
    snapshot, the real agent thread and ``_chat_run_agent`` all execute as written.
    """

    def __init__(self):
        import queue as _queue

        from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
        self._mixin = CLIChatTurnMixin
        self.chat = CLIChatTurnMixin.chat.__get__(self)
        self._sync_fallback_chain_with_config = (
            CLIChatTurnMixin._sync_fallback_chain_with_config.__get__(self))
        self._chat_route_images = CLIChatTurnMixin._chat_route_images.__get__(self)
        self._chat_expand_context_references = (
            CLIChatTurnMixin._chat_expand_context_references.__get__(self))
        self._chat_stage_user_message = CLIChatTurnMixin._chat_stage_user_message.__get__(self)
        self._chat_run_agent = CLIChatTurnMixin._chat_run_agent.__get__(self)
        self._chat_release_turn_audio = CLIChatTurnMixin._chat_release_turn_audio.__get__(self)

        # What the (already fail-closed, already bypass-aware) constructor handed this CLI.
        self._fallback_model = []
        self.agent = None
        self.conversation_history = []
        self.session_id = "r11-session"
        self.model = "claude-sonnet-4"
        self.provider = "anthropic"
        self.base_url = ""
        self.api_key = ""
        self.requested_provider = ""
        self.console = None
        self.show_timestamps = False
        self.timestamp_format = "%H:%M"
        self.streaming_enabled = False
        self._interrupt_queue = _queue.Queue()
        self._pending_input = _queue.Queue()
        self._clarify_state = None
        self._clarify_freetext = None
        self._voice_mode = False
        self._voice_continuous = False
        self._voice_tts = None
        self._last_turn_interrupted = False
        self._active_agent_route_signature = "sig"
        self._reasoning_shown_this_turn = False
        self._pending_model_switch_note = None
        self._pending_skills_reload_note = None
        self._pending_moa_config = None
        self._pending_moa_disable_after_turn = False
        self._pending_one_turn_model_restore = None
        self.rendered = None

    # --- stubs: credentials/route/agent construction (covered by their own suites) -------------
    def _ensure_runtime_credentials(self):
        return True

    def _resolve_turn_agent_config(self, _message):
        return {"signature": "sig", "model": None, "runtime": None, "request_overrides": None}

    def _init_agent(self, **_kwargs):
        if self.agent is None:
            self.agent = _FakeAgent()
            self.conversation_history = self.agent._session_messages
        return True

    # --- stubs: terminal/rendering only --------------------------------------------------------
    def _secret_capture_callback(self, *_a, **_k):
        return None

    _sudo_password_callback = _approval_callback = _secret_capture_callback
    _vault_unlock_callback = _vault_save_login_callback = _vault_code_callback = _secret_capture_callback

    def _reset_stream_state(self):
        return None

    def _chat_setup_turn_audio(self, *_a, **_k):
        return None

    def _invalidate(self, **_k):
        return None

    def _flush_credit_notices(self):
        return None

    def _restore_model_runtime_snapshot(self, _snap):
        return None

    def _chat_monitor_agent_thread(self, _turn, agent_thread):
        agent_thread.join(timeout=30)
        return None

    def _chat_settle_turn(self, _turn):
        return None

    def _chat_render_turn(self, turn, _thread, _interrupt):
        self.rendered = turn.result
        return (turn.result or {}).get("final_response")

    def run_one_real_turn(self, message="hello"):
        response = self.chat(message)
        return response, self.agent


def test_a_real_chat_turn_runs_with_the_user_chain_suppressed_under_safe_mode(cli_home, monkeypatch):
    """R9 item 5: the first turn of a real ``chat()``, not just the sync statement.

    ``chat()`` reaches ``_sync_fallback_chain_with_config`` at ``cli_chat_turn_mixin.py:94``,
    before ``agent_thread.start()``, so the chain the model call sees is the one this turn
    installed. Under ``--safe-mode`` that must not be the user's.
    """
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    response, agent = _CLITurn().run_one_real_turn()
    assert agent.ran, "the turn never reached the model call; this arm would assert nothing"
    assert response == "ok"
    assert agent.chain_at_model_call in ([], None), (
        f"a real --safe-mode turn handed the user chain to the model call: {agent.chain_at_model_call}")


def test_a_real_chat_turn_still_applies_managed_policy_under_safe_mode(cli_home, monkeypatch):
    """The paired positive arm: mandatory managed policy reaches the real turn's model call."""
    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_CHAIN, encoding="utf-8")
    _apply_the_real_flag(monkeypatch, safe_mode=True)
    _response, agent = _CLITurn().run_one_real_turn()
    assert agent.ran
    assert agent.chain_at_model_call == _MANAGED_ONLY, (
        f"a real --safe-mode turn dropped managed policy: {agent.chain_at_model_call}")


def test_a_real_chat_turn_without_the_flag_adopts_the_user_chain(cli_home, monkeypatch):
    """Positive control for #95066: an ordinary turn still adopts the user's chain edit."""
    _response, agent = _CLITurn().run_one_real_turn()
    assert agent.ran
    assert agent.chain_at_model_call == _USER_LEGACY_ENTRY


# ------------------------------------------- R10 §6.4: retention partitioning on a FAILED read


def test_a_failed_read_serves_the_retained_answer_for_its_own_question_only(cli_home, monkeypatch):
    """The retention partition, exercised through the FAILED-read path.

    R10 proved ``_chain_retention_key`` by static inspection only: its test cleared the cache and
    performed another CLEAN observation, which cannot detect key aliasing. Here both questions are
    answered cleanly first (non-bypassed → the user chain; bypassed → the managed-only answer),
    then the managed file is broken so the next read must take the retention path. Each question
    must get its OWN retained answer back.
    """
    from gateway.run_config_loaders import (load_fail_closed_fallback_chain,
                                            record_clean_fallback_chain_observation)
    from hermes_cli import managed_scope
    from hermes_constants import get_hermes_home

    _home, managed = cli_home
    (managed / "config.yaml").write_text(_MANAGED_CHAIN, encoding="utf-8")
    _clear_caches()
    # Clean, NON-bypassed observation: user chain + managed entry under the plain key.
    plain_clean = record_clean_fallback_chain_observation(get_hermes_home())
    assert plain_clean, "the non-bypassed clean read produced no chain; this arm would prove nothing"

    _apply_the_real_flag(monkeypatch, safe_mode=True, keep_retention=True)
    bypassed_clean = record_clean_fallback_chain_observation(get_hermes_home())
    assert bypassed_clean == _MANAGED_ONLY

    # Break the managed file: both questions now go through the retention path.
    (managed / "config.yaml").write_text("fallback_providers: [unclosed\n", encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    from hermes_cli import config_effective
    config_effective._EFFECTIVE_CACHE.clear()

    assert load_fail_closed_fallback_chain(get_hermes_home()) == _MANAGED_ONLY, (
        "the bypassed failed read served an answer from the non-bypassed slot")

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    config_effective._EFFECTIVE_CACHE.clear()
    assert load_fail_closed_fallback_chain(get_hermes_home()) == plain_clean, (
        "the non-bypassed failed read served the bypassed slot's narrower answer")
