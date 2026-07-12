"""Phase 1 — `compaction_queue` config surface (config only; the queue stays dark).

The queue must SHIP DARK. The InstallDir *is* the live backend, so a default-on
queue would change real behaviour on the next restart. These tests pin that:
``enabled`` defaults to False, agent init stores the settings but calls nothing,
and no production code path touches the coordinator yet.

They also pin load-time normalisation. ``max_concurrent`` is the dangerous field:
a ``0`` would admit nobody and deadlock ALL compaction — the exact inversion of the
fail-open contract (spec §4.3/§6). It is clamped to ``>= 1`` at load, so a nonsense
value can never reach the coordinator primitives.
"""

import subprocess
from pathlib import Path

import pytest

from hermes_cli.config import (
    DEFAULT_CONFIG,
    _KNOWN_ROOT_KEYS,
    get_compaction_queue_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cfg(**queue):
    return {"compaction_queue": queue}


# ── Defaults: the queue ships DARK ──────────────────────────────────────────


class TestDefaults:
    def test_default_config_has_the_block(self):
        assert "compaction_queue" in DEFAULT_CONFIG

    def test_enabled_defaults_to_FALSE(self):
        """The entire safety story rests on this one line.

        Rollout is a separate, explicitly user-approved step. If this ever flips
        to True by default, the running backend starts queueing compaction on its
        next restart without anyone deciding to.
        """
        assert DEFAULT_CONFIG["compaction_queue"]["enabled"] is False
        assert get_compaction_queue_settings({})["enabled"] is False
        assert get_compaction_queue_settings(DEFAULT_CONFIG)["enabled"] is False

    def test_default_max_concurrent_is_conservative(self):
        # 1 is the point of the feature: serialise the herd.
        assert DEFAULT_CONFIG["compaction_queue"]["max_concurrent"] == 1

    def test_defaults_match_the_spec(self):
        block = DEFAULT_CONFIG["compaction_queue"]
        assert block["slot_ttl_seconds"] == 300
        assert block["max_wait_seconds"] == 300
        assert block["notify_after_seconds"] == 60

    def test_section_is_allowlisted_at_root(self):
        assert "compaction_queue" in _KNOWN_ROOT_KEYS

    def test_missing_block_yields_safe_defaults(self):
        s = get_compaction_queue_settings({})
        assert s["enabled"] is False
        assert s["max_concurrent"] == 1


# ── Normalisation: malformed config can never break a turn ──────────────────


class TestNormalisation:
    """Compaction runs inside the agent loop, so a bad value must degrade, never
    raise. ``get_compaction_queue_settings()`` is total."""

    @pytest.mark.parametrize(
        "bad", [0, -1, -99, 0.4, "x", None, [], {}, float("nan"), float("inf")]
    )
    def test_max_concurrent_is_clamped_to_at_least_one(self, bad):
        s = get_compaction_queue_settings(_cfg(max_concurrent=bad))
        assert s["max_concurrent"] >= 1, (
            f"max_concurrent={bad!r} produced {s['max_concurrent']} — anything below 1 "
            f"admits nobody and deadlocks ALL compaction."
        )
        assert isinstance(s["max_concurrent"], int)

    @pytest.mark.parametrize("value,expected", [(1, 1), (2, 2), (8, 8), (3.9, 3)])
    def test_valid_max_concurrent_passes_through(self, value, expected):
        assert get_compaction_queue_settings(
            _cfg(max_concurrent=value))["max_concurrent"] == expected

    def test_bool_max_concurrent_is_not_silently_read_as_a_number(self):
        """``bool`` is an ``int`` subclass in Python.

        Without an explicit bool check, ``max_concurrent: true`` from YAML would be
        read as the number 1. It falls back to the default instead — same value
        here, but for the right reason, and it would matter the moment the default
        changed.
        """
        assert get_compaction_queue_settings(
            _cfg(max_concurrent=True))["max_concurrent"] == 1

    @pytest.mark.parametrize("bad", ["soon", None, [], {}, float("nan")])
    def test_bad_ttl_falls_back_to_default(self, bad):
        assert get_compaction_queue_settings(
            _cfg(slot_ttl_seconds=bad))["slot_ttl_seconds"] == 300.0

    def test_ttl_is_floored(self):
        assert get_compaction_queue_settings(
            _cfg(slot_ttl_seconds=0))["slot_ttl_seconds"] >= 1.0

    def test_malformed_block_type_does_not_raise(self):
        for junk in ("nonsense", [], 5, None):
            s = get_compaction_queue_settings({"compaction_queue": junk})
            assert s["enabled"] is False
            assert s["max_concurrent"] == 1

    def test_normalised_output_is_directly_safe_for_the_coordinator(self):
        """The clamped output must be usable as-is: the coordinator's own
        defensive coercers should never have to reject a value that came from
        config normalisation."""
        from agent.compaction_coordinator import _coerce_max_concurrent, _coerce_ttl

        for bad in (0, -5, "x", None, True, float("nan")):
            s = get_compaction_queue_settings(
                _cfg(max_concurrent=bad, slot_ttl_seconds=bad))
            # These raise _CoordinatorInputError on a bad value. They must not here.
            assert _coerce_max_concurrent(s["max_concurrent"]) >= 1
            assert _coerce_ttl(s["slot_ttl_seconds"]) >= 1.0


class TestEnabledParsingIsStrictTruthy:
    """``enabled`` is STRICT-TRUTHY, and that is a deliberate deviation.

    The neighbouring convention (e.g. ``compression.enabled`` in agent_init) is
    ``str(value).lower() in {"true","1","yes"}``, which accepts the INTEGER ``1``.
    This block does not: only real booleans and the usual YAML string spellings
    turn the queue on.

    Why diverge: this is a DARK feature guarded by a rollout gate. Every ambiguous
    value should resolve to OFF, because the cost of a false "on" (the live backend
    silently starts queueing compaction) is far higher than the cost of a false
    "off" (the user writes `enabled: true` properly). ``enabled: 1`` is more likely
    to be a typo or a stray migration artefact than a considered decision to enable
    an unreleased feature.

    Flagged for the reviewer: if the project prefers convention-consistency over
    this asymmetry, the fix is a one-line change in get_compaction_queue_settings()
    plus this class.
    """

    @pytest.mark.parametrize("raw", [True, "true", "True", "TRUE", " yes ", "1", "on"])
    def test_explicit_true_spellings_enable(self, raw):
        assert get_compaction_queue_settings(_cfg(enabled=raw))["enabled"] is True

    @pytest.mark.parametrize("raw", [False, "false", "no", "off", "", "maybe", None, [], {}])
    def test_explicit_and_ambiguous_falsey_values_stay_off(self, raw):
        assert get_compaction_queue_settings(_cfg(enabled=raw))["enabled"] is False

    @pytest.mark.parametrize("raw", [1, 2, 1.0])
    def test_integer_one_does_NOT_enable_the_queue(self, raw):
        """Deliberate: a dark feature's safe default is OFF for anything ambiguous."""
        assert get_compaction_queue_settings(_cfg(enabled=raw))["enabled"] is False

    def test_result_is_always_a_real_bool(self):
        for raw in (True, "yes", 1, None, "x"):
            assert isinstance(get_compaction_queue_settings(_cfg(enabled=raw))["enabled"], bool)


# ── Agent init: fields stored, but INERT ────────────────────────────────────


class TestAgentInitFields:
    QUEUE_FIELDS = (
        "compaction_queue_enabled",
        "compaction_queue_max_concurrent",
        "compaction_slot_ttl_seconds",
        "compaction_queue_max_wait_seconds",
        "compaction_queue_notify_after_seconds",
    )

    def test_a_REAL_agent_carries_the_queue_fields_and_is_dark(self, tmp_path):
        """RUNTIME assertion — construct an actual AIAgent and read the attributes.

        The source-text check below is a cheap backstop, but it would happily pass
        if a refactor moved the assignments behind a condition that never fires.
        This one cannot: it builds a real agent (the same minimal construction the
        other agent tests use) and asserts the attributes exist, are the right
        types, and are DARK.

        Skipped — not silently weakened — when the local OpenAI SDK install is
        broken, since AIAgent construction needs it. That is a pre-existing
        environment fault on this machine (a missing pydantic_core binary), not a
        property of this change, and the test is genuinely valuable in a healthy
        environment/CI.
        """
        pytest.importorskip(
            "pydantic_core",
            reason="AIAgent construction requires a working OpenAI SDK install",
        )

        from hermes_state import SessionDB
        from run_agent import AIAgent

        db = SessionDB(tmp_path / "state.db")
        sid = "sess-compaction-queue-config"
        db.create_session(session_id=sid, source="test", model="test")

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=sid,
        )

        for field in self.QUEUE_FIELDS:
            assert hasattr(agent, field), f"agent init did not populate {field}"

        # DARK by default — this is the whole safety property.
        assert agent.compaction_queue_enabled is False

        # Normalised and directly usable by the coordinator (no re-validation needed).
        assert isinstance(agent.compaction_queue_max_concurrent, int)
        assert agent.compaction_queue_max_concurrent >= 1
        assert agent.compaction_slot_ttl_seconds >= 1.0
        assert agent.compaction_queue_max_wait_seconds >= 0.0
        assert agent.compaction_queue_notify_after_seconds >= 0.0

    def test_init_populates_the_normalised_fields(self):
        """Cheap backstop for the runtime test above (and a clear failure message
        if the assignments are removed outright)."""
        import agent.agent_init as ai

        src = Path(ai.__file__).read_text(encoding="utf-8")
        for field in self.QUEUE_FIELDS:
            assert f"agent.{field}" in src, f"agent init must populate {field}"

    def test_a_malformed_block_cannot_break_startup(self):
        """The queue is an optional perf control; a bad block must degrade to
        'disabled', never abort agent construction."""
        s = get_compaction_queue_settings({"compaction_queue": "totally bogus"})
        assert s["enabled"] is False
        assert s["max_concurrent"] == 1
        assert s["slot_ttl_seconds"] == 300.0

        # And init guards the call itself, so even an unexpected raise is contained.
        import agent.agent_init as ai

        src = Path(ai.__file__).read_text(encoding="utf-8")
        assert "get_compaction_queue_settings" in src
        assert "compaction_queue config unreadable" in src, (
            "agent init must swallow a failure to read the queue block"
        )

    def test_init_does_not_call_the_coordinator(self):
        """Phase 1 is config ONLY — init must not acquire/refresh/release."""
        import agent.agent_init as ai

        src = Path(ai.__file__).read_text(encoding="utf-8")
        for banned in (
            "try_acquire_compaction_slot",
            "release_compaction_slot",
            "refresh_compaction_slot",
            "compaction_coordinator",
        ):
            assert banned not in src, f"agent_init must not touch {banned} in Phase 1"


# ── Scope: the queue is still dark ──────────────────────────────────────────


class TestQueueStillDark:
    """These guards assert on CODE, not on source text.

    A naive substring/grep check is worse than useless here: the config block's
    own comment names ``agent/compaction_coordinator.py``, the spec doc names
    ``try_acquire_compaction_slot``, and the coordinator's docstrings discuss
    ``HERMES_HOME`` and ``HERMES_KANBAN_HOME`` in prose. All three are legitimate
    *mentions*. Only real imports, real calls, and real ``os.environ`` reads count.
    """

    PROD = ["agent", "hermes_cli", "gateway", "tui_gateway", "run_agent.py"]

    def _git_grep(self, pattern, paths):
        out = subprocess.run(
            ["git", "grep", "-nE", pattern, "--", *paths],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        ).stdout.strip()
        return [ln for ln in out.splitlines() if ln]

    def test_no_production_code_IMPORTS_the_coordinator_yet(self):
        """Fails the moment someone wires the queue in without the rollout step."""
        imports = self._git_grep(
            r"^\s*(from +agent +import +compaction_coordinator"
            r"|from +agent\.compaction_coordinator +import"
            r"|import +agent\.compaction_coordinator)",
            self.PROD,
        )
        assert not imports, (
            f"production code imports the coordinator before the wiring phase:\n"
            + "\n".join(imports)
        )

    def test_no_production_code_CALLS_the_slot_primitives_yet(self):
        calls = self._git_grep(
            r"(try_acquire_compaction_slot|release_compaction_slot"
            r"|refresh_compaction_slot|get_compaction_slot_load) *\(",
            self.PROD,
        )
        # The coordinator module defines them; that is not a call site.
        offenders = [c for c in calls if not c.startswith("agent/compaction_coordinator.py")]
        assert not offenders, (
            "compaction slot primitives are called outside the coordinator:\n"
            + "\n".join(offenders)
        )

    def test_compression_path_does_not_reference_the_queue(self):
        for mod in ("agent/conversation_compression.py", "agent/context_compressor.py"):
            src = (REPO_ROOT / mod).read_text(encoding="utf-8")
            assert "compaction_slot" not in src
            assert "compaction_coordinator" not in src

    def test_the_coordinator_reads_exactly_one_env_var(self):
        """Queue tunables are YAML-only (AGENTS: no new HERMES_* for non-secret
        config). HERMES_COMPACTION_HOME is a Phase 0 PATH override — it relocates
        the coordinator file and tunes no behaviour — and must remain the only
        environment variable the coordinator actually READS.

        Asserted by parsing os.environ accesses, not by counting substrings: the
        module's docstrings legitimately discuss HERMES_HOME and HERMES_KANBAN_HOME
        when explaining why root-anchoring exists.
        """
        import ast

        src = (REPO_ROOT / "agent" / "compaction_coordinator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        # The coordinator reads os.environ.get(COMPACTION_HOME_ENV) — a module
        # constant, not a literal — so resolve module-level string assignments.
        consts = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            consts[tgt.id] = node.value.value

        def _as_env_name(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return consts.get(node.id)
            return None

        env_reads = set()
        for node in ast.walk(tree):
            # os.environ.get(X) / os.getenv(X)
            if isinstance(node, ast.Call) and node.args:
                if getattr(node.func, "attr", None) in {"get", "getenv"}:
                    name = _as_env_name(node.args[0])
                    if name and name.startswith("HERMES_"):
                        env_reads.add(name)
            # os.environ[X]
            if isinstance(node, ast.Subscript):
                name = _as_env_name(node.slice)
                if name and name.startswith("HERMES_"):
                    env_reads.add(name)

        assert env_reads == {"HERMES_COMPACTION_HOME"}, (
            f"the coordinator must read exactly one env var (the path override); "
            f"found: {sorted(env_reads)}"
        )

    def test_no_queue_tunable_env_vars_anywhere_in_config(self):
        offenders = self._git_grep(
            r"HERMES_(COMPACTION_(ENABLED|MAX_CONCURRENT|QUEUE)|SLOT_TTL|COMPACTION_MAX_WAIT)",
            ["hermes_cli/config.py", "agent"],
        )
        assert not offenders, f"queue tunables must be YAML-only:\n" + "\n".join(offenders)
