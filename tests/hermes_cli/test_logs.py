"""Tests for hermes_cli.logs — log viewing and filtering."""

from datetime import datetime, timedelta


from hermes_cli.logs import (
    LOG_FILES,
    _extract_level,
    _extract_logger_name,
    _line_matches_component,
    _matches_filters,
    _parse_line_timestamp,
    _parse_since,
)


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

class TestParseSince:
    def test_hours(self):
        cutoff = _parse_since("2h")
        assert cutoff is not None
        assert abs((datetime.now() - cutoff).total_seconds() - 7200) < 2


    def test_invalid_returns_none(self):
        assert _parse_since("abc") is None
        assert _parse_since("") is None
        assert _parse_since("10x") is None

    def test_whitespace_tolerance(self):
        cutoff = _parse_since("  5m  ")
        assert cutoff is not None


class TestParseLineTimestamp:
    def test_standard_format(self):
        ts = _parse_line_timestamp("2026-04-11 10:23:45 INFO gateway.run: msg")
        assert ts == datetime(2026, 4, 11, 10, 23, 45)


class TestExtractLevel:
    def test_info(self):
        assert _extract_level("2026-01-01 00:00:00 INFO gateway.run: msg") == "INFO"


# ---------------------------------------------------------------------------
# Logger name extraction (new for component filtering)
# ---------------------------------------------------------------------------

class TestExtractLoggerName:
    def test_standard_line(self):
        line = "2026-04-11 10:23:45 INFO gateway.run: Starting gateway"
        assert _extract_logger_name(line) == "gateway.run"


    def test_no_match(self):
        assert _extract_logger_name("random text") is None


class TestLineMatchesComponent:

    def test_gateway_nested(self):
        # Migrated platform adapters log under plugins.platforms.* (#41112) and
        # must still resolve to the gateway component. Use the real expanded
        # gateway prefixes (COMPONENT_PREFIXES["gateway"]) the CLI passes, not a
        # bare ("gateway",), since the logger name no longer literally starts
        # with "gateway".
        from hermes_logging import COMPONENT_PREFIXES
        line = "2026-04-11 10:23:45 INFO plugins.platforms.telegram.adapter: msg"
        assert _line_matches_component(line, COMPONENT_PREFIXES["gateway"])





    def test_unparseable_line(self):
        assert not _line_matches_component("random text", ("gateway",))


# ---------------------------------------------------------------------------
# Combined filter
# ---------------------------------------------------------------------------

class TestMatchesFilters:

    def test_level_filter(self):
        assert _matches_filters(
            "2026-01-01 00:00:00 WARNING x: msg", min_level="WARNING")
        assert not _matches_filters(
            "2026-01-01 00:00:00 INFO x: msg", min_level="WARNING")


    def test_combined_filters(self):
        """All filters must pass for a line to match."""
        line = "2026-04-11 10:00:00 WARNING [sess_1] gateway.run: connection lost"
        assert _matches_filters(
            line,
            min_level="WARNING",
            session_filter="sess_1",
            component_prefixes=("gateway",),
        )
        # Fails component filter
        assert not _matches_filters(
            line,
            min_level="WARNING",
            session_filter="sess_1",
            component_prefixes=("tools",),
        )

    def test_since_filter(self):
        # Line with a very old timestamp should be filtered out
        assert not _matches_filters(
            "2020-01-01 00:00:00 INFO x: old msg",
            since=datetime.now() - timedelta(hours=1))
        # Line with a recent timestamp should pass
        recent = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        assert _matches_filters(
            f"{recent} INFO x: recent msg",
            since=datetime.now() - timedelta(hours=1))


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# LOG_FILES registry
# ---------------------------------------------------------------------------

class TestLogFiles:
    def test_known_log_files(self):
        assert "agent" in LOG_FILES
        assert "errors" in LOG_FILES
        assert "gateway" in LOG_FILES
        assert "gui" in LOG_FILES


# ---------------------------------------------------------------------------
# tail_log / list_logs over the merged layout
# ---------------------------------------------------------------------------

class TestTailLogFollow:
    def test_follow_on_a_home_with_no_log_yet_waits_instead_of_exiting(self, tmp_path, monkeypatch, capsys):
        """A fresh Windows home has no file until the first record (delay=True); `hermes logs -f`
        must wait for it, not exit 1."""
        import hermes_cli.logs_merge as merge
        from hermes_cli.logs import tail_log

        home = tmp_path / "home"
        monkeypatch.setenv("HERMES_HOME", str(home))
        calls = []
        monkeypatch.setattr(merge, "follow_merged", lambda log_dir, filename, emit, **kw: calls.append((log_dir, filename)))
        tail_log("agent", follow=True)
        assert calls == [(home / "logs", "agent.log")]

    def test_without_follow_a_missing_log_still_exits(self, tmp_path, monkeypatch):
        import pytest
        from hermes_cli.logs import tail_log

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        with pytest.raises(SystemExit):
            tail_log("agent", follow=False)

    def test_follow_resumes_from_the_extents_the_tail_read(self, tmp_path, monkeypatch, capsys):
        """tail_log hands follow_merged the byte extents its initial tail consumed (H4-R1-2b)."""
        import hermes_cli.logs_merge as merge
        from hermes_cli.logs import tail_log

        home = tmp_path / "home"
        monkeypatch.setenv("HERMES_HOME", str(home))
        seg = home / "logs" / "agent.d" / "1-20261030-100000.1.log"
        seg.parent.mkdir(parents=True)
        seg.write_bytes(b"2026-10-30 10:00:00 INFO a: one\n2026-10-30 10:00:01 INFO a: two\n")
        captured = {}
        monkeypatch.setattr(merge, "follow_merged", lambda log_dir, filename, emit, **kw: captured.update(kw))
        tail_log("agent", follow=True)
        assert captured["start_positions"] == {seg: seg.stat().st_size}


class TestListLogs:
    def test_list_shows_rotated_backups_and_sums_segments(self, tmp_path, monkeypatch, capsys):
        from hermes_cli.logs import list_logs

        home = tmp_path / "home"
        monkeypatch.setenv("HERMES_HOME", str(home))
        logs = home / "logs"
        (logs / "agent.d").mkdir(parents=True)
        (logs / "agent.log").write_text("live\n")
        (logs / "agent.log.1").write_text("rotated\n")
        (logs / "agent.d" / "1-20261030-100000.1.log").write_text("a\n")
        (logs / "agent.d" / "2-20261030-100001.1.log").write_text("b\n")
        list_logs()
        out = capsys.readouterr().out
        assert "agent.log.1" in out
        assert "agent.d/" in out and "2 process segments" in out


    def test_a_log_nobody_may_read_says_permission_denied(self, tmp_path, monkeypatch, capsys):
        """When every file of the log refuses to open, say so and exit 1 — not an empty banner
        with exit 0 (H4-R3-2b)."""
        import pytest
        import hermes_cli.logs_merge as merge
        from hermes_cli.logs import tail_log

        home = tmp_path / "home"
        monkeypatch.setenv("HERMES_HOME", str(home))
        seg = home / "logs" / "agent.d" / "1-20261030-100000.1.log"
        seg.parent.mkdir(parents=True)
        seg.write_text("2026-10-30 10:00:00 INFO a: one\n", encoding="utf-8")

        def denied(path, n, *args, **kwargs):
            raise PermissionError(13, "denied")

        monkeypatch.setattr(merge, "_tail_lines", denied)
        with pytest.raises(SystemExit):
            tail_log("agent", follow=False)
        assert "Permission denied" in capsys.readouterr().out


    def test_a_partially_unreadable_log_prints_a_note_about_the_skipped_file(self, tmp_path, monkeypatch, capsys):
        import hermes_cli.logs_merge as merge
        from hermes_cli.logs import tail_log

        home = tmp_path / "home"
        monkeypatch.setenv("HERMES_HOME", str(home))
        logs = home / "logs"
        logs.mkdir(parents=True)
        (logs / "agent.log.1").write_text("2026-10-30 09:00:00 INFO a: old\n", encoding="utf-8")
        live = logs / "agent.log"
        live.write_text("2026-10-30 10:00:00 INFO a: live\n", encoding="utf-8")
        real = merge._tail_lines

        def flaky(path, n, *args, **kwargs):
            if path == live:
                raise PermissionError(13, "denied")
            return real(path, n, *args, **kwargs)

        monkeypatch.setattr(merge, "_tail_lines", flaky)
        tail_log("agent", follow=False)
        out = capsys.readouterr()
        assert "old" in out.out
        assert "agent.log" in out.err and "skipped" in out.err.lower()
