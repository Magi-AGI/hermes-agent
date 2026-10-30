"""Config/env loaders (busy modes, reasoning, service tier, timeouts, fallback) for GatewayRunner.

Split out of ``gateway/run.py``; bound onto ``GatewayRunner`` via the MRO.
``gateway.run`` internals are imported lazily inside method bodies (import cycle),
so ``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from gateway.config import Platform
from gateway.restart import (
    DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT, DEFAULT_GATEWAY_POST_INTERRUPT_GRACE_TIMEOUT,
    DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT, DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT, parse_cron_drain_timeout,
    parse_restart_after_turn_timeout, parse_restart_drain_timeout,
    launchd_service_label, parse_signal_interrupt_grace_timeout, read_launchd_exit_timeout_s,
    resolve_launchd_capped_drain,
)
from gateway.session import SessionSource
from gateway.session_state import SERVICE_TIER_UNSET as _SERVICE_TIER_UNSET
from hermes_cli.config import cfg_get, resolve_ephemeral_system_prompt_from_config
from hermes_cli.fallback_config import get_fallback_chain
from utils import is_truthy_value

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner  # noqa: F401
    from gateway.run_turn_runner import TurnRunner  # noqa: F401

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")

DEFAULT_HUMAN_DELAY_RANGE_MS: tuple[int, int] = (800, 2500)

# hermes_home_key -> the last CLEANLY read fallback chain for that home (None = the home has no
# chain). One process serves many profiles, so this is keyed per home, never a single slot: a
# runner-wide slot handed a secondary profile the default profile's chain. Read only when the
# config.yaml for that home does NOT read cleanly, so the admin layer's last allowed answer
# survives a torn write instead of the user's unconstrained chain being published
# (:meth:`GatewayRunner._load_fallback_model`, CA-P2/P3-6 round 3).
#
# ONE slot per home for ONE fact, shared by the constructor and the per-turn refresh. They used to
# keep separate caches — this module global and ``self._fallback_model_by_home`` — and never
# cross-updated, so a chain the admin layer had suppressed could be refreshed away on the instance
# while this slot still held the older, WIDER answer; the next managed failure served that revoked
# chain to a newly constructed API agent, which installs it ungated
# (``agent/chat_completion_helpers.py``). Both writers now record here, including a successful
# EMPTY observation — a clean suppression is retained AS a suppression, which is the whole point
# (R4-2). Writes are ``_record_clean_fallback_chain``; reads are ``_retained_fallback_chain``.
_LAST_CLEAN_FALLBACK_CHAIN: Dict[str, Optional[List[Any]]] = {}

# How long a retained chain may be served after the config for that home stopped reading cleanly.
# Unbounded retention means a permanently broken managed file serves the stale chain for the
# process's whole life; the bounded analogue is ``hermes_cli/managed_scope.py``'s
# ``VANISHED_GRACE_SECONDS``. Past the grace the answer is None (no chain) — fail closed, same
# direction as a cold process (R4-6).
FALLBACK_CHAIN_RETENTION_GRACE_SECONDS = 300.0

# hermes_home_key -> monotonic timestamp of the write that filled _LAST_CLEAN_FALLBACK_CHAIN.
_LAST_CLEAN_FALLBACK_CHAIN_AT: Dict[str, float] = {}

# hermes_home_key -> the observation epoch (hermes_cli.config_effective's per-read sequence) behind
# the retained record. Observation happens under ``_config._CONFIG_LOCK``; publication happens AFTER
# that lock is released, and the write used to overwrite value and timestamp unconditionally. So
# reader A could observe chain F, be descheduled, reader B observe and publish a suppression, and A
# then publish F with a FRESH timestamp — the newer suppression lost, and the next managed failure
# serving the revoked chain. Gateway turns run in a thread pool, so concurrent readers are normal.
# Carrying the epoch lets a publication be REFUSED when a strictly newer observation is already on
# record, which is the ordering guarantee without holding the config lock across the publication
# (R5-3).
_LAST_CLEAN_FALLBACK_CHAIN_EPOCH: Dict[str, int] = {}

# Guards the three retention dicts as ONE record: a reader must never see a value from one
# publication with the timestamp or epoch of another.
_FALLBACK_CHAIN_RETENTION_LOCK = threading.Lock()

# "nothing is retained for this home" — distinct from a retained None, which is a CLEAN observation
# that the home has no chain (a managed suppression) and must be served as such.
_NO_RETENTION = object()


def _chain_retention_key(home_key: str, ignore_user_config: bool) -> str:
    """The retention slot for *home_key* under the ``--safe-mode`` / ``--ignore-user-config`` bypass.

    "What chain may a new agent have for this home" has TWO answers once the bypass exists — with
    and without the user layer — and one slot cannot hold both. One process routinely asks both
    questions (a safe-mode CLI still hosts gateway, cron and TUI readers for the same home), so a
    bypassed observation recording "no chain" under the plain key would make the next NON-bypassed
    constructor serve "no chain" on a failed managed read when the user's last clean chain was the
    correct answer — and the reverse would leak the ignored user chain into a bypassed constructor.
    Still ONE record per question, not a second cache: the R4-2 bug class was two slots for one
    fact, not one slot per distinct fact.
    """
    return f"{home_key}\x00no-user" if ignore_user_config else home_key


def _record_clean_fallback_chain(home_key: str, chain: Optional[List[Any]], *,
                                 epoch: Optional[int] = None) -> Optional[List[Any]]:
    """Record a SUCCESSFUL fallback-chain observation for *home_key* and return it unchanged.

    Called by both chain readers on their success paths. An empty/absent chain is recorded too: a
    managed ``fallback_providers: []`` that read cleanly is the admin layer's current answer, and
    forgetting it would let the previous, wider answer be served on the next failure (R4-2).

    *epoch* is the observation's sequence number from
    :func:`hermes_cli.config_effective.last_observation_epoch`, taken while the config lock was
    held. A publication whose observation is OLDER than the one already on record is refused, so a
    descheduled reader cannot resurrect a chain a later reader already saw revoked (R5-3). The
    caller still gets its own observation back — only the shared retention is protected.
    """
    with _FALLBACK_CHAIN_RETENTION_LOCK:
        if epoch is not None:
            recorded = _LAST_CLEAN_FALLBACK_CHAIN_EPOCH.get(home_key)
            if recorded is not None and recorded > epoch:
                logger.debug("fallback chain: refusing to publish observation %s for %s over the "
                             "newer observation %s already on record", epoch, home_key, recorded)
                return chain
            _LAST_CLEAN_FALLBACK_CHAIN_EPOCH[home_key] = epoch
        _LAST_CLEAN_FALLBACK_CHAIN[home_key] = chain
        _LAST_CLEAN_FALLBACK_CHAIN_AT[home_key] = time.monotonic()
    return chain


def _retained_fallback_chain(home_key: str) -> Any:
    """The retained observation for *home_key*, or ``_NO_RETENTION``.

    A retained ``None`` is returned as ``None``: it means "the last clean read said this home has no
    chain", which is exactly the suppression a caller must honour. ``_NO_RETENTION`` means there is
    no usable observation at all — nothing recorded (a cold process) or the record is older than
    ``FALLBACK_CHAIN_RETENTION_GRACE_SECONDS`` (R4-6).
    """
    with _FALLBACK_CHAIN_RETENTION_LOCK:
        if home_key not in _LAST_CLEAN_FALLBACK_CHAIN:
            return _NO_RETENTION
        recorded_at = _LAST_CLEAN_FALLBACK_CHAIN_AT.get(home_key)
        if recorded_at is None or (time.monotonic() - recorded_at) > FALLBACK_CHAIN_RETENTION_GRACE_SECONDS:
            logger.debug("fallback chain: the retained chain for %s is older than the %.0fs retention "
                         "grace; serving no chain rather than a stale one",
                         home_key, FALLBACK_CHAIN_RETENTION_GRACE_SECONDS)
            return _NO_RETENTION
        return _LAST_CLEAN_FALLBACK_CHAIN[home_key]


def load_fail_closed_fallback_chain(home: Path) -> Optional[List[Any]]:
    """The fallback chain for *home*, read FAIL-CLOSED against the shared per-home retention.

    THE single chain-construction primitive for the agent constructors in this repo's own
    surfaces — :meth:`GatewayConfigLoadersMixin._load_fallback_model` (gateway / API agents),
    ``cron/scheduler.py``, ``tui_gateway/agent_callbacks.py``, ``hermes_cli/cli_init_mixin.py``
    (classic CLI startup) and ``hermes_cli/oneshot.py`` — so there is one implementation of "what
    chain may a newly constructed agent have" and one retention behind it rather than a third and
    fourth cache (R5-2, extended to the CLI surfaces as R5-6 option 2).

    It does NOT claim to cover every conceivable chain source: the activation seam
    ``agent/chat_completion_helpers.try_activate_fallback`` is still ungated and revalidating an
    already-clean chain mid-turn remains a separate (P3) question. The earlier wording here —
    "every surface that builds a new agent" — contradicted the R5-6 nonclaim recorded 550 lines
    below and is corrected to the enumeration above (R8 evidence-honesty finding).

    A failed MANAGED read is a failed read (A44): the admin layer can only ever REMOVE or override
    what the user file says — a managed ``fallback_providers: []`` suppresses a user chain — so
    applying an unreadable managed file as an EMPTY overlay would hand the caller the user's
    unconstrained chain while reporting success. On a failed read the last CLEAN answer for this home
    is served instead, never the user's own chain; when nothing is on record (a cold process, or a
    record past the retention grace) the answer IS ``None``, i.e. no chain.

    :func:`record_clean_fallback_chain_observation` is the body; this wrapper only converts its
    failed-read exception into the retained answer.

    Under ``--safe-mode`` / ``--ignore-user-config`` the answer is the MANAGED layer's chain, or
    none — never the user's. See :func:`record_clean_fallback_chain_observation`.
    """
    from hermes_cli.config_effective import user_config_bypassed
    from hermes_constants import hermes_home_key
    try:
        return record_clean_fallback_chain_observation(home)
    except Exception:
        home_key = _chain_retention_key(hermes_home_key(home), user_config_bypassed())
        retained = _retained_fallback_chain(home_key)
        chain = None if retained is _NO_RETENTION else retained
        logger.debug("fallback chain: config.yaml for %s did not read cleanly; serving the last clean "
                     "chain (%s entries%s) rather than the unconstrained user chain",
                     home, len(chain or []),
                     ", nothing retained" if retained is _NO_RETENTION else "", exc_info=True)
        return chain


def record_clean_fallback_chain_observation(home: Path) -> Optional[List[Any]]:
    """Read the chain for *home* fail-closed, PUBLISH the observation, and return it.

    Raises whatever the fail-closed config read raised, so a caller that holds its own
    last-known-good state keeps it (the per-turn refresh contract, #95066) instead of reading a
    failed read as "chain removed". Only a SUCCESSFUL observation publishes — including an EMPTY
    one, which is a clean managed suppression and must be retained AS a suppression (R4-2).

    Separating this from :func:`load_fail_closed_fallback_chain` is what lets the per-turn refreshes
    share the retention without changing their failure behaviour. ``tui_gateway``'s per-turn sync
    used to call ``get_fallback_chain(load_user_config_effective(...))`` directly: fail-closed for
    the agent in front of it, but publishing nothing, so a suppression it observed never reached the
    record the CONSTRUCTORS read. The sequence that opened was: a constructor records chain ``F``;
    the admin layer revokes it and a normal turn observes the suppression; the managed file then
    stops reading; within the grace the next constructor serves ``F`` to a brand-new agent and the
    ungated activation seam installs it (R9, the R8 review's P2 counterexample).

    The observation epoch is captured while the config lock is still effectively the read's own, and
    BEFORE ``get_fallback_chain`` runs, so the publication can be ordered against a concurrent
    reader's (R5-3). Every writer therefore carries an epoch; a publication with none could not be
    refused and would be able to overwrite a strictly newer observation.

    ``--safe-mode`` / ``--ignore-user-config`` is honoured HERE, at the one function that owns what
    chain a new agent may have, rather than inside ``load_user_config_effective`` (whose ~20 other
    callers have their own contract with that flag) and rather than at each call site (five
    constructors plus two per-turn syncs, which is how the R9 repair came to cover construction but
    not the first turn — R10/F1/D-1). The user layer is suppressed; the MANAGED overlay still
    applies and is still read fail-closed, so the bypass cannot be used to escape administrator
    policy or A44. The observation is retained under its own key
    (:func:`_chain_retention_key`) so the two questions never contaminate each other.
    """
    from hermes_cli.config_effective import (last_observation_epoch, load_user_config_effective,
                                             user_config_bypassed)
    ignore_user_config = user_config_bypassed()
    cfg = load_user_config_effective(home / "config.yaml", fail_closed=True,
                                     ignore_user_config=ignore_user_config)
    return publish_clean_fallback_chain_observation(home, cfg, epoch=last_observation_epoch(),
                                                    ignore_user_config=ignore_user_config)


def publish_clean_fallback_chain_observation(home: Path, cfg: dict, *, epoch,
                                             ignore_user_config: bool) -> Optional[List[Any]]:
    """Publish a retention observation from an ALREADY-COMPLETED fail-closed read.

    The caller finished its own ``load_user_config_effective(..., fail_closed=True)`` and hands
    over that read's config, observation epoch AND the bypass identity the read was made under, so
    ONE physical observation serves both the caller's derivation and this shared retention — a
    reader that observed a suppression must publish it (R14-3), and the single-observation
    direction is the recorded R11-2 follow-up. ``ignore_user_config`` is REQUIRED and must be the
    read's own setting, never re-sampled here: an in-process bypass transition between read and
    publication would otherwise file a user-inclusive chain into the safe-mode slot (R15-2).
    Ordering semantics are unchanged: the epoch is the read's own, captured before any derivation.
    """
    from hermes_constants import hermes_home_key
    home_key = _chain_retention_key(hermes_home_key(home), ignore_user_config)
    return _record_clean_fallback_chain(home_key, get_fallback_chain(cfg) or None, epoch=epoch)


_BUSY_INPUT_MODES = {"interrupt", "queue", "steer"}


class GatewayConfigLoadersMixin:
    """Config/env loaders (busy modes, reasoning, service tier, timeouts, fallback) for GatewayRunner."""

    @staticmethod
    def _cfg_str(section: str, key: str) -> str:
        """``<section>.<key>`` from the gateway runtime config as a stripped string ("" when unset)."""
        from gateway.run import _load_gateway_config
        return str(cfg_get(_load_gateway_config(), section, key, default="") or "").strip()

    @classmethod
    def _env_or_cfg_str(cls, env_var: str, section: str, key: str) -> str:
        """Non-empty stripped env var, else :meth:`_cfg_str`."""
        return os.getenv(env_var, "").strip() or cls._cfg_str(section, key)

    @staticmethod
    def _load_prefill_messages() -> List[Dict[str, Any]]:
        """Load ephemeral prefill messages from config or env var.

        HERMES_PREFILL_MESSAGES_FILE env wins, then top-level prefill_messages_file in config.yaml,
        then legacy agent.prefill_messages_file. Relative paths resolve from ~/.hermes/.
        """
        from gateway.run import _gateway_config_home, _load_gateway_config
        file_path = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "")
        if not file_path:
            cfg = _load_gateway_config()
            file_path = str(
                cfg.get("prefill_messages_file", "") or cfg_get(cfg, "agent", "prefill_messages_file", default="") or ""
            )
        if not file_path:
            return []
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = _gateway_config_home() / path
        if not path.exists():
            logger.warning("Prefill messages file not found: %s", path)
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning("Prefill messages file must contain a JSON array: %s", path)
                return []
            return data
        except Exception as e:
            logger.warning("Failed to load prefill messages from %s: %s", path, e)
            return []

    @staticmethod
    def _load_ephemeral_system_prompt() -> str:
        """HERMES_EPHEMERAL_SYSTEM_PROMPT env first, then ``display.personality`` / ``agent.system_prompt``."""
        from gateway.run import _load_gateway_config
        prompt = os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", "")
        if prompt:
            return prompt
        return resolve_ephemeral_system_prompt_from_config(_load_gateway_config())

    def _channel_override(self, platform: Platform, chat_id: str, thread_id, parent_id):
        """``channel_overrides`` entry for this channel/thread, or None (also when no config is bound)."""
        from gateway.run import _get_channel_override
        config = getattr(self, "config", None)
        if not config:
            return None
        return _get_channel_override(config, platform, chat_id, thread_id=thread_id, parent_id=parent_id)

    def _resolve_model_for_channel(
        self, platform: Platform, chat_id: str, *, user_config: Optional[dict] = None,
        thread_id: Optional[str] = None, parent_id: Optional[str] = None,
    ) -> str:
        """Resolve model for this channel: channel_overrides else global default.

        Precedence lives in :func:`hermes_cli.model_switch.resolve_effective_model` (shared with the
        API server so the surfaces cannot diverge). No session tier here: session /model overrides
        are applied later by ``_apply_session_model_override``.
        """
        from gateway.run import _resolve_gateway_model
        from hermes_cli.model_switch import resolve_effective_model
        return resolve_effective_model(
            None,  # session tier applied downstream (_apply_session_model_override)
            self._channel_override(platform, chat_id, thread_id, parent_id),
            _resolve_gateway_model(user_config),
        )

    def _get_system_prompt_for_channel(
        self, platform: Platform, chat_id: str, *, thread_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> str:
        """Ephemeral system prompt for this channel/thread.

        ``channel_overrides`` when set, else the gateway prompt resolved from the CURRENT profile's
        config on every call (callers run inside ``_profile_runtime_scope``, so routed multiplex
        profiles get their own personality/system_prompt and ``/personality`` edits apply next turn).
        Legacy ``channel_prompts`` are applied separately via ``event.channel_prompt`` in ``run_sync``.

        Callers run inside ``_profile_runtime_scope`` (``run_sync`` under ``_run_agent``), so a routed
        multiplex profile gets its own ``display.personality`` / ``agent.system_prompt`` instead of a
        boot-time snapshot of the launch profile's (#89161); ``/personality`` edits take effect on the next
        turn for the same reason.
        """
        override = self._channel_override(platform, chat_id, thread_id, parent_id)
        if override and override.system_prompt:
            return (override.system_prompt or "").strip()
        return self._load_ephemeral_system_prompt()

    @staticmethod
    def _load_reasoning_config(model: str = "") -> dict | None:
        """Reasoning effort from config.yaml via :func:`hermes_constants.resolve_reasoning_config`.

        Per-model override > global ``agent.reasoning_effort``; YAML False = disabled. Empty
        ``model`` uses ``model.default``.

        Closes #21256.
        """
        from gateway.run import _load_gateway_config
        from hermes_constants import resolve_reasoning_config
        return resolve_reasoning_config(_load_gateway_config(), model)

    @staticmethod
    def _parse_reasoning_command_args(raw_args: str) -> tuple[str, bool]:
        """Parse `/reasoning` args into `(value, persist_global)`; `--global` anywhere persists to config."""
        import shlex
        text = str(raw_args or "").strip().replace("—", "--")
        if not text:
            return "", False
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        value = " ".join(token for token in tokens if token != "--global")
        return value.strip().lower(), "--global" in tokens

    def _resolve_session_reasoning_config(
        self, *, source: Optional[SessionSource] = None, session_key: Optional[str] = None,
        model: str = "",
    ) -> dict | None:
        """Session ``/reasoning --session`` > per-model ``agent.reasoning_overrides`` > global.

        ``model`` must be the session's *effective* model (session ``/model`` override included);
        empty uses ``model.default``.
        """
        resolved_session_key = self._resolve_session_key_or_none(source, session_key)
        if resolved_session_key:
            _r_state = self._peek_session_state(resolved_session_key)
            if _r_state is not None and _r_state.conversation.reasoning_override is not None:
                return _r_state.conversation.reasoning_override
        return self._load_reasoning_config(model)

    def _set_session_reasoning_override(self, session_key: str, reasoning_config: Optional[dict]) -> None:
        """Set or clear the session-scoped reasoning override."""
        if not session_key:
            return
        # Per-session field write: a lazy ``_session_reasoning_overrides = {}`` init replaced the
        # WHOLE dict, racing concurrent sessions; a SessionState field reset cannot cross sessions.
        self._session_state(session_key).conversation.reasoning_override = (
            None if reasoning_config is None else dict(reasoning_config)
        )

    def _resolve_session_service_tier(self, source=None, session_key: Optional[str] = None) -> Optional[str]:
        """Effective service tier: a session-scoped /fast override beats the config default.

        The override stores "priority" or None (explicit normal), so presence — not truthiness — decides.
        """
        resolved_session_key = self._resolve_session_key_or_none(source, session_key)
        if resolved_session_key:
            _t_state = self._peek_session_state(resolved_session_key)
            if _t_state is not None and _t_state.conversation.service_tier_override is not _SERVICE_TIER_UNSET:
                return _t_state.conversation.service_tier_override
        return self._load_service_tier()

    def _set_session_service_tier_override(self, session_key: str, service_tier, clear: bool = False) -> None:
        """Set ("priority" / None = explicit normal) or ``clear`` the session-scoped /fast override."""
        if not session_key:
            return
        # Presence-sensitive: "priority" or None (explicit normal) both count as an override; the
        # sentinel means "no override". Per-session field write: a lazy dict replace races sessions.
        self._session_state(session_key).conversation.service_tier_override = (
            _SERVICE_TIER_UNSET if clear else service_tier
        )

    @classmethod
    def _load_service_tier(cls) -> str | None:
        """``agent.service_tier``: fast/priority/on => "priority"; normal/off => None; None when unset/unknown."""
        raw = cls._cfg_str("agent", "service_tier")
        value = raw.lower()
        if not value or value in {"normal", "default", "standard", "off", "none"}:
            return None
        if value in {"fast", "priority", "on"}:
            return "priority"
        if value in {"auto", "cold"}:
            return value
        logger.warning("Unknown service_tier '%s', ignoring", raw)
        return None

    @staticmethod
    def _load_show_reasoning() -> bool:
        """``display.show_reasoning`` toggle."""
        from gateway.run import _load_gateway_config
        return is_truthy_value(cfg_get(_load_gateway_config(), "display", "show_reasoning"), default=False)

    @classmethod
    def _load_busy_input_mode(cls) -> str:
        """Gateway drain-time busy-input behavior from env/config (default ``interrupt``)."""
        mode = cls._env_or_cfg_str("HERMES_GATEWAY_BUSY_INPUT_MODE", "display", "busy_input_mode").lower()
        return mode if mode in {"queue", "steer"} else "interrupt"

    @classmethod
    def _load_busy_text_mode(cls) -> str:
        """Normal busy TEXT follow-up behavior.

        ``busy_input_mode`` is the source of truth (default ``interrupt``); legacy ``busy_text_mode``
        is honored only when explicitly set so existing queue setups keep working.
        """
        from gateway.run import GatewayRunner
        legacy = cls._env_or_cfg_str("HERMES_GATEWAY_BUSY_TEXT_MODE", "display", "busy_text_mode").lower()
        if legacy in {"interrupt", "queue"}:
            return legacy
        return "queue" if GatewayRunner._load_busy_input_mode() == "queue" else "interrupt"

    @staticmethod
    def _busy_modes_from_config(config: dict, *, fallback_input: str, fallback_text: str) -> tuple[str, str]:
        """Resolve one profile's busy modes without consulting process env."""
        raw_input = str(cfg_get(config, "display", "busy_input_mode", default="") or "").strip().lower()
        input_mode = raw_input if raw_input in _BUSY_INPUT_MODES else fallback_input
        raw_text = str(cfg_get(config, "display", "busy_text_mode", default="") or "").strip().lower()
        if raw_text in {"interrupt", "queue"}:
            text_mode = raw_text
        elif raw_input in _BUSY_INPUT_MODES:
            text_mode = "queue" if input_mode == "queue" else "interrupt"
        else:
            text_mode = fallback_text
        return input_mode, text_mode

    @staticmethod
    def _busy_text_timing_from_config(config: dict) -> tuple[float, float]:
        """``display.busy_text_debounce_seconds`` / ``display.busy_text_hard_cap_seconds`` for one
        profile, without consulting process env (#116893). A non-numeric or negative value is
        rejected with a warning naming the key, never silently coerced."""
        from gateway.platforms.base import DEFAULT_BUSY_TEXT_DEBOUNCE_SECONDS, DEFAULT_BUSY_TEXT_HARD_CAP_SECONDS
        out = []
        for key, default in (("busy_text_debounce_seconds", DEFAULT_BUSY_TEXT_DEBOUNCE_SECONDS),
                             ("busy_text_hard_cap_seconds", DEFAULT_BUSY_TEXT_HARD_CAP_SECONDS)):
            raw = cfg_get(config, "display", key, default=None)
            if raw is None or raw == "":
                out.append(default)
                continue
            try:
                value = float(raw)
                if value < 0 or value != value:
                    raise ValueError(raw)
            except (TypeError, ValueError):
                logger.warning("display.%s=%r is not a non-negative number; using %s", key, raw, default)
                value = default
            out.append(value)
        return out[0], out[1]

    @staticmethod
    def _human_delay_from_config(config: dict) -> Optional[tuple[int, int]]:
        """``human_delay.{mode,min_ms,max_ms}`` for one profile as a ``(lo_ms, hi_ms)`` range, or
        ``None`` when off (#116895). ``natural`` is the fixed 800-2500 range; ``custom`` reads the
        bounds and rejects non-integer, negative or inverted values with a warning naming the key,
        falling back to the natural range instead of letting ``random.uniform`` raise at send."""
        mode = str(cfg_get(config, "human_delay", "mode", default="off") or "off").strip().lower()
        if mode == "off":
            return None
        lo, hi = DEFAULT_HUMAN_DELAY_RANGE_MS
        if mode != "natural":
            if mode != "custom":
                logger.warning("human_delay.mode=%r is not off/natural/custom; using natural", mode)
                return DEFAULT_HUMAN_DELAY_RANGE_MS
            bounds = []
            for key, default in (("min_ms", lo), ("max_ms", hi)):
                raw = cfg_get(config, "human_delay", key, default=None)
                try:
                    value = default if raw is None or raw == "" else int(raw)
                    if value < 0:
                        raise ValueError(raw)
                except (TypeError, ValueError):
                    logger.warning("human_delay.%s=%r is not a non-negative integer; using %s", key, raw, default)
                    value = default
                bounds.append(value)
            lo, hi = bounds
            if lo > hi:
                logger.warning("human_delay.min_ms=%s exceeds human_delay.max_ms=%s; using natural range", lo, hi)
                lo, hi = DEFAULT_HUMAN_DELAY_RANGE_MS
        return lo, hi

    def _snapshot_profile_busy_modes(self, profile_name: str, config: dict) -> None:
        """Cache a routed profile's busy policy and pacing for this gateway lifetime."""
        input_mode, text_mode = self._busy_modes_from_config(
            config, fallback_input=getattr(self, "_busy_input_mode", "interrupt"),
            fallback_text=getattr(self, "_busy_text_mode", "interrupt"),
        )
        self.__dict__.setdefault("_busy_input_modes_by_profile", {})[profile_name] = input_mode
        self.__dict__.setdefault("_busy_text_modes_by_profile", {})[profile_name] = text_mode
        self.__dict__.setdefault("_busy_text_timing_by_profile", {})[profile_name] = (
            self._busy_text_timing_from_config(config))
        self.__dict__.setdefault("_human_delay_by_profile", {})[profile_name] = (
            self._human_delay_from_config(config))

    def _busy_profile_name_for_source(self, source: SessionSource) -> Optional[str]:
        """Return the routed profile whose busy policy applies, if any."""
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return None
        name = str(getattr(source, "profile", "") or "").strip()
        if not name:
            try:
                name = str(self._profile_name_for_source(source) or "").strip()
            except Exception:
                name = ""
        return name or None

    def _effective_busy_mode(self, source: SessionSource, attr: str) -> str:
        """Busy mode from the routed profile snapshot (``attr``: ``_busy_input_mode`` / ``_busy_text_mode``)."""
        fallback = getattr(self, attr, "interrupt")
        profile_name = self._busy_profile_name_for_source(source)
        if not profile_name:
            return fallback
        modes = getattr(self, attr + "s_by_profile", None)
        return modes.get(profile_name, fallback) if isinstance(modes, dict) else fallback

    def _effective_busy_input_mode(self, source: SessionSource) -> str:
        """Resolve busy input mode from the routed profile startup snapshot."""
        return self._effective_busy_mode(source, "_busy_input_mode")

    def _effective_busy_text_mode(self, source: SessionSource) -> str:
        """Resolve legacy busy text mode from the routed profile snapshot."""
        return self._effective_busy_mode(source, "_busy_text_mode")

    @staticmethod
    def _warn_unparsable_timeout(cfg_key: str, raw: object, default: float) -> None:
        """Warn when a supplied timeout value is not a number (the parser already fell back to ``default``)."""
        try:
            float(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid %s '%s', using default %.0fs", cfg_key, raw, default)

    @classmethod
    def _load_restart_drain_timeout(cls) -> float:
        """Graceful gateway restart/stop drain timeout in seconds."""
        raw = cls._env_or_cfg_str("HERMES_RESTART_DRAIN_TIMEOUT", "agent", "restart_drain_timeout")
        value = parse_restart_drain_timeout(raw)
        if raw and value == DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT:
            cls._warn_unparsable_timeout("restart_drain_timeout", raw, DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT)
        return value

    @staticmethod
    def _load_launchd_exit_timeout(drain_timeout: float) -> Optional[float]:
        """Read the live launchd ``ExitTimeOut`` this job runs under, if any.

        launchd is the one supervisor the gateway cannot size from config: the per-user (gui)
        domain clamps ``ExitTimeOut`` (measured 60s on macOS 26), and any signal-driven stop that
        drains past it is SIGKILLed mid-teardown — the unclean-exit half of the state.db
        corruption class. Returns ``None`` (fail-open, drain unchanged) when not launchd-owned or
        when ``launchctl print`` is unavailable. Logs a WARNING when the configured drain exceeds
        the live budget so the misconfiguration is visible at boot, not at the next SIGKILL.
        """
        label = launchd_service_label()
        if label is None:
            return None
        # read_launchd_exit_timeout_s is already fail-open (returns None on any probe failure).
        exit_timeout = read_launchd_exit_timeout_s(label)
        if exit_timeout is None:
            return None
        effective = resolve_launchd_capped_drain(drain_timeout, exit_timeout)
        if effective < drain_timeout:
            logger.warning(
                "restart_drain_timeout=%.0fs exceeds the live launchd exit timeout (%.0fs) for %s; "
                "signal-driven stops will drain at most %.0fs so teardown finishes before launchd "
                "SIGKILLs (launchd clamps ExitTimeOut in the per-user domain).",
                drain_timeout, exit_timeout, label, effective,
            )
        else:
            logger.info(
                "launchd exit timeout for %s is %.0fs (drain %.0fs fits)",
                label, exit_timeout, drain_timeout,
            )
        return exit_timeout

    @classmethod
    def _load_env_or_agent_cfg_timeout(cls, env_var: str, cfg_key: str, parse, default: float) -> float:
        """Env var (non-empty) else ``agent.<cfg_key>``; warn once when a supplied value fails to parse.

        ``0`` is a valid value; the parser falls back to ``default`` on garbage."""
        from gateway.run import _load_gateway_config
        env_raw = os.getenv(env_var)
        if env_raw is not None and str(env_raw).strip() != "":
            raw: object = env_raw
        else:
            raw = cfg_get(_load_gateway_config(), "agent", cfg_key, default=None)
        value = parse(raw)
        if raw is not None and str(raw).strip() != "":
            cls._warn_unparsable_timeout(cfg_key, raw, default)
        return value

    @classmethod
    def _load_restart_after_turn_timeout(cls) -> float:
        """In-band restart wait-for-idle timeout in seconds."""
        return cls._load_env_or_agent_cfg_timeout(
            "HERMES_RESTART_AFTER_TURN_TIMEOUT", "restart_after_turn_timeout",
            parse_restart_after_turn_timeout, DEFAULT_GATEWAY_RESTART_AFTER_TURN_TIMEOUT,
        )

    @classmethod
    def _load_cron_drain_timeout(cls) -> float:
        """The cron-only floor under the stop()/drain wait.

        See #82161.
        """
        return cls._load_env_or_agent_cfg_timeout(
            "HERMES_CRON_DRAIN_TIMEOUT", "cron_drain_timeout",
            parse_cron_drain_timeout, DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT,
        )

    @classmethod
    def _load_signal_interrupt_grace_timeout(cls) -> float:
        """``gateway.signal_interrupt_grace_timeout``: unexpected-signal post-interrupt grace in seconds."""
        from gateway.run import _load_gateway_config
        raw = cfg_get(_load_gateway_config(), "gateway", "signal_interrupt_grace_timeout", default=None)
        value = parse_signal_interrupt_grace_timeout(raw)
        if raw is not None and raw != "":
            cls._warn_unparsable_timeout(
                "signal_interrupt_grace_timeout", raw, DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT
            )
        return value

    def _post_interrupt_grace_timeout(self) -> float:
        """Grace before teardown after forcibly interrupting agents (longer on an unexpected signal)."""
        if getattr(self, "_signal_initiated_shutdown", False) and not getattr(self, "_restart_requested", False):
            grace = getattr(self, "_signal_interrupt_grace_timeout", DEFAULT_GATEWAY_SIGNAL_INTERRUPT_GRACE_TIMEOUT)
            return max(0.0, float(grace))
        return DEFAULT_GATEWAY_POST_INTERRUPT_GRACE_TIMEOUT

    @staticmethod
    def _load_background_notifications_mode() -> str:
        """Background process notification mode from env/config (default ``concise``), resolved for
        the AMBIENT profile — callers deciding for another profile's event enter its scope first
        (``_completion_event_scope``). The env override reads through the secret scope so a served
        secondary sees its own ``.env`` value, not the launch profile's ``os.environ``."""
        from gateway.run import _load_gateway_config
        from gateway.platforms._shared import platform_gate_env as _platform_gate_env
        mode = _platform_gate_env("HERMES_BACKGROUND_NOTIFICATIONS")
        if not mode:
            raw = cfg_get(_load_gateway_config(), "display", "background_process_notifications")
            if raw is False:
                mode = "off"
            elif raw not in {None, ""}:
                mode = str(raw)
        mode = (mode or "concise").strip().lower()
        if mode not in {"concise", "all", "result", "error", "off"}:
            logger.warning("Unknown background_process_notifications '%s', defaulting to 'concise'", mode)
            return "concise"
        return mode

    @staticmethod
    def _load_provider_routing() -> dict:
        """OpenRouter provider routing preferences (canonical fail-open loader: managed overlay + ${VAR})."""
        from gateway.run import _load_gateway_config
        try:
            return _load_gateway_config().get("provider_routing", {}) or {}
        except Exception:
            return {}

    @staticmethod
    def _load_fallback_model() -> list | None:
        """Fallback chain: ``fallback_providers`` (kept first) merged with legacy ``fallback_model``.

        Read FAIL-CLOSED, per Hermes home. This is the chain source for every agent created without
        a confirmed runtime lock (``gateway/platforms/api_server.py``: ``"fallback_model": None if
        confirmed_runtime_lock else GatewayRunner._load_fallback_model()``), and the admin layer can
        only ever REMOVE or override what the user file says — a managed ``fallback_providers: []``
        suppresses a user chain. Through the fail-OPEN ``_load_gateway_config`` an unreadable managed
        file was applied as an EMPTY overlay, so a newly created API agent received exactly the chain
        the admin layer had suppressed, and ``agent.chat_completion_helpers.try_activate_fallback``
        installs chain entries with no A44 gate of its own (CA-P2/P3-6 round 3).

        On a failed read the last CLEAN answer for that home is served, never the user's
        unconstrained chain: a managed file that stopped reading says nothing about the chain the
        admin layer last allowed, so dropping straight to "no chain" would silently disable failover
        for every agent created afterwards while an allowed chain was on record. Per home, because
        one process serves many profiles and a single runner-wide slot would hand a secondary profile
        the default profile's chain.

        When there is NOTHING on record for the home — a cold process, or a record past the retention
        grace — the answer IS None, i.e. no chain. An earlier version of this docstring claimed the
        reader never returns an empty chain either; that was wrong for exactly these two cases, which
        are also the fail-closed ones, so the behaviour is kept and the claim corrected (R4-7).

        The retention is ONE slot per home shared with ``_refresh_fallback_model``
        (``_record_clean_fallback_chain`` / ``_retained_fallback_chain``), and it records successful
        EMPTY observations too, so a clean suppression is retained as a suppression. With two
        divergent caches a suppression seen only by the refresh left this reader holding the older,
        wider chain, and the next managed failure served that REVOKED chain to a new API agent
        (R4-2). The retention is also time-bounded (``FALLBACK_CHAIN_RETENTION_GRACE_SECONDS``), so
        a permanently broken managed file does not serve the stale chain forever (R4-6).

        A torn USER file reaches this path too, where ``config_effective``'s last-known-good/backup
        rescue would otherwise have recovered it: on a cold process during an ordinary
        ``config.yaml`` edit a brand-new API agent therefore gets no failover rather than the user's
        chain. That is the deliberate trade — the alternative is distinguishing a torn user read from
        a failed managed read here, and only the managed failure is the one A44 cares about, but the
        A44 rule is "a failed read is a failed read" and this seam cannot tell a torn write from a
        hostile one (R4-5, recorded, not closed).

        The gate is applied HERE rather than in ``_load_gateway_config``: that loader is the
        documented fail-open primitive with ~15 callers whose contract is "``{}`` on any error", and
        converting it would change all of them. Same shape ``_refresh_fallback_model`` already uses.

        The body is :func:`load_fail_closed_fallback_chain`, which ``cron/scheduler.py``,
        ``tui_gateway/agent_callbacks.py``, ``hermes_cli/cli_init_mixin.py`` and
        ``hermes_cli/oneshot.py`` now share: closing only THIS constructor left the others feeding
        the same ungated activation seam from fail-open reads (R4-4, closed as R5-2; the two CLI
        surfaces converted as R5-6 option 2).

        That does NOT make the activation seam
        (``agent/chat_completion_helpers.try_activate_fallback``) safe to leave ungated, and the
        constructor work must not be read as closing it. Both reviewer lanes agreed the
        constructor/activation split is valid — the seam only walks ``agent._fallback_chain``, so an
        empty chain hits the exhaustion guard and the seam cannot manufacture an entry — but that is
        an argument about what the seam can do with what it is GIVEN, not a gate on the seam:

        - a chain that was clean when the agent was constructed and is revoked mid-session is still
          installable by the seam. The per-turn refreshes narrow the window (and, since R9, publish
          what they observe to the shared retention), but they run at turn START, not at activation;
        - gating the seam itself needs the A44 observation threaded through the mid-turn retry loop
          (CA-P2/P3-6's standing P3 follow-up). That is the remaining open item, deliberately NOT
          attempted here, and it is a design question for review rather than a silent widening.

        The interactive-CLI per-turn sync is no longer an exception to this. R10 converted
        ``cli_chat_turn_mixin._sync_fallback_chain_with_config`` onto
        :func:`record_clean_fallback_chain_observation`, so it reads fail-closed AND **publishes**
        what it observes into this shared retention (NC-7, the CLI half of the R5-2/R9 TUI fix) and
        honours ``--safe-mode`` / ``--ignore-user-config`` on the first turn. The earlier wording
        here — that it "publishes nothing to the shared retention … recorded rather than fixed" —
        described the pre-R10 code and is corrected (R9 §3.2/D-7 directed the correction; R10's own
        code then contradicted the original text outright).

        For the record, the classic CLI does NOT build one agent per process: nine
        ``_retire_agent`` call sites rebuild it after turns have run
        (``cli_chat_turn_mixin.py:85,376``; ``cli_agent_setup_mixin.py:340,511``;
        ``cli_model_switch_mixin.py:928``; ``cli_commands_mixin.py:1571,1576,2610,2668``), each
        landing on a fresh ``AIAgent`` at ``cli_agent_setup_mixin.py:662``. That rebuild path
        sources its chain from the fresher instance slot ``self._fallback_model``
        (``cli_agent_setup_mixin.py:685``), which the per-turn sync sets on a clean read and leaves
        untouched on a failed one — so the slot and the shared record now move together.
        """
        from gateway.run import _gateway_config_home
        return load_fail_closed_fallback_chain(_gateway_config_home())

    def _refresh_fallback_model(self) -> list | None:
        """Re-read fallback_providers from disk for the next agent create/reuse.

        Lets a chain edited after startup reach messaging sessions (cron already re-reads per job).
        A TRANSIENT read/parse failure (user mid-edit, non-atomic write) keeps the last known-good
        chain for THAT HOME; only a successful read that genuinely lacks the key clears it.

        Cron already does this per job via ``get_fallback_chain``; the gateway previously froze
        ``self._fallback_model`` at process start, so a chain configured (or changed) after ``hermes
        gateway`` was running never reached messaging sessions even though the same process's cron jobs fell
        back correctly. Fixes #60955.

        Reads the ACTIVE gateway home (the routed profile under multiplexing, else the launch home)
        and keeps one last-known-good chain per home: a single runner-wide slot filled from the launch
        home handed every secondary profile the default profile's fallback chain.

        The answer is a HOME-BOUND LOCAL, and the ``_NO_RETENTION`` case is explicitly ``None``.
        ``self._fallback_model`` used to be the fall-through source on that path, and it is a single
        instance slot not keyed by ``hermes_home_key``, which failed open twice (R5-1):

        - **expiry restored a known-revoked chain.** This method observed ``F``; the static reader
          observed a clean managed suppression for the same home; the managed file broke; past
          ``FALLBACK_CHAIN_RETENTION_GRACE_SECONDS`` the retention reported ``_NO_RETENTION`` and
          this method served ``F`` again — so the R4-6 grace failed closed on the static reader and
          OPEN here, the opposite of its stated invariant;
        - **cross-home leak.** Home A refreshed to ``F``; routing moved to home B, whose config did
          not read and for which nothing was retained; this method returned home A's ``F``,
          violating the per-home rule the retention dict exists to enforce.

        ``self._fallback_model`` is still assigned — it is the instance's current answer and the turn
        path reads it — but it is never the SOURCE, so it cannot answer for a home that has nothing
        on record.
        """
        from gateway.run import _gateway_config_home
        chain = load_fail_closed_fallback_chain(_gateway_config_home())
        self._fallback_model = chain
        return chain

    @staticmethod
    def _apply_fallback_chain_to_agent(agent: Any, chain: list | None) -> None:
        """Keep a cached agent's fallback chain aligned with current config.

        Skips the rewrite while a cooldown holds the agent on an activated fallback provider
        (``restore_primary_runtime`` owns that lifecycle); otherwise replaces the chain so
        mid-uptime ``fallback_providers`` edits apply without a restart.

        When primary is active (or cooldown expired), replace the chain so mid-uptime ``fallback_providers``
        edits take effect without requiring a gateway restart (#60955).
        """
        if agent is None:
            return
        new_chain = list(chain or [])
        rate_limited_until = getattr(agent, "_rate_limited_until", 0) or 0
        if getattr(agent, "_fallback_activated", False) and rate_limited_until > time.monotonic():
            return
        old_chain = list(getattr(agent, "_fallback_chain", []) or [])
        agent._fallback_chain = new_chain
        agent._fallback_model = new_chain[0] if new_chain else None
        if not getattr(agent, "_fallback_activated", False):
            agent._fallback_index = 0
        # A config edit means the user changed something — drop the session-scoped unavailability
        # memo so re-configured entries (e.g. credentials added mid-uptime) get retried. Only on real
        # content change, so the per-message no-op refresh keeps the memo's rate-limiting benefit.
        # See #60955.
        if new_chain != old_chain:
            unavailable = getattr(agent, "_unavailable_fallback_keys", None)
            if unavailable:
                unavailable.clear()
