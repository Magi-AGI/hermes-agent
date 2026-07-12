"""Part 1 fast-follow for the compaction route guard (spec §5.3 startup validation, §7 metrics).

Two additive concerns, neither of which changes the Phase 0.5 guard's decisions:

* **Counters** — ``compaction.route_rejected_total{provider, reason}`` and
  ``compaction.credential_candidate_skipped_total{provider, source, reason}``.
  A *skip* (search continued past a metered-shaped credential) is deliberately
  distinct from a *rejection* (the route ended).
* **Startup validation** — a metered/unknown configured compaction route is
  surfaced when the session starts, instead of at the first compaction.

Neither concern may activate the queue. The ``compaction_queue`` config block now
exists (Phase 1), so the guard tests at the bottom assert what actually matters:
it defaults to **dark** (``enabled: false``), nothing wires it into the routing
path, and the route guard does not import the coordinator. The privacy guard and
the performance queue are independent subsystems — the guard ships enabled and
must keep working regardless of the queue's state.
"""

from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch

from agent import anthropic_adapter as aa
from agent import auxiliary_client as ac
from agent import compaction_metrics as cm
from agent.auxiliary_client import (
    CompressionRoutingRejected,
    compression_route_guard,
    validate_configured_compression_routes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

OAUTH_TOKEN = "sk-ant-oat01-real-claude-max-token"
METERED_KEY = "sk-ant-api03-metered-console-key"

ANTHROPIC_ENV = ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def clean_counters_and_credentials(monkeypatch):
    cm.reset()
    for var in ANTHROPIC_ENV:
        monkeypatch.delenv(var, raising=False)
    # Neutralise the non-env credential sources so a developer's real machine
    # credentials cannot make a fail-closed assertion pass for the wrong reason.
    monkeypatch.setattr(aa, "read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(aa, "_resolve_anthropic_pool_token", lambda: None)
    monkeypatch.setattr(ac, "_select_pool_entry", lambda provider: (False, None))
    yield
    cm.reset()


def _cfg(**compression):
    return {"auxiliary": {"compression": compression}}


# ── A. Metrics / counters (§7) ──────────────────────────────────────────────


class TestRouteRejectedCounter:
    def test_metered_anthropic_api_key_increments_with_provider_and_reason(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", METERED_KEY)
        with patch.object(aa, "build_anthropic_client") as build:
            with compression_route_guard():
                client, _ = ac._try_anthropic()

        assert client is None
        build.assert_not_called()
        assert cm.get_counter(
            cm.ROUTE_REJECTED_TOTAL, provider="anthropic", reason="metered_auth_mode",
        ) == 1

    def test_non_allowlisted_provider_increments_not_allowlisted(self):
        openrouter = MagicMock()
        openrouter.base_url = "https://openrouter.ai/api/v1"
        with compression_route_guard():
            assert ac._compression_client_allowed("openrouter", openrouter) is False
        assert cm.get_counter(
            cm.ROUTE_REJECTED_TOTAL, provider="openrouter", reason="endpoint_mismatch",
        ) == 1

    def test_counter_accumulates_across_rejections(self):
        client = MagicMock()
        client.base_url = "https://openrouter.ai/api/v1"
        with compression_route_guard():
            ac._compression_client_allowed("openrouter", client)
            ac._compression_client_allowed("openrouter", client)
        assert cm.get_counter(
            cm.ROUTE_REJECTED_TOTAL, provider="openrouter", reason="endpoint_mismatch",
        ) == 2

    def test_admitted_route_does_not_increment(self):
        oauth = ac.AnthropicAuxiliaryClient(
            MagicMock(), "claude-haiku-4-5", OAUTH_TOKEN,
            "https://api.anthropic.com", is_oauth=True,
        )
        with compression_route_guard():
            assert ac._compression_client_allowed("anthropic", oauth) is True
        assert cm.snapshot().get(cm.ROUTE_REJECTED_TOTAL, {}) == {}

    def test_guard_inactive_does_not_increment(self):
        """Counters are compaction-scoped: a normal aux call must not move them."""
        client = MagicMock()
        client.base_url = "https://openrouter.ai/api/v1"
        assert ac._compression_client_allowed("openrouter", client) is True  # no guard
        assert cm.snapshot() == {}


class TestCredentialCandidateSkippedCounter:
    def test_scan_past_metered_token_increments_skip_with_source_label(self, monkeypatch):
        """A metered ANTHROPIC_TOKEN passed over in favour of a later OAuth source."""
        monkeypatch.setenv("ANTHROPIC_TOKEN", METERED_KEY)
        monkeypatch.setattr(aa, "_resolve_anthropic_pool_token", lambda: OAUTH_TOKEN)

        with patch.object(aa, "build_anthropic_client", return_value=MagicMock()) as build:
            with compression_route_guard():
                client, _ = ac._try_anthropic()

        # The compaction SUCCEEDED on the later OAuth credential...
        assert client is not None and client.is_oauth is True
        assert build.call_args[0][0] == OAUTH_TOKEN
        # ...and the skip is recorded with provider+source+reason.
        assert cm.get_counter(
            cm.CREDENTIAL_CANDIDATE_SKIPPED_TOTAL,
            provider="anthropic", source=aa.SOURCE_ANTHROPIC_TOKEN, reason="metered_shape",
        ) == 1
        # A skip is NOT a rejection — the search continued, the route did not end.
        assert cm.snapshot().get(cm.ROUTE_REJECTED_TOTAL, {}) == {}

    def test_exhaustion_records_skips_and_a_rejection(self, monkeypatch):
        """The §7 diagnostic pair: 'we skipped metered candidates, then found nothing'."""
        monkeypatch.setenv("ANTHROPIC_TOKEN", METERED_KEY)
        monkeypatch.setenv("ANTHROPIC_API_KEY", METERED_KEY)

        with patch.object(aa, "build_anthropic_client") as build:
            with compression_route_guard():
                client, _ = ac._try_anthropic()

        assert client is None
        build.assert_not_called()
        assert cm.get_counter(
            cm.CREDENTIAL_CANDIDATE_SKIPPED_TOTAL,
            provider="anthropic", source=aa.SOURCE_ANTHROPIC_TOKEN, reason="metered_shape",
        ) == 1
        assert cm.get_counter(
            cm.CREDENTIAL_CANDIDATE_SKIPPED_TOTAL,
            provider="anthropic", source=aa.SOURCE_ANTHROPIC_API_KEY, reason="metered_shape",
        ) == 1
        assert cm.get_counter(
            cm.ROUTE_REJECTED_TOTAL, provider="anthropic", reason="metered_auth_mode",
        ) == 1


# ── B. Startup route validation (§5.3) ──────────────────────────────────────


class TestStartupRouteValidation:
    def _validate(self, cfg):
        with patch("hermes_cli.config.load_config", return_value=cfg):
            return validate_configured_compression_routes()

    def test_allowlisted_primary_passes(self):
        assert self._validate(_cfg(provider="openai-codex")) == []
        assert self._validate(_cfg(provider="anthropic")) == []

    def test_auto_is_not_a_configured_route(self):
        """`auto` is screened at runtime by the guard, not statically here."""
        assert self._validate(_cfg(provider="auto")) == []
        assert self._validate(_cfg(provider="")) == []

    @pytest.mark.parametrize("provider", [
        "openai-api", "openrouter", "gemini", "custom", "deepseek", "azure-foundry",
    ])
    def test_metered_or_unknown_primary_is_reported(self, provider):
        problems = self._validate(_cfg(provider=provider))
        assert len(problems) == 1
        assert "auxiliary.compression.provider" in problems[0]
        assert provider in problems[0]
        assert "not an allowed compaction route" in problems[0]

    def test_task_fallback_chain_rungs_are_validated(self):
        problems = self._validate(_cfg(
            provider="openai-codex",
            fallback_chain=[
                {"provider": "anthropic", "model": "claude-haiku-4-5"},   # allowed
                {"provider": "openrouter", "model": "some/model"},        # metered
            ],
        ))
        assert len(problems) == 1
        assert "auxiliary.compression.fallback_chain[1]" in problems[0]
        assert "openrouter" in problems[0]

    def test_top_level_fallback_providers_are_validated(self):
        cfg = _cfg(provider="openai-codex")
        cfg["fallback_providers"] = [
            {"provider": "gemini", "model": "gemini-3-flash"},
        ]
        problems = self._validate(cfg)
        assert len(problems) == 1
        assert "fallback_providers[0]" in problems[0]
        assert "gemini" in problems[0]

    def test_allowlisted_provider_aliased_to_foreign_endpoint_is_reported(self):
        """The base_url aliasing trap, caught statically at startup (§5.3.4)."""
        problems = self._validate(_cfg(
            provider="anthropic", base_url="https://openrouter.ai/api/v1",
        ))
        assert len(problems) == 1
        assert "does not point at api.anthropic.com" in problems[0]

    def test_allowlisted_provider_with_its_own_endpoint_passes(self):
        assert self._validate(_cfg(
            provider="anthropic", base_url="https://api.anthropic.com",
        )) == []

    def test_empty_allowed_routes_is_a_config_error_not_an_opt_out(self):
        with pytest.raises(CompressionRoutingRejected, match="configuration error"):
            self._validate(_cfg(provider="openai-codex", allowed_routes=[]))

    def test_metered_allowed_routes_entry_rejected_at_startup(self):
        with pytest.raises(CompressionRoutingRejected, match="metered route"):
            self._validate(_cfg(
                provider="anthropic",
                allowed_routes=[{"provider": "anthropic", "auth_mode": "api_key"}],
            ))

    def test_narrowed_allowlist_reports_the_now_disallowed_provider(self):
        """Narrowing to Codex-only must make a configured anthropic route a problem."""
        problems = self._validate(_cfg(
            provider="anthropic",
            allowed_routes=[{"provider": "openai-codex", "auth_mode": "oauth_subscription"}],
        ))
        assert len(problems) == 1
        assert "anthropic" in problems[0]

    def test_validation_makes_no_network_or_credential_calls(self, monkeypatch):
        """Constructor-time check: must not resolve credentials (which can trigger
        an OAuth refresh over the network) or build any client."""
        def _boom(*a, **k):
            raise AssertionError("startup validation must not resolve credentials")

        monkeypatch.setattr(aa, "resolve_anthropic_token", _boom)
        monkeypatch.setattr(aa, "resolve_anthropic_token_with_provenance", _boom)
        monkeypatch.setattr(aa, "build_anthropic_client", _boom)
        monkeypatch.setattr(ac, "_try_anthropic", _boom)
        monkeypatch.setattr(ac, "_read_codex_access_token", _boom)

        assert self._validate(_cfg(provider="anthropic")) == []
        assert self._validate(_cfg(provider="openrouter")) != []


class TestStartupValidationWiring:
    """check_compression_model_feasibility surfaces the problem to the user."""

    def _agent(self):
        agent = MagicMock()
        agent.compression_enabled = True
        agent._compression_warning = None
        return agent

    def test_metered_primary_warns_at_startup(self):
        from agent.conversation_compression import check_compression_model_feasibility

        agent = self._agent()
        with patch(
            "agent.auxiliary_client.validate_configured_compression_routes",
            return_value=["auxiliary.compression.provider: 'openrouter' is not an allowed compaction route"],
        ):
            # Stop after the route check — the context-window probe is not under test.
            with patch("agent.auxiliary_client.get_text_auxiliary_client",
                       return_value=(None, None)):
                check_compression_model_feasibility(agent)

        assert agent._compression_warning is not None
        assert "subscription routes" in agent._compression_warning
        assert "openrouter" in agent._compression_warning
        agent._emit_status.assert_called()

    def test_malformed_allowed_routes_surfaces_as_config_error(self):
        from agent.conversation_compression import check_compression_model_feasibility

        agent = self._agent()
        with patch(
            "agent.auxiliary_client.validate_configured_compression_routes",
            side_effect=CompressionRoutingRejected("allowed_routes is empty or malformed"),
        ):
            with patch("agent.auxiliary_client.get_text_auxiliary_client",
                       return_value=(None, None)):
                check_compression_model_feasibility(agent)

        assert "configuration error" in agent._compression_warning

    def test_allowlisted_route_emits_no_route_warning(self):
        from agent.conversation_compression import check_compression_model_feasibility

        agent = self._agent()
        with patch(
            "agent.auxiliary_client.validate_configured_compression_routes",
            return_value=[],
        ):
            with patch("agent.auxiliary_client.get_text_auxiliary_client",
                       return_value=(None, None)):
                check_compression_model_feasibility(agent)

        # The no-provider warning may still fire, but never the route warning.
        assert "subscription routes" not in (agent._compression_warning or "")


# ── C. Scope discipline: the queue must not activate ────────────────────────


def test_queue_config_exists_but_is_dark_and_does_not_activate():
    """The route guard must never depend on, or be changed by, the queue.

    This guard originally asserted ``compaction_queue`` was ABSENT from
    DEFAULT_CONFIG. Phase 1 deliberately adds that block, so the absence check is
    now stale — but the property it was protecting is not. What actually matters,
    and what is asserted instead, is that the queue is **dark**: the config exists
    but defaults to disabled, and nothing has wired it into the auxiliary/routing
    path. The InstallDir is the running backend, so an accidentally-activated queue
    would be a live-runtime hazard.
    """
    from hermes_cli.config import DEFAULT_CONFIG, get_compaction_queue_settings

    # The block exists (Phase 1) but ships OFF.
    assert DEFAULT_CONFIG["compaction_queue"]["enabled"] is False
    assert get_compaction_queue_settings(DEFAULT_CONFIG)["enabled"] is False

    # The queue lives in its own module; the auxiliary client — which owns the
    # route guard — must remain entirely ignorant of it.
    for banned in (
        "compaction_slots",
        "try_acquire_compaction_slot",
        "release_compaction_slot",
        "SlotOutcome",
    ):
        assert not hasattr(ac, banned), (
            f"the route guard's module must not grow queue primitives ({banned})"
        )


def test_route_guard_does_not_import_the_coordinator():
    """The privacy guard and the performance queue are independent subsystems.

    The route guard ships enabled and must keep working regardless of the queue's
    state (spec: the guard is a privacy control and is explicitly NOT part of any
    queue rollback).
    """
    import ast
    from pathlib import Path

    for mod in ("agent/auxiliary_client.py", "agent/context_compressor.py",
                "agent/conversation_compression.py"):
        tree = ast.parse((REPO_ROOT / mod).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "compaction_coordinator" not in (node.module or ""), mod
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "compaction_coordinator" not in alias.name, mod
