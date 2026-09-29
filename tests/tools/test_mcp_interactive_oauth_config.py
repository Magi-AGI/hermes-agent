"""H-B: ``mcp.interactive_oauth: false`` guarantees no unattended browser launch for MCP OAuth.

Without the key, whether an MCP OAuth authorization-code flow may open a browser is decided
only by context: background paths wrap themselves in ``suppress_interactive_oauth()``, and
everything else falls back to "is stdin a real console". Any connect that runs on a console
(CLI startup discovery, the CLI config-watcher auto-reload thread, a lazy first-use connect
inside a model's tool call, a mid-session re-auth on a transport the CLI opened, cron run from
a terminal) could therefore open a browser tab nobody asked for.

Contract under test:
- key false: no browser, no loopback listener, no provider for a tokenless server — the
  existing actionable ``hermes mcp login <server>`` error instead;
- an explicit, user-started login (``force_interactive_oauth``: ``hermes mcp login``, the
  desktop/dashboard login button) still works — forced beats the config key;
- context suppression still beats forced (unchanged upstream contract);
- key absent: behaviour identical to before.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _write_config(text: str) -> None:
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


@pytest.fixture()
def console(monkeypatch):
    """A real console on stdin: the case where upstream opens a browser without being asked."""
    import tools.mcp_oauth as mod

    monkeypatch.setattr(mod, "_stdin_is_console", lambda: True)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(mod, "_can_open_browser", lambda: True)
    browser = MagicMock(return_value=True)
    monkeypatch.setattr("webbrowser.open", browser)
    return browser


# --- key off: nothing unattended opens a browser -------------------------------------------

def test_key_off_redirect_handler_opens_no_browser(console):
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: false\n")
    with pytest.raises(mod.OAuthNonInteractiveError, match="hermes mcp login"):
        asyncio.run(mod._make_redirect_handler(49311)("https://idp.example.invalid/authorize?state=s1"))
    console.assert_not_called()


def test_key_off_callback_waiter_binds_no_listener(console, monkeypatch):
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: false\n")
    monkeypatch.setattr(mod, "HTTPServer", MagicMock(side_effect=AssertionError("listener bound")))
    with pytest.raises(mod.OAuthNonInteractiveError, match="hermes mcp login"):
        asyncio.run(mod._make_callback_waiter(49312, timeout=300.0)())


def test_key_off_tokenless_connect_fails_with_login_hint(console, monkeypatch):
    """The provider guard every connect goes through (startup, lazy first use, reload, cron):
    a server with no cached tokens is refused before any flow starts."""
    pytest.importorskip("mcp.client.auth")
    from tools.mcp_oauth import OAuthNonInteractiveError, build_oauth_auth

    _write_config("mcp:\n  interactive_oauth: false\n")
    with pytest.raises(OAuthNonInteractiveError, match="hermes mcp login"):
        build_oauth_auth("fake-oauth-srv", "https://mcp.example.invalid/mcp")
    console.assert_not_called()


@pytest.mark.parametrize("value", ["false", "'off'", "'no'", "0", "'flase'"])
def test_key_off_spellings_and_typos_fail_closed(console, value):
    """Recognised false spellings disable; an unrecognised value fails CLOSED — the key exists
    to guarantee no browser, so a typo must not silently re-enable it."""
    import tools.mcp_oauth as mod

    _write_config(f"mcp:\n  interactive_oauth: {value}\n")
    assert mod._is_interactive() is False


# --- a config that cannot be read fails closed ---------------------------------------------

def test_config_loader_raising_fails_closed(console, monkeypatch):
    """The loader itself raises: nothing says the user did not write ``interactive_oauth: false``."""
    import hermes_cli.config as config_mod
    import tools.mcp_oauth as mod

    def boom():
        raise OSError("config.yaml: permission denied")

    monkeypatch.setattr(config_mod, "load_config_readonly", boom)
    assert mod._config_allows_unattended_oauth() is False
    assert mod._is_interactive() is False
    with pytest.raises(mod.OAuthNonInteractiveError, match="hermes mcp login"):
        asyncio.run(mod._make_redirect_handler(49316)("https://idp.example.invalid/authorize?state=s6"))
    console.assert_not_called()


def test_config_loader_silent_defaults_fallback_fails_closed(console):
    """A fresh process with a broken config.yaml and no good backup: the loader returns
    DEFAULT_CONFIG (``interactive_oauth: true``) without raising. The ``false`` in the broken file
    must still win, so the silent fallback is detected and treated as a read failure."""
    import hermes_cli.config as config_mod
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: false\n  broken: [unclosed\n")
    loaded = config_mod.load_config_readonly()
    assert loaded["mcp"]["interactive_oauth"] is True, "precondition: the loader fell back silently"
    assert config_mod.get_active_config_parse_failure(), "precondition: the parse failure is recorded"
    assert mod._config_allows_unattended_oauth() is False
    assert mod._is_interactive() is False


def test_config_loader_last_known_good_fallback_fails_closed(console):
    """The in-process fallback serves the previous good config while the file is broken: still a
    failed read of what is on disk now, so closed as well."""
    import hermes_cli.config as config_mod
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: true\n")
    assert mod._config_allows_unattended_oauth() is True
    _write_config("mcp:\n  interactive_oauth: false\n  broken: [unclosed\n")
    assert config_mod.load_config_readonly()["mcp"]["interactive_oauth"] is True
    assert mod._config_allows_unattended_oauth() is False


def test_config_repaired_after_failure_restores_default(console):
    """Fixing the file ends the fail-closed state: key absent + clean read is upstream behaviour."""
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  broken: [unclosed\n")
    assert mod._config_allows_unattended_oauth() is False
    _write_config("mcp:\n  discovery_concurrency: 4\n")
    assert mod._config_allows_unattended_oauth() is True
    assert mod._is_interactive() is True


# --- an interrupted read (stat succeeds, then open/read fails) fails closed ------------------

def _config_path():
    from hermes_cli.config import get_config_path

    return Path(get_config_path())


def _inject_open_fault(monkeypatch, *, vanish: bool, fail_at: str):
    """Make the loader's ``open(config.yaml)`` fail after its stat succeeded.

    ``vanish`` deletes the file first, so the loader's own follow-up stat fails as well (the
    stat -> open race: an editor's delete-then-write, a sync tool). ``fail_at`` is ``"open"``
    (open raises) or ``"read"`` (open succeeds, the read breaks partway)."""
    import builtins
    import hermes_cli.config as config_mod

    target = _config_path()
    real_open = builtins.open

    def faulty_open(file, *args, **kwargs):
        if Path(file) != target:
            return real_open(file, *args, **kwargs)
        if fail_at == "open":
            if vanish:
                target.unlink()
                raise FileNotFoundError(2, "No such file or directory", str(target))
            raise PermissionError(13, "Permission denied", str(target))

        handle = real_open(file, *args, **kwargs)

        class _Broken:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                handle.close()
                return False

            def read(self, *a, **k):
                handle.read(8)
                if vanish:
                    handle.close()
                    target.unlink()
                raise OSError(5, "Input/output error", str(target))

            def __getattr__(self, name):
                return getattr(handle, name)

        return _Broken()

    monkeypatch.setattr(config_mod, "open", faulty_open, raising=False)


@pytest.mark.parametrize("fail_at", ["open", "read"])
@pytest.mark.parametrize("vanish", [True, False], ids=["vanished", "present"])
def test_interrupted_read_fresh_process_fails_closed(console, monkeypatch, fail_at, vanish):
    """No earlier good config in this process: the loader serves DEFAULT_CONFIG
    (``interactive_oauth: true``) for a file that said ``false``."""
    import hermes_cli.config as config_mod
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: false\n")
    _inject_open_fault(monkeypatch, vanish=vanish, fail_at=fail_at)
    assert config_mod.load_config_readonly()["mcp"]["interactive_oauth"] is True, \
        "precondition: the interrupted read served a true value"
    assert mod._config_allows_unattended_oauth() is False
    assert mod._is_interactive() is False


@pytest.mark.parametrize("fail_at", ["open", "read"])
def test_interrupted_read_after_earlier_true_fails_closed(console, monkeypatch, fail_at):
    """An earlier ``true`` in this process: the loader serves it as last-known-good for the file
    that now says ``false`` and vanished mid-read."""
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: true\n")
    assert mod._config_allows_unattended_oauth() is True
    _write_config("mcp:\n  interactive_oauth: false\n  discovery_concurrency: 3\n")
    _inject_open_fault(monkeypatch, vanish=True, fail_at=fail_at)
    assert mod._config_allows_unattended_oauth() is False


def test_interrupted_read_stays_closed_until_the_file_is_back(console, monkeypatch):
    """After the race the file is simply missing; that is still the failed read, not a config
    the user never had. Writing the file again ends it."""
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: false\n")
    _inject_open_fault(monkeypatch, vanish=True, fail_at="open")
    assert mod._config_allows_unattended_oauth() is False
    import builtins
    import hermes_cli.config as config_mod

    # Only the fault goes away (monkeypatch.undo would also revert the sandboxed HERMES_HOME).
    monkeypatch.setattr(config_mod, "open", builtins.open, raising=False)
    assert not _config_path().exists()
    assert mod._config_allows_unattended_oauth() is False
    _write_config("mcp:\n  discovery_concurrency: 4\n")
    assert mod._config_allows_unattended_oauth() is True


# --- unchanged: empty config, absent key, a config file that does not exist ------------------

def test_config_file_never_existed_keeps_default(console):
    """No config.yaml at all (a fresh install): nothing was configured, so today's default."""
    import tools.mcp_oauth as mod

    path = _config_path()
    if path.exists():
        path.unlink()
    assert mod._config_allows_unattended_oauth() is True
    assert mod._is_interactive() is True


def test_config_deleted_between_loads_keeps_default(console):
    """A config the user deleted (seen as absent at stat, no read interrupted): the same as no
    file. Only a read that the deletion interrupted fails closed."""
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: false\n")
    assert mod._config_allows_unattended_oauth() is False
    _config_path().unlink()
    assert mod._config_allows_unattended_oauth() is True


def test_intentionally_empty_config_keeps_default(console):
    import tools.mcp_oauth as mod

    _write_config("")
    assert mod._config_allows_unattended_oauth() is True


# --- explicit user-started login still works -----------------------------------------------

def test_key_off_explicit_login_still_opens_browser(console):
    """``hermes mcp login`` runs under force_interactive_oauth: forced beats the config key."""
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: false\n")

    async def _go():
        with mod.force_interactive_oauth():
            await mod._make_redirect_handler(49313)("https://idp.example.invalid/authorize?state=s2")

    asyncio.run(_go())
    console.assert_called_once_with("https://idp.example.invalid/authorize?state=s2")


def test_key_off_desktop_login_still_publishes_to_dashboard(console):
    """The desktop/dashboard login button: force + dashboard flow publishes the URL for the UI."""
    import tools.mcp_oauth as mod
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow, dashboard_oauth_flow

    _write_config("mcp:\n  interactive_oauth: false\n")
    flow = DashboardOAuthFlow(flow_id="f", server_name="srv", profile=None, hermes_home=".",
                              redirect_uri="https://example.invalid/cb")

    async def _go():
        with mod.force_interactive_oauth(), dashboard_oauth_flow(flow):
            await mod._make_redirect_handler(0)("https://idp.example.invalid/authorize?state=s3")

    asyncio.run(_go())
    assert flow.authorization_url == "https://idp.example.invalid/authorize?state=s3"
    console.assert_not_called()


def test_suppression_still_beats_forced_with_key_off(console):
    """Unchanged upstream contract: background suppression wins over everything."""
    import tools.mcp_oauth as mod

    _write_config("mcp:\n  interactive_oauth: false\n")

    async def _go():
        with mod.force_interactive_oauth(), mod.suppress_interactive_oauth():
            await mod._make_redirect_handler(49314)("https://idp.example.invalid/authorize?state=s4")

    with pytest.raises(mod.OAuthNonInteractiveError):
        asyncio.run(_go())
    console.assert_not_called()


# --- default: nothing changes --------------------------------------------------------------

@pytest.mark.parametrize("config_text", ["", "mcp:\n  discovery_concurrency: 4\n",
                                         "mcp:\n  interactive_oauth: true\n"])
def test_default_console_behaviour_unchanged(console, config_text):
    """Key absent (or true): a console context is interactive and the browser opens, as before."""
    import tools.mcp_oauth as mod

    _write_config(config_text)
    assert mod._is_interactive() is True
    asyncio.run(mod._make_redirect_handler(49315)("https://idp.example.invalid/authorize?state=s5"))
    console.assert_called_once()


def test_default_non_console_still_non_interactive(monkeypatch):
    import tools.mcp_oauth as mod

    _write_config("")
    monkeypatch.setattr(mod, "_stdin_is_console", lambda: False)
    assert mod._is_interactive() is False


def test_config_default_and_example_document_the_key():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["mcp"]["interactive_oauth"] is True
    example = (Path(__file__).resolve().parents[2] / "cli-config.yaml.example").read_text(encoding="utf-8")
    assert "interactive_oauth:" in example
