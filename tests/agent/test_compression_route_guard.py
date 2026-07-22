"""Phase 0.5 — subscription-only compaction ROUTE guard.

Compaction inputs are business-sensitive: the summariser is handed the middle
window of the user's conversation verbatim. It may therefore run ONLY on a
subscription/private route (``openai-codex`` ChatGPT OAuth, or ``anthropic``
Claude Max / Claude Code OAuth) — never on a metered one.

These tests are the constraint-3 proof, anchored on the REAL compaction path:
``context_compressor._generate_summary`` → ``auxiliary_client.call_llm(task=
"compression")`` → ``_try_anthropic`` → ``anthropic_adapter`` token resolution.
"""

import pytest
from unittest.mock import MagicMock, patch

from agent import auxiliary_client as ac
from agent import anthropic_adapter as aa
from agent.auxiliary_client import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_OAUTH_SUBSCRIPTION,
    AnthropicAuxiliaryClient,
    CodexAuxiliaryClient,
    CompressionRoutingRejected,
    compression_route_guard,
)
from agent.context_compressor import ContextCompressor

OAUTH_TOKEN = "sk-ant-oat01-real-claude-max-token"
METERED_KEY = "sk-ant-api03-metered-console-key"
LEGACY_OAUTH_IN_API_KEY = "sk-ant-oat01-legacy-oauth-parked-in-api-key"

ANTHROPIC_ENV = (
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def isolate_anthropic_sources(monkeypatch):
    """Every test declares its own credential sources — start from none.

    Also neutralises the two non-env sources (Claude Code credential file and
    the credential pool) so a developer's real machine credentials cannot make
    a fail-closed test pass for the wrong reason.
    """
    for var in ANTHROPIC_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(aa, "read_claude_code_credentials", lambda: None)
    monkeypatch.setattr(aa, "_resolve_anthropic_pool_token", lambda: None)
    monkeypatch.setattr(ac, "_select_pool_entry", lambda provider: (False, None))
    yield


def _fake_anthropic_client(base_url="https://api.anthropic.com", is_oauth=True, token=OAUTH_TOKEN):
    """A real AnthropicAuxiliaryClient over a stub SDK client."""
    return AnthropicAuxiliaryClient(MagicMock(), "claude-haiku-4-5", token, base_url, is_oauth=is_oauth)


# ── 1. Anthropic provenance: shape beats source, scan-past, exhaustion ───────


class TestAnthropicProvenance:
    def test_metered_shape_classified_api_key(self):
        assert aa.classify_anthropic_auth_mode(METERED_KEY) == AUTH_MODE_API_KEY

    def test_oauth_shapes_classified_subscription(self):
        for tok in (OAUTH_TOKEN, "eyJhbGciOi.x.y", "cc-abc123"):
            assert aa.classify_anthropic_auth_mode(tok) == AUTH_MODE_OAUTH_SUBSCRIPTION

    def test_structural_oauth_cannot_launder_a_metered_shape(self):
        """A mislabelled pool entry must not buy a Console key an OAuth verdict."""
        assert aa.classify_anthropic_auth_mode(METERED_KEY, structural_oauth=True) == AUTH_MODE_API_KEY

    def test_legacy_oauth_parked_in_anthropic_api_key_is_allowed(self, monkeypatch):
        """Source var is NOT authoritative — token shape is (both directions)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", LEGACY_OAUTH_IN_API_KEY)
        cred = aa.resolve_anthropic_token_with_provenance(subscription_only=True)
        assert cred is not None
        assert cred.token == LEGACY_OAUTH_IN_API_KEY
        assert cred.source == aa.SOURCE_ANTHROPIC_API_KEY
        assert cred.mode == AUTH_MODE_OAUTH_SUBSCRIPTION

    def test_metered_key_parked_in_anthropic_token_is_refused(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_TOKEN", METERED_KEY)
        assert aa.resolve_anthropic_token_with_provenance(subscription_only=True) is None

    def test_scan_past_metered_env_token_to_later_oauth_source(self, monkeypatch):
        """THE mixed-source case (spec §5.3.1).

        resolve_anthropic_token() short-circuits at step 1, so a metered-shaped
        ANTHROPIC_TOKEN would mask a perfectly good Claude Max credential in the
        Claude Code store. The compaction resolver must scan PAST it.
        """
        monkeypatch.setenv("ANTHROPIC_TOKEN", METERED_KEY)
        monkeypatch.setattr(aa, "_resolve_anthropic_pool_token", lambda: OAUTH_TOKEN)

        skipped = []
        cred = aa.resolve_anthropic_token_with_provenance(
            subscription_only=True, on_skip=skipped.append,
        )
        assert cred is not None
        assert cred.token == OAUTH_TOKEN
        assert cred.source == aa.SOURCE_CREDENTIAL_POOL
        assert cred.mode == AUTH_MODE_OAUTH_SUBSCRIPTION
        # The skipped candidate is reported, not silently ignored.
        assert [c.source for c in skipped] == [aa.SOURCE_ANTHROPIC_TOKEN]

        # And the bare resolver still short-circuits — proving scan-past is a
        # real behavioural difference and not an accident of the environment.
        assert aa.resolve_anthropic_token() == METERED_KEY

    def test_exhaustion_every_source_metered_fails_closed(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_TOKEN", METERED_KEY)
        monkeypatch.setenv("ANTHROPIC_API_KEY", METERED_KEY)
        skipped = []
        cred = aa.resolve_anthropic_token_with_provenance(
            subscription_only=True, on_skip=skipped.append,
        )
        assert cred is None
        assert len(skipped) == 2  # skip and rejection are distinguishable in telemetry

    def test_candidate_order_pins_bare_resolver(self, monkeypatch):
        """Drift guard: the generator's first candidate IS resolve_anthropic_token()."""
        for env, tok in (
            ("ANTHROPIC_TOKEN", OAUTH_TOKEN),
            ("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_TOKEN),
            ("ANTHROPIC_API_KEY", METERED_KEY),
        ):
            for var in ANTHROPIC_ENV:
                monkeypatch.delenv(var, raising=False)
            monkeypatch.setenv(env, tok)
            first = next(aa._iter_anthropic_credential_candidates())
            assert first.token == aa.resolve_anthropic_token()


# ── 2. The real runtime path: _try_anthropic under the guard ────────────────


class TestTryAnthropicRouteGuard:
    def test_oauth_credential_accepted_on_real_path(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_TOKEN", OAUTH_TOKEN)
        with patch.object(aa, "build_anthropic_client", return_value=MagicMock()) as build:
            with compression_route_guard():
                client, model = ac._try_anthropic()
        assert client is not None
        assert client.is_oauth is True
        assert build.call_args[0][0] == OAUTH_TOKEN
        assert ac._client_compression_route(client) == ("anthropic", AUTH_MODE_OAUTH_SUBSCRIPTION)

    def test_metered_anthropic_api_key_rejected_no_client_built(self, monkeypatch):
        """The real hazard: no OAuth source, ANTHROPIC_API_KEY set."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", METERED_KEY)
        with patch.object(aa, "build_anthropic_client") as build:
            with compression_route_guard() as guard:
                client, model = ac._try_anthropic()
        assert client is None
        build.assert_not_called()  # no client constructed, no request issued
        assert ("anthropic", "metered_auth_mode") in guard.rejections

    def test_explicit_metered_api_key_does_not_bypass_guard(self, monkeypatch):
        """_try_anthropic(explicit_api_key=...) — an explicit metered key is still metered."""
        with patch.object(aa, "build_anthropic_client") as build:
            with compression_route_guard() as guard:
                client, _ = ac._try_anthropic(explicit_api_key=METERED_KEY)
        assert client is None
        build.assert_not_called()
        assert ("anthropic", "metered_auth_mode") in guard.rejections

    def test_scan_past_uses_later_oauth_and_never_builds_on_metered(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_TOKEN", METERED_KEY)
        monkeypatch.setattr(aa, "_resolve_anthropic_pool_token", lambda: OAUTH_TOKEN)
        with patch.object(aa, "build_anthropic_client", return_value=MagicMock()) as build:
            with compression_route_guard() as guard:
                client, _ = ac._try_anthropic()
        assert client is not None and client.is_oauth is True
        # The metered value was never handed to a client constructor.
        assert build.call_args[0][0] == OAUTH_TOKEN
        assert ("anthropic", aa.SOURCE_ANTHROPIC_TOKEN, "metered_shape") in guard.skips

    def test_non_compression_path_still_uses_metered_key(self, monkeypatch):
        """Regression: outside the guard, Anthropic behaviour is unchanged."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", METERED_KEY)
        with patch.object(aa, "build_anthropic_client", return_value=MagicMock()) as build:
            client, _ = ac._try_anthropic()  # no guard — e.g. title generation
        assert client is not None
        assert client.is_oauth is False
        assert build.call_args[0][0] == METERED_KEY


# ── 3. Route classification of the CONSTRUCTED client (aliasing traps) ──────


class TestClientRouteClassification:
    def test_anthropic_oauth_at_anthropic_host_allowed(self):
        assert ac._client_compression_route(_fake_anthropic_client()) == (
            "anthropic", AUTH_MODE_OAUTH_SUBSCRIPTION,
        )

    def test_anthropic_api_key_route_is_metered(self):
        client = _fake_anthropic_client(is_oauth=False, token=METERED_KEY)
        assert ac._client_compression_route(client) == ("anthropic", AUTH_MODE_API_KEY)
        with compression_route_guard():
            assert ac._compression_client_allowed("anthropic", client) is False

    def test_anthropic_wrapper_aliased_to_foreign_host_rejected(self):
        """_maybe_wrap_anthropic rewraps ANY Anthropic-Messages endpoint —
        the wrapper class alone proves nothing, the host must be Anthropic's."""
        aliased = _fake_anthropic_client(base_url="https://openrouter.ai/api/v1")
        assert ac._client_compression_route(aliased) is None
        with compression_route_guard():
            assert ac._compression_client_allowed("anthropic", aliased) is False

    def test_codex_oauth_route_allowed(self):
        real = MagicMock()
        real.api_key = "codex-oauth"
        real.base_url = "https://chatgpt.com/backend-api/codex"
        codex = CodexAuxiliaryClient(real, "gpt-5.6")
        assert ac._client_compression_route(codex) == ("openai-codex", AUTH_MODE_OAUTH_SUBSCRIPTION)
        with compression_route_guard():
            assert ac._compression_client_allowed("openai-codex", codex) is True

    def test_codex_wrapper_on_azure_host_rejected(self):
        real = MagicMock()
        real.api_key = "azure-key"
        real.base_url = "https://my-proj.openai.azure.com"
        codex = CodexAuxiliaryClient(real, "gpt-5.6")
        assert ac._client_compression_route(codex) is None

    @pytest.mark.parametrize("base_url", [
        "https://openrouter.ai/api/v1",
        "https://api.openai.com/v1",
        "https://generativelanguage.googleapis.com/v1beta",
        "http://localhost:1234/v1",
    ])
    def test_metered_and_custom_providers_rejected(self, base_url):
        plain = MagicMock()
        plain.base_url = base_url
        assert ac._client_compression_route(plain) is None
        with compression_route_guard() as guard:
            assert ac._compression_client_allowed("openrouter", plain) is False
        assert guard.rejections


# ── 4. Config surface: no opt-out, no widening ──────────────────────────────


class TestAllowedRoutesConfig:
    def _with_cfg(self, routes):
        cfg = {"auxiliary": {"compression": {"allowed_routes": routes}}}
        return patch("hermes_cli.config.load_config", return_value=cfg)

    def test_default_routes(self):
        with patch("hermes_cli.config.load_config", return_value={}):
            assert ac._allowed_compression_routes() == frozenset(
                ac.DEFAULT_ALLOWED_COMPRESSION_ROUTES
            )

    def test_narrowing_to_codex_only_is_allowed(self):
        with self._with_cfg([{"provider": "openai-codex", "auth_mode": "oauth_subscription"}]):
            assert ac._allowed_compression_routes() == frozenset(
                {("openai-codex", "oauth_subscription")}
            )

    def test_empty_list_is_a_config_error_not_an_opt_out(self):
        with self._with_cfg([]):
            with pytest.raises(CompressionRoutingRejected, match="configuration error"):
                ac._allowed_compression_routes()

    def test_metered_auth_mode_entry_rejected_at_load(self):
        with self._with_cfg([{"provider": "anthropic", "auth_mode": "api_key"}]):
            with pytest.raises(CompressionRoutingRejected, match="metered route"):
                ac._allowed_compression_routes()

    def test_metered_provider_entry_rejected_at_load(self):
        with self._with_cfg([{"provider": "openrouter", "auth_mode": "oauth_subscription"}]):
            with pytest.raises(CompressionRoutingRejected, match="no subscription route"):
                ac._allowed_compression_routes()

    def test_malformed_entry_rejected(self):
        with self._with_cfg(["openai-codex"]):
            with pytest.raises(CompressionRoutingRejected, match="malformed"):
                ac._allowed_compression_routes()


# ── 5. call_llm(task="compression") — end-to-end fail-closed ────────────────


class TestCallLlmCompressionGuard:
    def test_metered_anthropic_fails_closed_without_issuing_a_request(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", METERED_KEY)
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("anthropic", "claude-haiku-4-5", "", "", ""),
        )
        with patch.object(aa, "build_anthropic_client") as build:
            with pytest.raises(CompressionRoutingRejected):
                ac.call_llm(task="compression", messages=[{"role": "user", "content": "hi"}])
        build.assert_not_called()

    def test_guard_is_not_active_for_other_tasks(self, monkeypatch):
        """A metered route stays perfectly usable for title generation."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", METERED_KEY)
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("anthropic", "claude-haiku-4-5", "", "", ""),
        )
        assert ac._compression_guard_active() is False
        with patch.object(aa, "build_anthropic_client", return_value=MagicMock()) as build:
            client, _ = ac._get_cached_client("anthropic", "claude-haiku-4-5")
        assert client is not None and build.called

    def test_guard_state_is_cleared_after_the_call(self, monkeypatch):
        with compression_route_guard():
            assert ac._compression_guard_active() is True
        assert ac._compression_guard_active() is False

    def test_no_provider_at_all_fails_closed(self, monkeypatch):
        """Spec failure table: 'all allowed subscription routes unavailable' →
        fail CLOSED. An unconfigured install must abort with the session intact,
        NOT degrade to the generic no-provider RuntimeError, which the compressor
        would turn into a static placeholder + middle-window drop."""
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("auto", "", "", "", ""),
        )
        monkeypatch.setattr(ac, "_get_cached_client", lambda *a, **k: (None, None))
        with pytest.raises(CompressionRoutingRejected, match="No auxiliary provider is configured"):
            ac.call_llm(task="compression", messages=[{"role": "user", "content": "hi"}])

    def test_no_provider_for_other_tasks_still_raises_runtimeerror(self, monkeypatch):
        """Regression: the legacy no-provider error is untouched off the compaction path."""
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("auto", "", "", "", ""),
        )
        monkeypatch.setattr(ac, "_get_cached_client", lambda *a, **k: (None, None))
        with pytest.raises(RuntimeError, match="No LLM provider configured"):
            ac.call_llm(task="title_generation", messages=[{"role": "user", "content": "hi"}])


# ── 5b. async_call_llm(task="compression") — the async mirror ───────────────


class TestAsyncCallLlmCompressionGuard:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("base_url", [
        "https://api.openai.com/v1",
        "https://openrouter.ai/api/v1",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://my-proj.openai.azure.com",
        "http://localhost:1234/v1",
    ])
    async def test_metered_and_custom_async_routes_rejected_before_egress(
        self, monkeypatch, base_url,
    ):
        """The async surface must not be a hole in the guard."""
        client = MagicMock()
        client.base_url = base_url
        client.chat.completions.create = MagicMock(
            side_effect=AssertionError("a request was issued to a metered route!")
        )
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("openrouter", "some/model", "", "", ""),
        )
        monkeypatch.setattr(ac, "_get_cached_client", lambda *a, **k: (client, "some/model"))
        monkeypatch.setattr(
            ac, "_try_configured_fallback_for_unavailable_client",
            lambda *a, **k: (None, None, ""),
        )

        with pytest.raises(CompressionRoutingRejected):
            await ac.async_call_llm(
                task="compression", messages=[{"role": "user", "content": "hi"}],
            )
        client.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_metered_anthropic_api_key_route_rejected(self, monkeypatch):
        metered = _fake_anthropic_client(is_oauth=False, token=METERED_KEY)
        metered.chat = MagicMock()
        metered.chat.completions.create = MagicMock(
            side_effect=AssertionError("a request was issued on the metered Anthropic route!")
        )
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("anthropic", "claude-haiku-4-5", "", "", ""),
        )
        monkeypatch.setattr(ac, "_get_cached_client", lambda *a, **k: (metered, "claude-haiku-4-5"))
        monkeypatch.setattr(
            ac, "_try_configured_fallback_for_unavailable_client",
            lambda *a, **k: (None, None, ""),
        )
        with pytest.raises(CompressionRoutingRejected):
            await ac.async_call_llm(
                task="compression", messages=[{"role": "user", "content": "hi"}],
            )
        metered.chat.completions.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_non_compression_task_is_not_guarded(self, monkeypatch):
        """Regression: a metered async route still serves non-compaction tasks."""
        response = MagicMock()
        response.choices = [MagicMock()]

        class _Completions:
            async def create(self, **kwargs):
                return response

        client = MagicMock()
        client.base_url = "https://openrouter.ai/api/v1"
        client.chat.completions = _Completions()

        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("openrouter", "some/model", "", "", ""),
        )
        monkeypatch.setattr(ac, "_get_cached_client", lambda *a, **k: (client, "some/model"))
        monkeypatch.setattr(ac, "_validate_llm_response", lambda resp, task, **_kw: resp)

        out = await ac.async_call_llm(
            task="title_generation", messages=[{"role": "user", "content": "hi"}],
        )
        assert out is response

    @pytest.mark.asyncio
    async def test_async_oauth_anthropic_route_is_admitted(self, monkeypatch):
        response = MagicMock()

        class _Completions:
            async def create(self, **kwargs):
                return response

        oauth = _fake_anthropic_client(is_oauth=True)
        oauth.chat = MagicMock()
        oauth.chat.completions = _Completions()

        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("anthropic", "claude-haiku-4-5", "", "", ""),
        )
        monkeypatch.setattr(ac, "_get_cached_client", lambda *a, **k: (oauth, "claude-haiku-4-5"))
        monkeypatch.setattr(ac, "_validate_llm_response", lambda resp, task, **_kw: resp)

        out = await ac.async_call_llm(
            task="compression", messages=[{"role": "user", "content": "hi"}],
        )
        assert out is response

    @pytest.mark.asyncio
    async def test_guard_does_not_leak_into_concurrent_async_tasks(self, monkeypatch):
        """The guard must be TASK-local, not thread-local.

        Every coroutine on an event loop shares one thread. With thread-local
        state, a compaction call that is suspended at an ``await`` leaves the
        guard visible to any other coroutine that runs in the meantime — so an
        unrelated auxiliary call (title generation) on a perfectly legitimate
        metered route would be screened as compaction and fail with a spurious
        CompressionRoutingRejected. This test pins the isolation.
        """
        import asyncio

        compression_started = asyncio.Event()
        release_compression = asyncio.Event()

        # The compaction call: an allowlisted OAuth Anthropic route that
        # SUSPENDS mid-request, holding the guard across the await.
        compression_response = MagicMock()

        class _SlowCompletions:
            async def create(self, **kwargs):
                compression_started.set()
                await release_compression.wait()  # guard is held across this await
                return compression_response

        oauth = _fake_anthropic_client(is_oauth=True)
        oauth.chat = MagicMock()
        oauth.chat.completions = _SlowCompletions()

        # The concurrent, unrelated task: a metered OpenRouter route, which is
        # perfectly valid for title generation.
        title_response = MagicMock()

        class _FastCompletions:
            async def create(self, **kwargs):
                return title_response

        metered = MagicMock()
        metered.base_url = "https://openrouter.ai/api/v1"
        metered.chat.completions = _FastCompletions()

        def _resolve(task, *a, **k):
            if task == "compression":
                return ("anthropic", "claude-haiku-4-5", "", "", "")
            return ("openrouter", "some/model", "", "", "")

        def _cached(provider, *a, **k):
            return (oauth, "claude-haiku-4-5") if provider == "anthropic" else (metered, "some/model")

        monkeypatch.setattr(ac, "_resolve_task_provider_model", _resolve)
        monkeypatch.setattr(ac, "_get_cached_client", _cached)
        monkeypatch.setattr(ac, "_validate_llm_response", lambda resp, task, **_kw: resp)

        compression = asyncio.create_task(ac.async_call_llm(
            task="compression", messages=[{"role": "user", "content": "summarise"}],
        ))
        await compression_started.wait()   # compaction is now suspended, guard held

        # This must NOT see the other coroutine's guard.
        assert ac._compression_guard_active() is False
        title = await ac.async_call_llm(
            task="title_generation", messages=[{"role": "user", "content": "title"}],
        )
        assert title is title_response     # served on the metered route, unscreened

        release_compression.set()
        assert await compression is compression_response

    @pytest.mark.asyncio
    async def test_async_no_provider_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("auto", "", "", "", ""),
        )
        monkeypatch.setattr(ac, "_get_cached_client", lambda *a, **k: (None, None))
        with pytest.raises(CompressionRoutingRejected):
            await ac.async_call_llm(
                task="compression", messages=[{"role": "user", "content": "hi"}],
            )


# ── 6. CompressionRoutingRejected propagation through the compressor ────────


def _compressor(abort_on_summary_failure=False):
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(
            model="main/model",
            summary_model_override="aux/model",  # arms the main-model fallback path
            threshold_percent=0.5,
            protect_first_n=1,
            protect_last_n=2,
            quiet_mode=True,
            abort_on_summary_failure=abort_on_summary_failure,
        )


def _messages(n=14):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n):
        msgs.append({"role": "user", "content": f"u{i} " * 50})
        msgs.append({"role": "assistant", "content": f"a{i} " * 50})
    return msgs


class TestRoutingRejectionPropagation:
    def test_generate_summary_does_not_swallow_the_rejection(self):
        c = _compressor()
        with patch("agent.context_compressor.call_llm", side_effect=CompressionRoutingRejected("no route")):
            with patch.object(c, "_fallback_to_main_for_compression") as fb:
                with pytest.raises(CompressionRoutingRejected):
                    c._generate_summary([{"role": "user", "content": "x"}])
        # Never retried on the MAIN MODEL — the second egress escape (:2142).
        fb.assert_not_called()
        assert c._last_summary_routing_rejected is True
        # Not recorded as a summary failure; no cooldown armed.
        assert c._last_summary_error is None
        assert c._summary_failure_cooldown_until == 0.0

    @pytest.mark.parametrize("abort_flag", [False, True])
    def test_compress_aborts_with_messages_unchanged(self, abort_flag):
        """The privacy refusal is independent of abort_on_summary_failure."""
        c = _compressor(abort_on_summary_failure=abort_flag)
        msgs = _messages()
        before = [dict(m) for m in msgs]

        with patch("agent.context_compressor.call_llm",
                   side_effect=CompressionRoutingRejected("no allowed subscription route")):
            with patch.object(c, "_build_static_fallback_summary") as static:
                with patch.object(c, "_fallback_to_main_for_compression") as fb:
                    result = c.compress(msgs, current_tokens=90000)

        # Messages returned COMPLETELY unchanged — no middle-window drop.
        assert result == before
        assert len(result) == len(before)
        # No static placeholder summary was inserted.
        static.assert_not_called()
        assert not any("summary unavailable" in str(m.get("content", "")).lower() for m in result)
        # No main-model retry.
        fb.assert_not_called()
        # Abort is signalled to callers (so no archive/rotation downstream).
        assert c._last_compress_aborted is True
        assert c._last_summary_routing_rejected is True
        assert c._last_summary_fallback_used is False
        assert c._last_summary_dropped_count == 0

    @pytest.mark.parametrize("abort_flag", [False, True])
    def test_no_provider_configured_aborts_unchanged(self, abort_flag, monkeypatch):
        """No allowed subscription route AND nothing configured → still fail closed.

        Before the carve-out was removed this fell through to the generic
        no-provider RuntimeError, and with abort_on_summary_failure=False the
        compressor dropped the middle window behind a static placeholder.
        """
        for var in ANTHROPIC_ENV:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(
            ac, "_resolve_task_provider_model", lambda *a, **k: ("auto", "", "", "", ""),
        )
        monkeypatch.setattr(ac, "_get_cached_client", lambda *a, **k: (None, None))

        c = _compressor(abort_on_summary_failure=abort_flag)
        msgs = _messages()
        before = [dict(m) for m in msgs]
        result = c.compress(msgs, current_tokens=90000)

        assert result == before
        assert c._last_compress_aborted is True
        assert c._last_summary_routing_rejected is True
        assert c._last_summary_fallback_used is False
        assert c._last_summary_dropped_count == 0

    def test_routing_message_is_distinct_and_actionable(self):
        c = _compressor()
        with patch("agent.context_compressor.call_llm",
                   side_effect=CompressionRoutingRejected("Compaction refused: no allowed subscription route.")):
            c.compress(_messages(), current_tokens=90000)
        # compress_context() surfaces this instead of a bare "unknown error";
        # it is deliberately NOT stored in _last_summary_error.
        assert "subscription route" in c._last_summary_routing_message
        assert c._last_summary_error is None

    def test_ordinary_summary_failure_still_uses_the_legacy_path(self):
        """Regression: a normal provider error keeps its main-model retry.

        Spy (``wraps=``) rather than replace: the real method sets
        ``_summary_model_fallen_back``, which is what terminates the retry.
        """
        c = _compressor()
        with patch("agent.context_compressor.call_llm", side_effect=RuntimeError("boom")):
            with patch.object(
                c, "_fallback_to_main_for_compression",
                wraps=c._fallback_to_main_for_compression,
            ) as fb:
                result = c._generate_summary([{"role": "user", "content": "x"}])
        # The ordinary path DOES retry on the main model, then gives up with a
        # recorded summary failure — exactly the behaviour a routing rejection
        # must NOT get.
        fb.assert_called_once()
        assert result is None
        assert c._last_summary_error
        assert c._last_summary_routing_rejected is False
