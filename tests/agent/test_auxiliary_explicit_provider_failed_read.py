"""CA-P2-3: explicit ``--provider`` sessions and their auxiliary routes under a failed config read.

Finding (new, beyond the REPORT-ROUND5 wording): the A44 provenance never REACHES the auxiliary
layer at all. ``hermes_cli/runtime_provider.py`` is the only writer of ``runtime["selection"]``
(:1029-1031), ``agent/auxiliary_client.py`` declares ``"selection"`` in
``_MAIN_RUNTIME_CONTEXT_FIELDS`` (:3700-3703) and all three auxiliary publication sites read
``runtime.get("selection")`` (:5354, :6219, :6375) -- but ``set_runtime_main`` (:2708-2737, the
only producer of that context) never writes the key, and ``agent._provider_selection`` (read at
``agent/agent_init.py``:840) is never set by anything. So ``selection`` is always ``None`` on every
auxiliary route, and the A44 ``selection == "explicit"`` allowance can never fire there.

Direction of failure: strictly MORE refusal. An explicit ``--provider X`` session loses its
auxiliary routes (compression, vision, titles, goal judging) during the window in which
config.yaml does not read cleanly, unless X happens to equal the last-known-good pin. No
unauthorized provider is ever published by this gap.

The first test pins the CURRENT deliberate fail-closed semantics (passing). The second pins the
DESIRED contract and is ``xfail(strict=True)``: it fails loudly once the provenance is plumbed,
which is the P3 follow-up recorded for this item.
"""

from __future__ import annotations

import pytest


def _clear_config_caches(config):
    for cache in (config._LOAD_CONFIG_CACHE, config._RAW_CONFIG_CACHE,
                  config._LAST_EXPANDED_CONFIG_BY_PATH):
        cache.clear()


def _break_config_after_clean_read(tmp_path, monkeypatch, config):
    """A home whose config read cleanly (pinning anthropic) and is now malformed."""
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("model:\n  provider: anthropic\n  default: claude-sonnet-4\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _clear_config_caches(config)
    assert config.load_config_with_status()[1] is None
    path.write_text("model: [save in progress\n", encoding="utf-8")
    assert config.current_config_read_failure(), "the config did not fail to read"
    return home


def test_auxiliary_route_to_a_non_pinned_provider_is_refused_during_a_failed_read(tmp_path, monkeypatch):
    """The fail-closed half of A44, which the gap cannot weaken: a non-pin provider is refused.

    This is the behaviour an explicit ``--provider gemini`` session sees for its auxiliary routes
    while the config is broken, and it is deliberate: the config's pin is anthropic, so gemini is
    not authorised by the served last-known-good config.
    """
    from agent import auxiliary_client as aux
    from hermes_cli import config

    _break_config_after_clean_read(tmp_path, monkeypatch, config)
    try:
        assert aux._route_blocked("gemini", None, "compression") is True
    finally:
        _clear_config_caches(config)


def test_the_pinned_provider_still_serves_auxiliary_routes_during_a_failed_read(tmp_path, monkeypatch):
    """The last-known-good pin keeps working, so a broken save does not stop every side task."""
    from agent import auxiliary_client as aux
    from hermes_cli import config

    _break_config_after_clean_read(tmp_path, monkeypatch, config)
    try:
        assert aux._route_blocked("anthropic", None, "compression") is False
    finally:
        _clear_config_caches(config)


@pytest.mark.xfail(strict=True, reason=(
    "CA-P2-3 (P3 follow-up, reclassified with evidence 2026-10-05): the A44 'selection' provenance "
    "is never plumbed to the auxiliary layer -- set_runtime_main does not publish the key it declares "
    "in _MAIN_RUNTIME_CONTEXT_FIELDS, and agent._provider_selection is never set -- so an explicit "
    "--provider session's auxiliary route to its OWN provider is refused during a failed read. "
    "Fails loudly once the provenance is plumbed through agent construction."))
def test_explicit_provider_session_keeps_auxiliary_routes_to_its_own_provider(tmp_path, monkeypatch):
    """The desired contract: ``--provider gemini`` authorises gemini for this run, aux included.

    An explicit per-run choice is the user's live instruction, not an implicit pick from a file
    that is currently unreadable, so the auxiliary route to that SAME provider should inherit the
    authorization the main route already has (``auth.provider_blocked_by_config_failure`` allows
    ``selection == "explicit"``).
    """
    from agent import auxiliary_client as aux
    from hermes_cli import config

    _break_config_after_clean_read(tmp_path, monkeypatch, config)
    token = None
    try:
        # A turn whose main runtime is the explicitly chosen provider, bound exactly as
        # agent/turn_context.py binds it for the turn.
        token = aux.set_runtime_main("gemini", "gemini-2.5-flash", requested_provider="gemini")
        runtime = aux._normalize_main_runtime(None)
        assert runtime.get("provider") == "gemini"
        assert runtime.get("selection") == "explicit", (
            "the main runtime carries no A44 provenance, so the auxiliary layer cannot know the "
            "provider was chosen explicitly this run")
        assert aux._route_blocked("gemini", runtime.get("selection"), "compression") is False
    finally:
        if token is not None:
            aux.reset_runtime_main(token)
        _clear_config_caches(config)
