"""hardening-sweep S1-e: `_fallback_session_info` must describe the SESSION, not the launch.

Pre-existing defects proven in the convoy-resume reviews (agy S2-6): the lazy-path info ignored
``session["model_override"]``, omitted ``profile_name``, resolved the model from the launch
profile's config, and ignored the compute-host metadata mirror. Known residual, recorded: an
ambient ``HERMES_MODEL`` env seed still wins inside ``_resolve_model`` (launch semantics, every
caller); the fix here scopes the CONFIG read and honours the session's own override/mirror first.
"""

from __future__ import annotations

import threading

from tui_gateway import server


def _lazy_session(tmp_path, **extra):
    session = dict(agent=None, history=[], display_history_prefix=[],
                   history_lock=threading.Lock(), session_key="s", created_at=1.0,
                   cwd=str(tmp_path))
    session.update(extra)
    return session


def test_fallback_info_honours_the_sessions_model_override(tmp_path):
    session = _lazy_session(tmp_path, model_override="custom/override-model")
    info = server._fallback_session_info(session)
    assert info["model"] == "custom/override-model"


def test_fallback_info_names_the_sessions_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "profile_name_for_home", lambda home: "prof-x" if home else "")
    session = _lazy_session(tmp_path, profile_home=str(tmp_path / "prof"))
    info = server._fallback_session_info(session)
    assert info.get("profile_name") == "prof-x"


def test_fallback_info_resolves_the_model_from_the_sessions_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    home = tmp_path / "prof-home"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  default: profile/own-model\n", encoding="utf-8")
    session = _lazy_session(tmp_path, profile_home=str(home))
    info = server._fallback_session_info(session)
    assert info["model"] == "profile/own-model", (
        f"the lazy info resolved the LAUNCH profile's model, not the session's: {info['model']!r}")


def test_a_dict_shaped_override_yields_its_model_and_never_its_credentials(tmp_path):
    """agy sweep-S1-1 (P1): production stores ``model_override`` as a DICT (model/provider/
    base_url/api_key). The info must carry the model STRING — never a dict repr, never a key."""
    session = _lazy_session(tmp_path, model_override={
        "model": "custom/dict-model", "provider": "custom", "api_key": "sk-SECRET"})
    info = server._fallback_session_info(session)
    assert info["model"] == "custom/dict-model"
    assert "sk-SECRET" not in repr(info), "a credential from the override reached the info payload"


def test_the_live_agent_arm_passes_the_session_through(tmp_path, monkeypatch):
    """agy sweep-S1-2 (P2): `_session_info` must receive the session (no unlocked registry walk)."""
    from types import SimpleNamespace

    agent = SimpleNamespace()
    session = _lazy_session(tmp_path, agent=agent)
    seen = {}

    def record_session_info(agent_, session_=None):
        seen["session"] = session_
        return {"model": "m"}

    monkeypatch.setattr(server, "_session_info", record_session_info)
    server._fallback_session_info(session)
    assert seen["session"] is session, "_session_info was called without the session"


def test_a_compute_host_mirror_model_beats_a_stale_stored_override(tmp_path):
    """agy sweep-S1-3 (P2): on an isolated (compute-host) session, /model switches land in the
    mirror while the parent's cold-seeded override goes stale — the mirror is the live truth."""
    session = _lazy_session(tmp_path, _compute_host_active=True, _compute_host_turn_id="t1",
                            model_override={"model": "stale/pre-switch"},
                            _metadata_mirror={"model": "host/live-model"})
    info = server._fallback_session_info(session)
    assert info["model"] == "host/live-model"


def test_fallback_info_prefers_the_compute_host_mirror_model(tmp_path):
    session = _lazy_session(tmp_path, _compute_host_active=True, _compute_host_turn_id="t1",
                            _metadata_mirror={"model": "host/mirrored-model"})
    info = server._fallback_session_info(session)
    assert info["model"] == "host/mirrored-model"


def test_a_torn_down_hosts_stale_mirror_never_beats_the_override(tmp_path):
    """agy R2-5: `_compute_host_active` can stay True after isolation toggle-off / host teardown
    (nothing clears it until the next admission). The mirror speaks only for a session the host
    genuinely still owns (`_session_uses_compute_host`); otherwise the stored override governs."""
    session = _lazy_session(tmp_path, _compute_host_active=True,  # stale: no turn, isolation off
                            model_override={"model": "fresh/override"},
                            _metadata_mirror={"model": "stale/host-model"})
    info = server._fallback_session_info(session)
    assert info["model"] == "fresh/override"
