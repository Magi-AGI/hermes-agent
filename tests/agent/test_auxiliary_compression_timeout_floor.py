"""Regression tests for the compression-scoped auxiliary timeout floor (#54915).

Context compression summarises large conversation histories.  When the
resolved auxiliary provider is a reasoning model (e.g. Codex / GPT-5.5) the
summary can legitimately exceed the default ``auxiliary.compression.timeout``
of 120 s, causing the stream to time out and the compressor to fall back to a
deterministic context marker — silently losing the LLM summary.

The fix layers a *bounded* timeout floor on top of the config-derived
compression timeout, while honouring the four constraints from the issue:

  * Only the ``compression`` task gets the floor (other auxiliary tasks keep
    their own timeouts).
  * An explicit per-call ``timeout=`` override is **not** floored.
  * The floor is a minimum — a config value already above it is unchanged.
  * Both the sync (``call_llm``) and async (``async_call_llm``) paths are
    covered.

These tests exercise the real ``call_llm`` / ``async_call_llm`` production
paths with a mocked LLM client and assert the timeout that actually reaches
``client.chat.completions.create``.
"""

from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from agent.auxiliary_client import CodexAuxiliaryClient, call_llm, async_call_llm

# The committed bounded floor for config-derived compression timeouts.
# Behaviour contract (see AGENTS.md "Behavior contracts over snapshots"):
# compression's effective timeout must be at least this when it is
# config-derived.
COMPRESSION_TIMEOUT_FLOOR = 300.0

# The default ``auxiliary.compression.timeout`` shipped in the config schema
# (hermes_cli/config.py).  Simulated here as the config-derived value.
COMPRESSION_CONFIG_TIMEOUT = 120.0


def _ok_response():
    return {"ok": True}


# These tests declare provider="openai-codex", so the client they hand back must
# actually BE a Codex-routed client. The compaction route guard
# (agent/auxiliary_client.py) screens the CONSTRUCTED client, not the provider
# label — a bare MagicMock on api.openai.com is the metered OpenAI API route no
# matter what the label says, and is correctly refused. So build a real
# ``CodexAuxiliaryClient`` on the ChatGPT OAuth endpoint (the allowlisted
# subscription route) and stub only its ``chat`` shim so the timeout that reaches
# the wire stays observable.
CODEX_OAUTH_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _codex_client(create_mock):
    inner = MagicMock()
    inner.api_key = "codex-oauth-token"
    inner.base_url = CODEX_OAUTH_BASE_URL
    client = CodexAuxiliaryClient(inner, "gpt-5.5")
    client.chat = MagicMock()
    client.chat.completions.create = create_mock
    return client


def _client_sync():
    return _codex_client(MagicMock(return_value=_ok_response()))


def _client_async():
    return _codex_client(AsyncMock(return_value=_ok_response()))


def _patches(client, *, task_timeout):
    """Common mocks: provider resolution, cached client, response validation,
    and the config-derived task timeout."""
    return (
        patch("agent.auxiliary_client._resolve_task_provider_model",
              return_value=("openai-codex", "gpt-5.5", None, None, None)),
        patch("agent.auxiliary_client._get_cached_client",
              return_value=(client, "gpt-5.5")),
        patch("agent.auxiliary_client._validate_llm_response",
              side_effect=lambda resp, _task, **_kw: resp),
        patch("agent.auxiliary_client._get_task_timeout",
              return_value=task_timeout),
    )


class TestCompressionTimeoutFloorSync:
    """Sync ``call_llm`` applies the floor to config-derived compression timeouts."""

    def test_config_derived_compression_timeout_is_raised_to_floor(self):
        """Layer 1: compression with a 120 s config timeout must reach the
        client with at least the 300 s floor."""
        client = _client_sync()
        p1, p2, p3, p4 = _patches(client, task_timeout=COMPRESSION_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarise this"}],
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout >= COMPRESSION_TIMEOUT_FLOOR, (
            f"compression timeout {timeout} should be >= floor "
            f"{COMPRESSION_TIMEOUT_FLOOR}"
        )
        assert timeout > COMPRESSION_CONFIG_TIMEOUT, (
            "the too-low config timeout must not pass through unchanged"
        )

    def test_explicit_per_call_timeout_is_not_floored(self):
        """Layer 3: an explicit per-call ``timeout=`` override is honoured
        even when it is below the floor."""
        client = _client_sync()
        explicit = 60.0
        p1, p2, p3, p4 = _patches(client, task_timeout=COMPRESSION_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "x"}],
                timeout=explicit,
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout == explicit, (
            f"explicit per-call timeout {explicit} must not be floored, got {timeout}"
        )

    def test_non_compression_task_is_not_floored(self):
        """Layer 4: only ``compression`` gets the floor; another auxiliary
        task with the same low config timeout must pass it through."""
        client = _client_sync()
        low = 30.0
        p1, p2, p3, p4 = _patches(client, task_timeout=low)
        with p1, p2, p3, p4:
            call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "x"}],
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout == low, (
            f"non-compression task timeout must stay {low}, got {timeout}"
        )

    def test_higher_config_timeout_is_not_lowered(self):
        """Layer 5: the floor is a minimum — a config value already above it
        is kept unchanged (``max`` semantics)."""
        client = _client_sync()
        high = 600.0
        p1, p2, p3, p4 = _patches(client, task_timeout=high)
        with p1, p2, p3, p4:
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "x"}],
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout == high, (
            f"config timeout {high} above the floor must be unchanged, got {timeout}"
        )


class TestCompressionTimeoutFloorAsync:
    """Async ``async_call_llm`` mirrors the sync floor (Layer 2)."""

    @pytest.mark.asyncio
    async def test_async_config_derived_compression_timeout_is_raised_to_floor(self):
        client = _client_async()
        p1, p2, p3, p4 = _patches(client, task_timeout=COMPRESSION_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            await async_call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarise this"}],
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout >= COMPRESSION_TIMEOUT_FLOOR, (
            f"async compression timeout {timeout} should be >= floor "
            f"{COMPRESSION_TIMEOUT_FLOOR}"
        )

    @pytest.mark.asyncio
    async def test_async_explicit_per_call_timeout_is_not_floored(self):
        client = _client_async()
        explicit = 45.0
        p1, p2, p3, p4 = _patches(client, task_timeout=COMPRESSION_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            await async_call_llm(
                task="compression",
                messages=[{"role": "user", "content": "x"}],
                timeout=explicit,
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout == explicit

    @pytest.mark.asyncio
    async def test_async_non_compression_task_is_not_floored(self):
        client = _client_async()
        low = 30.0
        p1, p2, p3, p4 = _patches(client, task_timeout=low)
        with p1, p2, p3, p4:
            await async_call_llm(
                task="session_search",
                messages=[{"role": "user", "content": "x"}],
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout == low
