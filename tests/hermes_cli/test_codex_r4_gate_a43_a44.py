"""Codex mcp-hbhc closure r4 probes for deploy gates A43, A44, A45, adopted from
review-artifacts/mcp-hbhc/codex-closure-r4/root/test_gate_a43_a44.py (one documented edit in the
first A44 probe: a corrupt_config refusal counts as not selecting the env provider; and, since the
closure round, the env rung it hooks is never reached after a failed read, so its restore is optional)."""

from __future__ import annotations

import builtins
import os

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def test_a43_same_signature_security_setting_edit_is_seen(tmp_path):
    import hermes_cli.config as config

    profile = tmp_path / "profile"
    profile.mkdir()
    token = set_hermes_home_override(profile)
    try:
        path = config.get_config_path()
        before = b"approvals:\n  deny: []  \n"
        after = b"approvals:\n  deny: [rm]\n"
        assert len(before) == len(after)
        path.write_bytes(before)
        assert config.load_config_readonly()["approvals"]["deny"] == []
        original = path.stat()
        signature = config.file_signature(original)

        with path.open("r+b") as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert path.read_bytes() == after
        if config.file_signature(path.stat()) != signature:
            pytest.skip("this filesystem changes the cache signature on in-place edits")

        assert config.load_config_readonly()["approvals"]["deny"] == ["rm"]
    finally:
        reset_hermes_home_override(token)


def test_a44_interrupted_config_read_cannot_select_env_provider(tmp_path, monkeypatch):
    import hermes_cli.auth as auth
    import hermes_cli.config as config

    profile = tmp_path / "profile"
    profile.mkdir()
    token = set_hermes_home_override(profile)
    try:
        path = config.get_config_path()
        configured = "model:\n  provider: gemini\n"
        path.write_text(configured, encoding="utf-8")
        actual_open = builtins.open
        interrupted = False
        restored = False

        def interrupted_open(file, *args, **kwargs):
            nonlocal interrupted
            if file == path and not interrupted:
                interrupted = True
                path.unlink()
                raise FileNotFoundError(2, "interrupted replacement", str(path))
            return actual_open(file, *args, **kwargs)

        def env_key_seen(_reader):
            nonlocal restored
            assert interrupted, "the config read must fail before env adoption"
            path.write_text(configured, encoding="utf-8")
            restored = True
            return True

        monkeypatch.setattr(config, "open", interrupted_open, raising=False)
        monkeypatch.setattr(auth, "_openrouter_auto_detected", env_key_seen)
        # Adoption note (config-auth): refusing with corrupt_config also satisfies this probe's intent
        # (no env provider selected); the verbatim probe expected a returned value only.
        try:
            result = auth.resolve_provider("auto")
        except auth.AuthError as exc:
            result = f"refused:{exc.code}"
        # Adoption note 2 (config-closure): the refusal now happens before any env rung runs, so the
        # probe's restore inside the env sniff may never execute.
        assert interrupted
        assert result != "openrouter", "an interrupted config read must not select an env provider"
    finally:
        reset_hermes_home_override(token)


def test_a44_non_openrouter_env_provider_also_refuses_failed_config(tmp_path, monkeypatch):
    import hermes_cli.auth as auth
    import hermes_cli.config as config

    profile = tmp_path / "profile"
    profile.mkdir()
    token = set_hermes_home_override(profile)
    try:
        path = config.get_config_path()
        path.write_text("model:\n  provider: anthropic\n", encoding="utf-8")
        actual_open = builtins.open
        interrupted = False

        def interrupted_open(file, *args, **kwargs):
            nonlocal interrupted
            if file == path and not interrupted:
                interrupted = True
                path.unlink()
                raise FileNotFoundError(2, "interrupted replacement", str(path))
            return actual_open(file, *args, **kwargs)

        def gemini_env_key(_reader, _oauth):
            assert interrupted
            assert config.get_active_config_parse_failure(), "failed read must remain recorded"
            return "gemini"

        monkeypatch.setattr(config, "open", interrupted_open, raising=False)
        monkeypatch.setattr(auth, "_openrouter_auto_detected", lambda _reader: False)
        monkeypatch.setattr(auth, "_logged_in_oauth_active_provider", lambda **_kw: None)
        monkeypatch.setattr(auth, "_env_key_auto_detected", gemini_env_key)
        with pytest.raises(auth.AuthError):
            auth.resolve_provider("auto")
    finally:
        reset_hermes_home_override(token)


def test_failed_config_read_reports_cause_before_unknown_custom_provider(tmp_path, monkeypatch):
    import hermes_cli.config as config
    from hermes_cli.model_switch import switch_model

    profile = tmp_path / "profile"
    profile.mkdir()
    token = set_hermes_home_override(profile)
    try:
        path = config.get_config_path()
        path.write_text(
            "providers:\n  ollama:\n    base_url: http://127.0.0.1:11434/v1\n",
            encoding="utf-8",
        )

        def failed_open(file, *args, **kwargs):
            if file == path:
                raise PermissionError(13, "interrupted config read", str(path))
            return builtins.open(file, *args, **kwargs)

        monkeypatch.setattr(config, "open", failed_open, raising=False)
        user_providers = config.read_raw_config().get("providers")
        result = switch_model(
            "local-model", current_provider="anthropic", current_model="old-model",
            explicit_provider="ollama", user_providers=user_providers,
        )
        assert not result.success
        assert "Unknown provider" not in result.error_message, result.error_message
        assert "config.yaml" in result.error_message, result.error_message
    finally:
        reset_hermes_home_override(token)
