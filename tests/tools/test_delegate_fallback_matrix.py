"""Pin x delegation.fallback_providers decision table for child agents (#80450, #65038).

R11: a DECLARED chain is no longer installed as given — it is replaced by what the
declaration's own configuration owner authorizes under a fail-closed, bypass-aware read
(``delegate_tool_config._authorized_child_chain``), because a declaration the clean config does
not contain was reaching a brand-new child agent (R10-2). The declared-chain rows below therefore
write the chain into the home's ``config.yaml``, which is where production reads it from:
``routing_cfg`` IS the ``delegation`` section of that file. The pin/absent/empty/malformed rows
are unaffected and still run without any config at all.
"""

from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import _build_child_agent
from tools.delegate_tool_config import _resolve_child_fallback_chain
from tests.tools.test_delegate import _make_mock_parent

PARENT_CHAIN = [
    {"provider": "openrouter", "model": "gpt-4o-mini", "api_key": "sk-or-parent"}
]
DECLARED_CHAIN = [
    {"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-ds-child"}
]


def _parent(chain=None):
    parent = _make_mock_parent(depth=0)
    parent._fallback_chain = chain
    return parent


def _clear_chain_caches():
    """Every config cache AND the shared per-home chain retention — a fresh process's state.

    The authorization read publishes a clean observation into the per-home retention
    (``gateway/run_config_loaders``), so without this a chain observed by one test could be
    served to another through the retention rather than read from its own config.
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


def _home_declaring(tmp_path, monkeypatch, declared):
    """Point ``HERMES_HOME`` at *tmp_path* and declare *declared* as the delegation chain there."""
    import yaml

    from hermes_constants import set_hermes_home_override

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"delegation": {"fallback_providers": declared}}), encoding="utf-8")
    token = set_hermes_home_override(tmp_path)
    _clear_chain_caches()
    return token


@pytest.fixture
def declaring_home(tmp_path, monkeypatch):
    """A clean ``HERMES_HOME`` whose ``config.yaml`` declares ``DECLARED_CHAIN`` for delegation.

    This is the configuration the declared-chain rows describe: the authorization read finds the
    same chain ``routing_cfg`` carries, so it installs. Without a home the read authorizes nothing
    and the declaration is correctly refused (that arm is covered in the R11 suite).
    """
    from hermes_constants import reset_hermes_home_override

    token = _home_declaring(tmp_path, monkeypatch, list(DECLARED_CHAIN))
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)
        _clear_chain_caches()


# (pinned, declared fallback_providers, expected) — the cells that need no declaring config.
@pytest.mark.parametrize(
    ("pinned", "declared", "expected"),
    [
        (True, "absent", None),                 # #80450: a pinned child fails loudly, never reroutes
        (True, None, None),
        (True, [], None),
        (False, "absent", PARENT_CHAIN),        # historical default preserved
        (False, None, PARENT_CHAIN),
        (False, [], None),                      # explicit [] disables fallback
        (True, "not-a-list", None),
        (False, "not-a-list", PARENT_CHAIN),
        (True, [{"provider": "deepseek"}], None),
        (False, [{"provider": "deepseek"}], PARENT_CHAIN),
    ],
)
def test_child_fallback_chain_matrix(pinned, declared, expected):
    cfg = {} if declared == "absent" else {"fallback_providers": declared}
    assert _resolve_child_fallback_chain(_parent(list(PARENT_CHAIN)), cfg, pinned=pinned) == expected


# The declared-chain cells: the chain is authorized by the home's own clean config (#65038).
@pytest.mark.parametrize("pinned", [True, False])
def test_a_declared_chain_the_config_authorizes_reaches_the_child(pinned, declaring_home):
    assert _resolve_child_fallback_chain(
        _parent(list(PARENT_CHAIN)), {"fallback_providers": list(DECLARED_CHAIN)}, pinned=pinned
    ) == DECLARED_CHAIN


def test_a_malformed_neighbour_does_not_sink_a_valid_declared_route(tmp_path, monkeypatch):
    """A valid route survives a malformed neighbour — through the authorization read."""
    from hermes_constants import reset_hermes_home_override

    declared = [{"provider": "deepseek", "model": "x"}, {"provider": "deepseek"}]
    token = _home_declaring(tmp_path, monkeypatch, declared)
    try:
        assert _resolve_child_fallback_chain(
            _parent(list(PARENT_CHAIN)), {"fallback_providers": declared}, pinned=False
        ) == [{"provider": "deepseek", "model": "x"}]
    finally:
        reset_hermes_home_override(token)
        _clear_chain_caches()


def _spawn_kwargs(parent, cfg, **overrides):
    model = overrides.pop("model", None)
    with patch("tools.delegate_tool._load_config", return_value=cfg), patch("run_agent.AIAgent") as MockAgent:
        MockAgent.return_value = MagicMock()
        _build_child_agent(task_index=0, goal="matrix wiring", context=None, toolsets=None, model=model,
                           max_iterations=10, parent_agent=parent, task_count=1, **overrides)
    return MockAgent.call_args[1]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, PARENT_CHAIN),                                                    # unpinned inherits
        ({"model": "deepseek-chat"}, None),                                    # model-only pin (#80450 model arm)
        ({"override_provider": "minimax", "override_base_url": "https://api.minimax.example/v1",
          "override_api_key": "sk-mm"}, None),                                 # provider pin
    ],
)
def test_pin_is_derived_from_provider_base_url_or_model(overrides, expected):
    assert _spawn_kwargs(_parent(list(PARENT_CHAIN)), {}, **overrides)["fallback_model"] == expected


def test_declared_chain_flows_through_real_profile_config_loader(
    tmp_path, monkeypatch
):
    """The public key must survive DEFAULT_CONFIG/profile loading without
    patching ``_load_config`` and reach the child constructor."""
    import yaml

    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    token = set_hermes_home_override(tmp_path)
    try:
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {"delegation": {"fallback_providers": list(DECLARED_CHAIN)}}
            ),
            encoding="utf-8",
        )
        with patch("run_agent.AIAgent") as mock_agent:
            mock_agent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="real config loader",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=_parent(list(PARENT_CHAIN)),
                task_count=1,
            )
    finally:
        reset_hermes_home_override(token)

    child_kwargs = mock_agent.call_args.kwargs
    assert child_kwargs["fallback_model"] == DECLARED_CHAIN


def test_explicit_empty_chain_survives_real_profile_config_loader(tmp_path, monkeypatch):
    """An explicit [] remains an authoritative disable after config loading."""
    import yaml

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    token = set_hermes_home_override(tmp_path)
    try:
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"delegation": {"fallback_providers": []}}),
            encoding="utf-8",
        )
        with patch("run_agent.AIAgent") as mock_agent:
            mock_agent.return_value = MagicMock()
            _build_child_agent(
                task_index=0,
                goal="explicit disable",
                context=None,
                toolsets=None,
                model=None,
                max_iterations=10,
                parent_agent=_parent(list(PARENT_CHAIN)),
                task_count=1,
            )
    finally:
        reset_hermes_home_override(token)

    assert mock_agent.call_args.kwargs["fallback_model"] is None


def test_pinned_review_does_not_borrow_general_worker_chain(tmp_path, monkeypatch):
    """The public /review route owns its fallback policy as well as its model."""
    import yaml

    from agent.review_engine import start_review
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "delegation": {
                    "fallback_providers": [
                        {"provider": "deepseek", "model": "worker-fallback"}
                    ]
                },
                "auxiliary": {
                    "review": {
                        "provider": "custom",
                        "model": "review-model",
                        "base_url": "http://127.0.0.1:18479/v1",
                        "api_key": "test-only",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    parent = _parent(list(PARENT_CHAIN))
    parent.session_id = "review-80479-parent"
    captured = {}

    class ReachedConstructor(RuntimeError):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise ReachedConstructor()

    token = set_hermes_home_override(tmp_path)
    try:
        with patch("run_agent.AIAgent", side_effect=capture):
            with pytest.raises(ReachedConstructor):
                start_review(
                    parent,
                    [{"role": "user", "content": "Check the last result"}],
                )
    finally:
        reset_hermes_home_override(token)

    assert captured["model"] == "review-model"
    assert captured["base_url"] == "http://127.0.0.1:18479/v1"
    assert captured["fallback_model"] is None


def test_declared_child_chain_activates_on_primary_failure(declaring_home):
    """The selected chain is accepted by the real fallback activation rail."""
    from agent.error_classifier import FailoverReason
    from run_agent import AIAgent

    chain = _resolve_child_fallback_chain(
        _parent(list(PARENT_CHAIN)),
        {"fallback_providers": list(DECLARED_CHAIN)},
        pinned=True,
    )
    assert chain == DECLARED_CHAIN
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        child = AIAgent(
            api_key="primary-test-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=chain,
        )
    fallback_client = MagicMock()
    fallback_client.base_url = "https://fallback.example/v1"
    fallback_client.api_key = "fallback-test-key"
    with (
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fallback_client, "deepseek-chat"),
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, _provider: model,
        ),
    ):
        assert child._try_activate_fallback(FailoverReason.rate_limit) is True

    assert child.model == "deepseek-chat"
    assert child.provider == "deepseek"


if __name__ == "__main__":
    unittest.main()
