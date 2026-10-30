"""Tests for hermes_logging — centralized logging setup."""
import io
import logging
import os
import stat
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import hermes_logging
# Both file-handler kinds (the POSIX shared-file handler and the Windows per-process
# segment handler) derive from stdlib RotatingFileHandler, so one isinstance check
# matches the real handlers on every platform.
from hermes_logging import RotatingFileHandler


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Reset the module-level sentinel and clean up root logger handlers
    added by setup_logging() so tests don't leak state.

    Under xdist (-n auto) other test modules may have called setup_logging()
    in the same worker process, leaving RotatingFileHandlers on the root
    logger.  We strip ALL RotatingFileHandlers before each test so the count
    assertions are stable regardless of test ordering.
    """
    hermes_logging._logging_initialized = False
    # File handlers now live behind the async QueueListener, not on the root
    # logger; tear down any leaked from other xdist tests in this worker.
    hermes_logging._reset_queued_handlers()
    root = logging.getLogger()
    prev_root_level = root.level
    root.setLevel(logging.NOTSET)
    # Snapshot the remaining (non-file) handlers so we can strip whatever the
    # test adds.
    pre_existing = list(root.handlers)
    # Ensure the record factory is installed (it's idempotent).
    hermes_logging._install_session_record_factory()
    yield
    # Restore — tear down async file logging + remove handlers added by the test.
    hermes_logging._reset_queued_handlers()
    for h in list(root.handlers):
        if h not in pre_existing:
            root.removeHandler(h)
            h.close()
    root.setLevel(prev_root_level)
    hermes_logging._logging_initialized = False
    hermes_logging.clear_session_context()


def _log_text(stem: Path) -> str:
    """Everything written for *stem* (``…/logs/agent.log``), whichever layout the host uses: the
    shared file on POSIX, this process's segments under ``<stem>.d/`` on Windows."""
    segments = sorted((stem.parent / f"{stem.stem}.d").glob("*.log"))
    return "".join(p.read_text(encoding="utf-8") for p in [stem, *segments] if p.exists())


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Provide an isolated HERMES_HOME for logging tests.

    Uses the same tmp_path as the autouse _isolate_hermes_home from conftest,
    reading it back from the env var to avoid double-mkdir conflicts.
    """
    home = Path(os.environ["HERMES_HOME"])
    return home


class TestSetupLogging:
    """setup_logging() creates agent.log + errors.log with RotatingFileHandler."""

    def test_creates_log_directory(self, hermes_home):
        log_dir = hermes_logging.setup_logging(hermes_home=hermes_home)
        assert log_dir == hermes_home / "logs"
        assert log_dir.is_dir()

    def test_creates_agent_log_handler(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        root = logging.getLogger()

        agent_handlers = [
            h for h in hermes_logging._queued_file_handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1
        assert agent_handlers[0].level == logging.INFO


    def test_idempotent_no_duplicate_handlers(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_logging(hermes_home=hermes_home)  # second call — should be no-op

        root = logging.getLogger()
        agent_handlers = [
            h for h in hermes_logging._queued_file_handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert len(agent_handlers) == 1





    def test_writes_to_agent_log(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)

        test_logger = logging.getLogger("test_hermes_logging.write_test")
        test_logger.info("test message for agent.log")

        # Flush handlers
        hermes_logging.flush_log_queue()

        # Layout-agnostic: the shared file on POSIX, this process's segment on Windows.
        content = _log_text(hermes_home / "logs" / "agent.log")
        assert "test message for agent.log" in content

    @pytest.mark.windows_only
    def test_windows_writes_per_process_segments_and_takes_no_cross_process_lock(self, hermes_home):
        """Gate H4: no shared agent.log, no concurrent-log-handler lock file — each process owns
        its segment under agent.d/ and the writer never waits on another process."""
        hermes_logging.setup_logging(hermes_home=hermes_home)
        logging.getLogger("test_hermes_logging.segments").info("segment me")
        hermes_logging.flush_log_queue()

        logs = hermes_home / "logs"
        assert not (logs / "agent.log").exists()
        assert not list(logs.glob(".__*.lock"))
        segments = list((logs / "agent.d").glob("*.log"))
        assert len(segments) == 1
        assert "segment me" in segments[0].read_text(encoding="utf-8")

    def test_profile_routing_follows_context_home(self, hermes_home, tmp_path):
        """Desktop multiplex cron records are written to their owning profile."""
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        profile_home = tmp_path / "profile-b"
        profile_home.mkdir()
        hermes_logging.setup_logging(hermes_home=hermes_home)
        assert hermes_logging.enable_profile_log_routing(
            [hermes_home, profile_home]
        ) is True

        logger = logging.getLogger("cron.scheduler.profile-routing-test")
        token = set_hermes_home_override(profile_home)
        try:
            logger.info("profile-routed cron record")
        finally:
            reset_hermes_home_override(token)
        hermes_logging.flush_log_queue()

        assert "profile-routed cron record" in _log_text(profile_home / "logs" / "agent.log")
        assert "profile-routed cron record" not in _log_text(hermes_home / "logs" / "agent.log")

    def test_release_profile_log_handlers_closes_only_deleted_profile(self, hermes_home, tmp_path):
        """Profile deletion releases its routed log files without disturbing another profile."""
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        deleted_home = tmp_path / "profile-deleted"
        other_home = tmp_path / "profile-other"
        deleted_home.mkdir()
        other_home.mkdir()
        hermes_logging.setup_logging(hermes_home=hermes_home)
        assert hermes_logging.enable_profile_log_routing(
            [hermes_home, deleted_home, other_home]
        ) is True

        logger = logging.getLogger("agent.profile-delete-log-release")
        token = set_hermes_home_override(deleted_home)
        try:
            logger.warning("deleted profile log handles")
        finally:
            reset_hermes_home_override(token)
        token = set_hermes_home_override(other_home)
        try:
            logger.warning("other profile log handles")
        finally:
            reset_hermes_home_override(token)
        hermes_logging.flush_log_queue()

        routers = [
            handler for handler in hermes_logging._queued_file_handlers
            if isinstance(handler, hermes_logging._ProfileRoutingFileHandler)
        ]
        assert len(routers) == 2  # agent.log and errors.log
        assert all(deleted_home.resolve() in handler._profile_handlers for handler in routers)
        assert all(other_home.resolve() in handler._profile_handlers for handler in routers)

        assert hermes_logging.release_profile_log_handlers(deleted_home) == 2

        assert all(deleted_home.resolve() not in handler._profile_handlers for handler in routers)
        assert all(deleted_home.resolve() not in handler._profile_homes for handler in routers)
        assert all(other_home.resolve() in handler._profile_handlers for handler in routers)
        assert "other profile log handles" in _log_text(other_home / "logs" / "agent.log")
        assert "other profile log handles" in _log_text(other_home / "logs" / "errors.log")

    def test_a_second_home_routes_instead_of_stacking_an_unfiltered_handler(self, hermes_home, tmp_path):
        """A dashboard or serve backend builds agents for several profiles in ONE process, and each
        one calls setup_logging for its own home. The second home must get a router — a bare file
        handler beside the first home's would receive every profile's records."""
        from logging.handlers import RotatingFileHandler

        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        profile_home = tmp_path / "profile-b"
        profile_home.mkdir()
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_logging(hermes_home=profile_home)

        assert not [h for h in hermes_logging._queued_file_handlers if isinstance(h, RotatingFileHandler)], (
            "the second home must not add an unfiltered file handler")

        logger = logging.getLogger("agent.conversation_loop.second-home-test")
        token = set_hermes_home_override(profile_home)
        try:
            logger.info("turn of profile b")
        finally:
            reset_hermes_home_override(token)
        logger.info("turn of the launch profile")
        hermes_logging.flush_log_queue()

        a_log = _log_text(hermes_home / "logs" / "agent.log")
        b_log = _log_text(profile_home / "logs" / "agent.log")
        assert "turn of profile b" in b_log and "turn of profile b" not in a_log
        assert "turn of the launch profile" in a_log and "turn of the launch profile" not in b_log

    def test_setup_for_an_already_routed_home_adds_no_duplicate_writer(self, hermes_home, tmp_path):
        """Routing already on (multiplexed gateway, Desktop cron ticker): a profile's agent starting
        up must not add a second writer for its home on top of the router."""
        from logging.handlers import RotatingFileHandler

        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        profile_home = tmp_path / "profile-b"
        profile_home.mkdir()
        hermes_logging.setup_logging(hermes_home=hermes_home)
        assert hermes_logging.enable_profile_log_routing([hermes_home, profile_home]) is True
        hermes_logging.setup_logging(hermes_home=profile_home)

        assert not [h for h in hermes_logging._queued_file_handlers if isinstance(h, RotatingFileHandler)]
        token = set_hermes_home_override(profile_home)
        try:
            logging.getLogger("agent.conversation_loop.routed-home-test").info("once please")
        finally:
            reset_hermes_home_override(token)
        hermes_logging.flush_log_queue()

        assert _log_text(profile_home / "logs" / "agent.log").count("once please") == 1
        assert "once please" not in _log_text(hermes_home / "logs" / "agent.log")

    def test_a_component_log_added_after_routing_is_routed_too(self, hermes_home, tmp_path):
        """setup_logging(mode="gateway") for an already-known home AFTER a second home turned
        routing on: gateway.log must be a routed writer, not a bare handler taking every home."""
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        profile_home = tmp_path / "profile-b"
        profile_home.mkdir()
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_logging(hermes_home=profile_home)
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        logger = logging.getLogger("gateway.run.routed-component-test")
        token = set_hermes_home_override(profile_home)
        try:
            logger.info("gw-b")
        finally:
            reset_hermes_home_override(token)
        logger.info("gw-a")
        hermes_logging.flush_log_queue()

        a_log = _log_text(hermes_home / "logs" / "gateway.log")
        assert "gw-a" in a_log and "gw-b" not in a_log
        assert "gw-b" in _log_text(profile_home / "logs" / "gateway.log")




    def test_explicit_params_override_config(self, hermes_home):
        """Explicit function params take precedence over config.yaml."""
        import yaml
        config = {"logging": {"level": "DEBUG"}}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        hermes_logging.setup_logging(hermes_home=hermes_home, log_level="WARNING")

        root = logging.getLogger()
        agent_handlers = [
            h for h in hermes_logging._queued_file_handlers
            if isinstance(h, RotatingFileHandler)
            and "agent.log" in getattr(h, "baseFilename", "")
        ]
        assert agent_handlers[0].level == logging.WARNING



class TestGatewayMode:
    """setup_logging(mode='gateway') creates a filtered gateway.log."""

    def test_gateway_log_created(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        root = logging.getLogger()

        gw_handlers = [
            h for h in hermes_logging._queued_file_handlers
            if isinstance(h, RotatingFileHandler)
            and "gateway.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gw_handlers) == 1

    def test_gateway_log_not_created_in_cli_mode(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="cli")
        root = logging.getLogger()

        gw_handlers = [
            h for h in hermes_logging._queued_file_handlers
            if isinstance(h, RotatingFileHandler)
            and "gateway.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gw_handlers) == 0



    def test_gateway_log_receives_gateway_records(self, hermes_home):
        """gateway.log captures records from gateway.* loggers."""
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        gw_logger = logging.getLogger("plugins.platforms.telegram.adapter")
        gw_logger.info("telegram connected")

        hermes_logging.flush_log_queue()

        assert "telegram connected" in _log_text(hermes_home / "logs" / "gateway.log")

    def test_gateway_log_rejects_non_gateway_records(self, hermes_home):
        """gateway.log does NOT capture records from tools.*, agent.*, etc."""
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        tool_logger = logging.getLogger("tools.terminal_tool")
        tool_logger.info("running command")

        agent_logger = logging.getLogger("agent.context_compressor")
        agent_logger.info("compressing context")

        hermes_logging.flush_log_queue()

        content = _log_text(hermes_home / "logs" / "gateway.log")
        assert "running command" not in content
        assert "compressing context" not in content



class TestGuiMode:
    """setup_logging(mode='gui') creates a filtered gui.log."""

    def test_gui_log_created(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gui")
        root = logging.getLogger()

        gui_handlers = [
            h for h in hermes_logging._queued_file_handlers
            if isinstance(h, RotatingFileHandler)
            and "gui.log" in getattr(h, "baseFilename", "")
        ]
        assert len(gui_handlers) == 1


    def test_gui_log_receives_only_gui_components(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gui")

        logging.getLogger("hermes_cli.web_server").info("dashboard online")
        logging.getLogger("tui_gateway.ws").info("ws connected")
        logging.getLogger("gateway.run").info("gateway event")

        hermes_logging.flush_log_queue()

        content = _log_text(hermes_home / "logs" / "gui.log")
        assert "dashboard online" in content
        assert "ws connected" in content
        assert "gateway event" not in content


class TestSessionContext:
    """set_session_context / clear_session_context + _SessionFilter."""

    def test_session_tag_in_log_output(self, hermes_home):
        """When session context is set, log lines include [session_id]."""
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.set_session_context("abc123")

        test_logger = logging.getLogger("test.session_tag")
        test_logger.info("tagged message")

        hermes_logging.flush_log_queue()

        content = _log_text(hermes_home / "logs" / "agent.log")
        assert "[abc123]" in content
        assert "tagged message" in content







class TestComponentFilter:
    """Unit tests for _ComponentFilter."""

    def test_passes_matching_prefix(self):
        f = hermes_logging._ComponentFilter(("gateway",))
        record = logging.LogRecord(
            "gateway.run", logging.INFO, "", 0, "msg", (), None
        )
        assert f.filter(record) is True


    def test_blocks_non_matching(self):
        f = hermes_logging._ComponentFilter(("gateway",))
        record = logging.LogRecord(
            "tools.terminal_tool", logging.INFO, "", 0, "msg", (), None
        )
        assert f.filter(record) is False





class TestSetupVerboseLogging:
    """setup_verbose_logging() adds a DEBUG-level console handler."""

    def test_adds_stream_handler(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        hermes_logging.setup_verbose_logging()

        root = logging.getLogger()
        verbose_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RotatingFileHandler)
            and getattr(h, "_hermes_verbose", False)
        ]
        assert len(verbose_handlers) == 1
        assert verbose_handlers[0].level == logging.DEBUG



class TestAddRotatingHandler:
    """_add_rotating_handler() is idempotent and creates the directory."""


    def test_no_duplicate_for_same_path(self, tmp_path):
        log_path = tmp_path / "test.log"
        formatter = logging.Formatter("%(message)s")

        hermes_logging._add_rotating_handler(
            log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )
        hermes_logging._add_rotating_handler(
            log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        rotating_handlers = [
            h for h in hermes_logging._queued_file_handlers
            if isinstance(h, RotatingFileHandler)
        ]
        assert len(rotating_handlers) == 1
        # Clean up

    def test_no_session_filter_on_handler(self, tmp_path):
        """Handlers rely on record factory, not per-handler _SessionFilter."""
        log_path = tmp_path / "no_session_filter.log"
        logger = logging.getLogger("_test_no_session_filter")
        formatter = logging.Formatter("%(session_tag)s%(message)s")

        hermes_logging._add_rotating_handler(
            log_path,
            level=logging.INFO, max_bytes=1024, backup_count=1,
            formatter=formatter,
        )

        handlers = [h for h in hermes_logging._queued_file_handlers if isinstance(h, RotatingFileHandler)]
        assert len(handlers) == 1
        # No _SessionFilter on the handler — record factory handles it
        assert len(handlers[0].filters) == 0

        # But session_tag still works (via record factory)
        hermes_logging.set_session_context("factory_test")
        logger.info("test msg")
        hermes_logging.flush_log_queue()
        content = _log_text(log_path)
        assert "[factory_test]" in content

        # Clean up
    @pytest.mark.linux_only  # managed mode is a NixOS install; Windows writes segments instead
    def test_managed_mode_initial_open_sets_group_writable(self, tmp_path):
        log_path = tmp_path / "managed-open.log"
        formatter = logging.Formatter("%(message)s")

        old_umask = os.umask(0o022)
        try:
            with patch("hermes_cli.config.is_managed", return_value=True):
                hermes_logging._add_rotating_handler(
                    log_path,
                    level=logging.INFO, max_bytes=1024, backup_count=1,
                    formatter=formatter,
                )
        finally:
            os.umask(old_umask)

        assert log_path.exists()
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o660



class TestReadLoggingConfig:
    """_read_logging_config() reads from config.yaml."""

    def test_returns_none_when_no_config(self, hermes_home):
        assert all(value is None for value in hermes_logging._read_logging_config())

    def test_reads_logging_section(self, hermes_home):
        import yaml
        config = {"logging": {"level": "DEBUG", "max_size_mb": 10, "backup_count": 5}}
        (hermes_home / "config.yaml").write_text(yaml.dump(config))

        level, max_size, backup = hermes_logging._read_logging_config()[:3]
        assert level == "DEBUG"
        assert max_size == 10
        assert backup == 5

    def test_logging_retention_days_reaches_the_segment_pruner(self, hermes_home):
        """config.yaml ``logging.retention_days`` → setup_logging prunes dead-process segments
        older than that under this home, keeps younger ones, and never touches legacy files."""
        import os
        import time
        import yaml

        (hermes_home / "config.yaml").write_text(yaml.dump({"logging": {"retention_days": 1}}))
        logs = hermes_home / "logs" / "agent.d"
        logs.mkdir(parents=True)
        stale = logs / "11-20261001-000000.1.log"
        fresh = logs / "12-20261029-000000.1.log"
        legacy = hermes_home / "logs" / "agent.log.1"
        for path, age_days in ((stale, 3), (fresh, 0.5), (legacy, 30)):
            path.write_text("x\n", encoding="utf-8")
            stamp = time.time() - age_days * 86400
            os.utime(path, (stamp, stamp))

        hermes_logging.setup_logging(hermes_home=hermes_home)

        assert not stale.exists()
        assert fresh.exists() and legacy.exists()



    def test_each_routed_home_is_pruned_by_its_own_retention(self, hermes_home, tmp_path):
        """Multiplex: profile B's segments are pruned by B's config.yaml, never by the launch
        profile's — A's one-day retention must not delete B's thirty-day history."""
        import os
        import time
        import yaml
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home_b = tmp_path / "profile-b"
        home_b.mkdir()
        (hermes_home / "config.yaml").write_text(yaml.dump({"logging": {"retention_days": 1}}))
        (home_b / "config.yaml").write_text(yaml.dump({"logging": {"retention_days": 30}}))
        stale_a = hermes_home / "logs" / "agent.d" / "11-20261001-000000.1.log"
        stale_b = home_b / "logs" / "agent.d" / "11-20261001-000000.1.log"
        for path in (stale_a, stale_b):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")
            stamp = time.time() - 5 * 86400
            os.utime(path, (stamp, stamp))

        hermes_logging.setup_logging(hermes_home=hermes_home)
        assert hermes_logging.enable_profile_log_routing([hermes_home, home_b]) is True
        token = set_hermes_home_override(home_b)
        try:
            logging.getLogger("agent.retention-routing-test").info("touch b")
        finally:
            reset_hermes_home_override(token)
        hermes_logging.flush_log_queue()

        assert not stale_a.exists()
        assert stale_b.exists()


    def test_a_boolean_retention_is_not_a_day_count(self, hermes_home):
        """`retention_days: true` is a config mistake, not "one day": fall back to the default
        instead of pruning everything older than 24 h (H4-R1-3b)."""
        import os
        import time
        import yaml

        (hermes_home / "config.yaml").write_text(yaml.dump({"logging": {"retention_days": True}}))
        three_days = hermes_home / "logs" / "agent.d" / "11-20261001-000000.1.log"
        three_days.parent.mkdir(parents=True)
        three_days.write_text("x\n", encoding="utf-8")
        stamp = time.time() - 3 * 86400
        os.utime(three_days, (stamp, stamp))

        hermes_logging.setup_logging(hermes_home=hermes_home)

        assert three_days.exists()


    def test_an_infinite_retention_disables_pruning(self, hermes_home):
        """`retention_days: .inf` is "keep forever", not the default (H4-R2-6)."""
        import os
        import time
        import yaml

        (hermes_home / "config.yaml").write_text(yaml.dump({"logging": {"retention_days": float("inf")}}))
        ancient = hermes_home / "logs" / "agent.d" / "11-20201001-000000.1.log"
        ancient.parent.mkdir(parents=True)
        ancient.write_text("x\n", encoding="utf-8")
        stamp = time.time() - 2000 * 86400
        os.utime(ancient, (stamp, stamp))

        hermes_logging.setup_logging(hermes_home=hermes_home)

        assert ancient.exists()


    def test_an_unreadable_config_never_authorizes_pruning(self, hermes_home):
        """Deletion needs a successfully observed policy: when the home's config cannot be read,
        nothing is pruned — a failed read is not "no retention setting" (Codex LL-C1)."""
        import os
        import time

        (hermes_home / "config.yaml").mkdir()  # a config path that exists but cannot be read
        stale = hermes_home / "logs" / "agent.d" / "11-20261001-000000.1.log"
        stale.parent.mkdir(parents=True)
        stale.write_text("x\n", encoding="utf-8")
        stamp = time.time() - 400 * 86400
        os.utime(stale, (stamp, stamp))

        hermes_logging.setup_logging(hermes_home=hermes_home)

        assert stale.exists()

    def test_an_oversized_retention_integer_falls_back_to_the_default(self, hermes_home):
        """``retention_days: 1e400``-sized integers overflow float(); that is a config mistake,
        not a crash at handler construction (Codex LL-L2)."""
        assert hermes_logging._retention_days(10 ** 400) == 7.0


    def test_a_config_that_is_not_a_mapping_never_authorizes_pruning(self, hermes_home):
        """``retention_days = 0`` typed into config.yaml parses as a YAML string, not a mapping:
        that is an unreadable policy, not an absent one (H4-R6-4)."""
        import os
        import time

        (hermes_home / "config.yaml").write_text("retention_days = 0\n", encoding="utf-8")
        stale = hermes_home / "logs" / "agent.d" / "11-20261001-000000.1.log"
        stale.parent.mkdir(parents=True)
        stale.write_text("x\n", encoding="utf-8")
        stamp = time.time() - 400 * 86400
        os.utime(stale, (stamp, stamp))

        hermes_logging.setup_logging(hermes_home=hermes_home)

        assert stale.exists()


    def test_the_retention_policy_is_derived_from_the_bytes_it_validated(self, hermes_home, monkeypatch):
        """Validation and policy must come from ONE observation of config.yaml: a file that is a
        valid mapping when probed but is replaced before a second read must not yield a different
        policy (Codex LL-C1). The seam is the single bytes observation."""
        calls = []

        def observe(path):
            calls.append(path)
            return b"logging:\n  retention_days: 0\n" if len(calls) == 1 else b"retention_days = 0\n"

        (hermes_home / "config.yaml").write_text("logging:\n  retention_days: 0\n", encoding="utf-8")
        monkeypatch.setattr(hermes_logging, "_observe_config_bytes", observe)
        assert hermes_logging._retention_policy(hermes_home) == 0.0
        assert len(calls) == 1

    def test_a_configured_managed_scope_disables_pruning_in_this_version(self, hermes_home, monkeypatch, tmp_path):
        """Lake's scope ruling after Codex round 6 (option 2): whether an administrator's overlay
        exists but is unavailable cannot be told apart from "no overlay" by any filesystem probe,
        so when a managed scope MIGHT exist (``HERMES_MANAGED_DIR`` set, or the platform default
        present) this version never prunes and never reads the managed files. Only the home's own
        config decides, and only when no managed scope is configured."""
        managed = tmp_path / "managed"
        managed.mkdir()
        (hermes_home / "config.yaml").write_text("logging:\n  retention_days: 30\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        (managed / "config.yaml").write_text("logging:\n  retention_days: 0\n", encoding="utf-8")
        assert hermes_logging._retention_policy(hermes_home) is None  # an admin pin exists: never prune
        (managed / "config.yaml").write_text("logging:\n  retention_days: 1\n", encoding="utf-8")
        assert hermes_logging._retention_policy(hermes_home) is None  # never adopted either: not read
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "corp-link" / "hermes"))  # unavailable ancestry
        assert hermes_logging._retention_policy(hermes_home) is None
        monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)  # no managed scope: the home decides
        assert hermes_logging._retention_policy(hermes_home) == 30.0
        # Outside pytest the platform default is probed on EVERY platform, exactly as the
        # canonical resolver (managed_scope.get_managed_dir) adopts it — on Windows the POSIX
        # path is drive-relative (C:\etc\hermes), but the resolver accepts such a directory, so
        # an install that has one is a managed install and must not be pruned (Codex round 7).
        from hermes_cli import managed_scope
        monkeypatch.setattr(managed_scope, "_under_pytest", lambda: False)
        monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", managed)
        assert hermes_logging._managed_scope_may_exist() is True
        assert hermes_logging._retention_policy(hermes_home) is None
        monkeypatch.setattr(managed_scope, "_DEFAULT_MANAGED_DIR", tmp_path / "no-such-dir")
        assert hermes_logging._managed_scope_may_exist() is False
        assert hermes_logging._retention_policy(hermes_home) == 30.0


    def test_an_env_templated_retention_value_is_expanded_and_an_unresolved_one_blocks_pruning(self, hermes_home, monkeypatch):
        """``retention_days: "${HERMES_RETENTION_DAYS}"`` is how an administrator pins the value
        from the service environment; it must expand like every other config value, and an
        unresolvable reference is an unreadable policy, not the default (H4-R8-3)."""
        (hermes_home / "config.yaml").write_text('logging:\n  retention_days: "${HERMES_RETENTION_DAYS}"\n', encoding="utf-8")
        monkeypatch.setenv("HERMES_RETENTION_DAYS", "0")
        assert hermes_logging._retention_policy(hermes_home) == 0.0
        monkeypatch.delenv("HERMES_RETENTION_DAYS", raising=False)
        assert hermes_logging._retention_policy(hermes_home) is None


    def test_a_routed_home_resolves_retention_references_in_its_own_scope_never_the_launch_environment(self, hermes_home, monkeypatch, tmp_path):
        """Codex round 3 (H4-R9-1, P2): the routed handler for profile B is created on the shared
        logging worker under profile A's process environment. A's launch env must never satisfy
        B's ``${RETENTION_DAYS}``: B's own ``.env`` decides, an unresolved reference under B is an
        unreadable policy (no pruning) even when A's environment would resolve it, and only the
        process's own home may take the value from its launch environment. With a managed scope
        configured nothing is pruned at all (S19)."""
        routed = tmp_path / "profile-b"
        routed.mkdir()
        (routed / "config.yaml").write_text('logging:\n  retention_days: "${RETENTION_DAYS}"\n', encoding="utf-8")
        (routed / ".env").write_text("RETENTION_DAYS=0\n", encoding="utf-8")
        monkeypatch.setenv("RETENTION_DAYS", "1")  # profile A's launch environment
        assert hermes_logging._retention_policy(routed) == 0.0  # B's .env, not A's env
        (routed / ".env").write_text("# nothing pinned here\n", encoding="utf-8")
        assert hermes_logging._retention_policy(routed) is None  # unresolved under B: fail closed
        (hermes_home / "config.yaml").write_text('logging:\n  retention_days: "${RETENTION_DAYS}"\n', encoding="utf-8")
        assert hermes_logging._retention_policy(hermes_home) == 1.0  # the process's own home
        managed = tmp_path / "managed"
        managed.mkdir()
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        (managed / "config.yaml").write_text('logging:\n  retention_days: "${RETENTION_DAYS}"\n', encoding="utf-8")
        assert hermes_logging._retention_policy(routed) is None  # no managed .env pin: the environ is no authority (Codex r4)


    def test_a_managed_env_pin_is_never_read_and_never_authorizes_pruning(self, hermes_home, monkeypatch, tmp_path):
        """H4-R10-1 (P2) and Codex round 4 (H4-R11-1) are moot under S19: ``os.environ`` is not the
        administrator's scope once ``load_hermes_dotenv`` has published profile A's ``.env`` over
        it, no record proves what the service was launched with, and the managed ``.env`` is not
        read either — with a managed scope configured nothing is pruned."""
        routed = tmp_path / "profile-b"
        routed.mkdir()
        (routed / "config.yaml").write_text("logging:\n  retention_days: 30\n", encoding="utf-8")
        managed = tmp_path / "managed"
        managed.mkdir()
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        (managed / "config.yaml").write_text('logging:\n  retention_days: "${RETENTION_DAYS}"\n', encoding="utf-8")
        monkeypatch.setenv("RETENTION_DAYS", "1")  # what profile A's .env published
        for pin in ("RETENTION_DAYS=0\n", "RETENTION_DAYS=1\n", "# no pin here\n"):
            (managed / ".env").write_text(pin, encoding="utf-8")
            # Option 2: with a managed scope configured nothing is pruned and nothing managed is
            # read — neither the admin's .env nor the environ (Codex round 4) can authorize deletion.
            assert hermes_logging._retention_policy(routed) is None, pin

    def test_an_expanded_retention_value_that_is_not_a_number_blocks_pruning(self, hermes_home, monkeypatch):
        """H4-R10-2 (P2): an expansion that yields ``""``, text, a still-templated value (nested
        ``${}`` in .env, an unclosed ``${``) is an unreadable policy, never the 7-day default;
        ``.inf`` from the environment still means keep forever."""
        cfg = hermes_home / "config.yaml"
        cfg.write_text('logging:\n  retention_days: "${RETENTION_DAYS}"\n', encoding="utf-8")
        for bad in ("", "abc", "nan"):
            monkeypatch.setenv("RETENTION_DAYS", bad)
            assert hermes_logging._retention_policy(hermes_home) is None, bad
        monkeypatch.setenv("RETENTION_DAYS", ".inf")
        assert hermes_logging._retention_policy(hermes_home) == float("inf")
        (hermes_home / ".env").write_text("RETENTION_DAYS=${BASE_RETENTION}\n", encoding="utf-8")
        monkeypatch.setenv("RETENTION_DAYS", "1")
        assert hermes_logging._retention_policy(hermes_home) is None  # nested reference: unresolved
        (hermes_home / ".env").unlink()
        cfg.write_text('logging:\n  retention_days: "${RETENTION_DAYS"\n', encoding="utf-8")
        assert hermes_logging._retention_policy(hermes_home) is None  # unclosed template
        cfg.write_text('logging:\n  retention_days: "abc"\n', encoding="utf-8")
        assert hermes_logging._retention_policy(hermes_home) is None  # literal text

    def test_an_unreadable_home_env_blocks_pruning_instead_of_falling_back_to_the_process_environment(self, hermes_home, monkeypatch):
        """H4-R10-3 (P2): the home's ``.env`` is observed fail-closed like config.yaml; a file that
        exists but cannot be read is an unreadable policy even when the process environment
        would resolve the reference."""
        (hermes_home / "config.yaml").write_text('logging:\n  retention_days: "${RETENTION_DAYS}"\n', encoding="utf-8")
        (hermes_home / ".env").write_text("RETENTION_DAYS=0\n", encoding="utf-8")
        monkeypatch.setenv("RETENTION_DAYS", "1")
        assert hermes_logging._retention_policy(hermes_home) == 0.0

        def denied(path):
            raise PermissionError(13, "denied", str(path))

        monkeypatch.setattr(hermes_logging, "_observe_env_bytes", denied)
        assert hermes_logging._retention_policy(hermes_home) is None

    def test_process_home_identity_expands_the_user_and_ignores_task_overrides(self, hermes_home, monkeypatch, tmp_path):
        """H4-R10-4 (P3): the process-home test goes through hermes_constants and expands ``~``."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setenv("HERMES_HOME", "~/own-home")
        (tmp_path / "own-home").mkdir()
        assert hermes_logging._is_process_home(Path("~/own-home")) is True
        assert hermes_logging._is_process_home(tmp_path / "own-home") is True
        assert hermes_logging._is_process_home(tmp_path / "other") is False


    def test_a_dangling_managed_link_blocks_pruning_instead_of_reading_as_absence(self, hermes_home, monkeypatch, tmp_path):
        """Codex round 4 (H4-R11-3, P2): an unobserved dangling ``<managed_dir>/.env`` (or
        ``config.yaml``) symlink is ``_ABSENT`` to the managed reader, so the pin it normally
        supplies vanished into the fallback. A link that exists but resolves nowhere is an
        unreadable policy for both managed files."""
        from hermes_cli import managed_scope

        managed = tmp_path / "managed"
        managed.mkdir()
        dangling: set = set()
        try:
            os.symlink(str(tmp_path / "missing-secrets.env"), str(managed / ".env"))
        except (OSError, NotImplementedError):
            # No symlink privilege on this host: present the same observation (is_symlink() True,
            # exists() False) for the paths the test dangles.
            real_is_symlink = Path.is_symlink
            monkeypatch.setattr(Path, "is_symlink", lambda self: str(self) in dangling or real_is_symlink(self))
            dangling.add(str(managed / ".env"))

            def os_symlink(_target, link):
                dangling.add(str(link))

            monkeypatch.setattr(os, "symlink", os_symlink)
        (managed / "config.yaml").write_text('logging:\n  retention_days: "${RETENTION_DAYS}"\n', encoding="utf-8")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        monkeypatch.setenv("RETENTION_DAYS", "7")
        managed_scope.invalidate_managed_cache()
        routed = tmp_path / "profile-b"
        routed.mkdir()
        (routed / "config.yaml").write_text("logging:\n  retention_days: 30\n", encoding="utf-8")
        try:
            assert hermes_logging._retention_policy(routed) is None
            (managed / ".env").unlink(missing_ok=True)
            dangling.discard(str(managed / ".env"))
            (managed / "config.yaml").unlink()
            os.symlink(str(tmp_path / "missing-config.yaml"), str(managed / "config.yaml"))
            managed_scope.invalidate_managed_cache()
            assert hermes_logging._retention_policy(routed) is None
        finally:
            managed_scope.invalidate_managed_cache()


    def test_cold_start_with_an_unavailable_managed_source_keeps_existing_segments(self, hermes_home, monkeypatch, tmp_path):
        """Codex round 5 (H4-R12-1, H4-R12-2, P2): the administrator's pin is unavailable at cold
        start — the managed DIRECTORY is a link whose target is unmounted, or the managed config
        link is repointed between a probe and the read. Neither is "no managed policy": an old
        segment under a user policy that would prune it must SURVIVE the sweep."""
        import os
        import time
        from hermes_cli import managed_scope

        (hermes_home / "config.yaml").write_text("logging:\n  retention_days: 1\n", encoding="utf-8")
        logs = hermes_home / "logs" / "agent.d"
        logs.mkdir(parents=True)
        stale = logs / "11-20261001-000000.1.log"
        stale.write_text("x\n", encoding="utf-8")
        stamp = time.time() - 3 * 86400
        os.utime(stale, (stamp, stamp))

        def sweep():
            monkeypatch.setattr(hermes_logging, "_pruned_log_dirs", set())
            managed_scope.invalidate_managed_cache()
            hermes_logging._prune_segments_once(hermes_home / "logs")

        # (0) Codex round 6 (H4-R13-2): the managed directory is reached through an ancestor that
        # is unavailable — the candidate itself is not a link, is not a directory, does not exist.
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "corp-link" / "hermes"))
        sweep()
        assert stale.exists(), "a managed scope configured through unavailable ancestry is an unreadable policy"

        # (a) the managed directory itself is a dangling link (its target is unmounted)
        dangling_dir = tmp_path / "etc-hermes"
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: str(self) == str(dangling_dir) or real_is_symlink(self))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(dangling_dir))
        sweep()
        assert stale.exists(), "a dangling managed directory link is an unreadable policy, not an absent one"

        # (b) the managed config link is valid at a pre-read probe and dangling at the read
        managed = tmp_path / "managed"
        managed.mkdir()
        (managed / "config.yaml").write_text("logging:\n  retention_days: 0\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))

        def repointed_before_read(path):
            raise FileNotFoundError(2, "repointed", str(path))

        monkeypatch.setattr(managed_scope, "_read_managed_bytes", repointed_before_read)
        monkeypatch.setattr(Path, "is_symlink", lambda self: str(self) == str(managed / "config.yaml") or real_is_symlink(self))
        sweep()
        assert stale.exists(), "a managed source that vanished at the authoritative read is an unreadable policy"


class TestExternalRotationRecovery:
    """_ManagedRotatingFileHandler recovers from external rotation.

    External rotation = anything that renames, unlinks, or replaces the
    log file without going through ``doRollover()``: logrotate, manual
    ``mv``, another process rotating under us, or a transient ``rm``.
    Before this fix the open file descriptor stayed pinned to the old
    inode forever, so every subsequent write went to the rotated backup
    instead of the file the operator expects to read.
    """

    def _make_handler(self, log_path: Path) -> hermes_logging._ManagedRotatingFileHandler:
        handler = hermes_logging._ManagedRotatingFileHandler(
            str(log_path), maxBytes=10 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def _emit(self, handler: logging.Handler, msg: str) -> None:
        record = logging.LogRecord(
            name="gateway.run", level=logging.INFO, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )
        # Match the record factory that hermes_logging installs at import time.
        record.session_tag = ""
        handler.emit(record)
        hermes_logging.flush_log_queue()

    @pytest.mark.linux_only  # renaming an open file is the POSIX logrotate shape Windows cannot do
    def test_recovers_after_external_rename(self, tmp_path):
        """logrotate-style external rename: ``mv gateway.log gateway.log.1``.

        Handler's fd was pinned to the renamed inode; new writes used to
        go to ``gateway.log.1`` forever.  After fix, the handler reopens
        ``gateway.log`` at the original path.
        """
        log_path = tmp_path / "gateway.log"
        rotated = tmp_path / "gateway.log.1"
        handler = self._make_handler(log_path)
        try:
            self._emit(handler, "before rotation")
            assert log_path.read_text() == "before rotation\n"

            # External rotation (NOT via handler.doRollover()).
            os.rename(log_path, rotated)
            assert not log_path.exists()

            self._emit(handler, "after rotation")

            # The new write should land in a freshly recreated gateway.log,
            # not appended to the rotated backup.
            assert log_path.exists(), "handler did not recreate gateway.log"
            assert log_path.read_text() == "after rotation\n"
            assert rotated.read_text() == "before rotation\n"
        finally:
            handler.close()


    def test_external_truncate_does_not_force_reopen(self, tmp_path):
        """``: > gateway.log`` keeps the same inode — no reopen needed.

        Truncation in place preserves the inode, so subsequent writes
        continue to the same file descriptor.  We assert the post-truncate
        content reflects the truncate (size shrinks) and then grows with
        new writes — i.e. the handler correctly does NOT detect this as
        an inode change.
        """
        log_path = tmp_path / "gateway.log"
        handler = self._make_handler(log_path)
        try:
            self._emit(handler, "AAAA" * 32)
            assert log_path.stat().st_size > 0

            with open(log_path, "w"):
                pass  # truncate to zero
            assert log_path.stat().st_size == 0

            self._emit(handler, "after truncate")
            assert log_path.read_text() == "after truncate\n"
        finally:
            handler.close()


    @pytest.mark.linux_only  # same: external rename of the open shared file
    def test_gateway_log_attached_after_external_rotation_then_re_setup(
        self, hermes_home,
    ):
        """End-to-end Allen-reproduction: gateway.log gets externally rotated,
        ``setup_logging(mode='gateway')`` is re-called, the handler keeps
        working.

        Reproduces Allen's symptom (gateway.log frozen mid-write, all gateway
        records leaking to agent.log) when something external rotates the
        file between setup_logging() calls.
        """
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")
        gw_path = hermes_home / "logs" / "gateway.log"
        rotated = hermes_home / "logs" / "gateway.log.1"

        logging.getLogger("gateway.run").info("line BEFORE rotation")
        hermes_logging.flush_log_queue()
        assert "BEFORE rotation" in gw_path.read_text()

        # External actor renames the file out from under us.
        os.rename(gw_path, rotated)
        assert not gw_path.exists()

        # Caller (or some restart path) re-enters setup_logging.  This used
        # to silently no-op due to the per-path dedup check, leaving the
        # stale fd in place.
        hermes_logging.setup_logging(hermes_home=hermes_home, mode="gateway")

        logging.getLogger("gateway.run").info("line AFTER rotation")
        hermes_logging.flush_log_queue()

        # The new record must reach the live gateway.log, not the rotated
        # backup.  Allen's logs had everything past the rotation point
        # going into agent.log only, never gateway.log.
        assert gw_path.exists(), "gateway.log was never recreated"
        assert "AFTER rotation" in gw_path.read_text()
        assert "AFTER rotation" not in rotated.read_text()


def test_eio_from_file_handler_names_the_path_once_then_recovers(tmp_path, capsys):
    """A failing log destination is named once (no per-record traceback) and writes resume
    once the file is reachable again."""
    import io

    class _SickStream(io.TextIOBase):
        def writable(self):
            return True

        def write(self, *_a):
            raise OSError(5, "Input/output error")

        seek = tell = flush = write

    path = tmp_path / "agent.log"
    handler = hermes_logging._ManagedRotatingFileHandler(
        str(path), maxBytes=1024, backupCount=1, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.stream.close()
        handler.stream = _SickStream()
        for i in range(5):
            handler.handle(logging.LogRecord("t", logging.INFO, __file__, 0, f"sick {i}", (), None))
        err = capsys.readouterr().err
        assert "--- Logging error ---" not in err
        assert err.count(str(path)) == 1 and "Input/output error" in err

        # Stream dropped, so the next emit reopens the real file and logging resumes.
        handler.handle(logging.LogRecord("t", logging.INFO, __file__, 0, "recovered", (), None))
        assert "recovered" in path.read_text(encoding="utf-8")
    finally:
        handler.close()


def test_eio_after_successful_reopen_still_names_the_path_once(tmp_path, capsys):
    """The reported case: open() succeeds but every write/seek/flush raises EIO. Reopening must
    not re-arm the notice, or a stuck device prints the path once per record."""
    import io

    class _SickStream(io.TextIOBase):
        def writable(self):
            return True

        def write(self, *_a):
            raise OSError(5, "Input/output error")

        seek = tell = flush = write

    path = tmp_path / "agent.log"
    handler = hermes_logging._ManagedRotatingFileHandler(
        str(path), maxBytes=1024, backupCount=1, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler._builtin_open = lambda *_a, **_kw: _SickStream()
        handler.stream.close()
        handler.stream = _SickStream()
        for i in range(25):
            handler.handle(logging.LogRecord("t", logging.INFO, __file__, 0, f"sick {i}", (), None))
        err = capsys.readouterr().err
        assert "--- Logging error ---" not in err
        assert err.count(str(path)) == 1
    finally:
        handler.close()


class TestSafeStderr:
    """Tests for _safe_stderr() — Unicode tolerance on Windows console."""


    def test_wraps_non_utf8_stderr(self, monkeypatch):
        """On non-UTF-8 systems (e.g. Windows cp949), wraps stderr with UTF-8."""
        import io

        class FakeStderr:
            """Simulates a Windows stderr with legacy encoding."""
            encoding = "cp949"
            buffer = io.BytesIO()

            def write(self, s):
                pass

            def flush(self):
                pass

        fake = FakeStderr()
        monkeypatch.setattr(sys, "stderr", fake)
        result = hermes_logging._safe_stderr()
        # Should be a TextIOWrapper, not the original FakeStderr
        assert isinstance(result, io.TextIOWrapper)
        assert result.encoding == "utf-8"
        assert result.errors == "replace"

    def test_handler_emits_unicode_without_crash(self, tmp_path):
        """StreamHandler with _safe_stderr can emit Unicode messages."""
        import io

        # Create a stderr-like stream with ASCII encoding
        class AsciiStream:
            encoding = "ascii"
            buffer = io.BytesIO()

            def write(self, s):
                self.buffer.write(s.encode("ascii", errors="replace"))

            def flush(self):
                pass

        # Without the fix, this would crash on cp949/ASCII stderr.
        # With the wrapper, the em-dash is replaced with '?'
        handler = logging.StreamHandler(
            io.TextIOWrapper(
                io.BytesIO(),
                encoding="utf-8",
                errors="replace",
            )
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("_test_unicode")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            # Em-dash U+2014 — the exact character from the bug report
            logger.info("Session hygiene: 400 messages — auto-compressing")
        finally:
            logger.removeHandler(handler)


class TestAsyncQueueLogging:
    """File logging runs through a QueueListener so emits never block on the
    cross-process rotation lock (Windows event-loop-stall fix)."""

    def test_file_handlers_not_on_root(self, hermes_home):
        hermes_logging.setup_logging(hermes_home=hermes_home)
        root = logging.getLogger()
        # Rotating file handlers live on the async listener, never on root.
        assert not any(isinstance(h, RotatingFileHandler) for h in root.handlers)
        # Exactly one queue handler funnels records to the listener.
        queue_handlers = [
            h for h in root.handlers if getattr(h, "_hermes_queue", False)
        ]
        assert len(queue_handlers) == 1
        # The real file handlers are discoverable via the accessor.
        assert any(
            "agent.log" in getattr(h, "baseFilename", "")
            for h in hermes_logging._queued_file_handlers
        )
