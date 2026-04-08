# -*- coding: utf-8 -*-
"""Tests for skills/apple/eventkit_bridge.py — Apple Calendar + Reminders."""

from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, call

import pytest

from skills.apple.eventkit_bridge import (
    _escape_applescript,
    _run_osascript,
    ensure_calendar_exists,
    create_calendar_event,
    check_event_exists,
    ensure_reminder_list_exists,
    create_reminder,
    create_trial_events,
    create_case_deadline_reminder,
    parse_trial_command,
    DEFAULT_CALENDAR,
    DEFAULT_REMINDER_LIST,
)


# ── AppleScript utilities ──

class TestEscapeAppleScript:
    def test_escapes_double_quotes(self):
        assert _escape_applescript('hello "world"') == 'hello \\"world\\"'

    def test_escapes_backslash(self):
        assert _escape_applescript("path\\to\\file") == "path\\\\to\\\\file"

    def test_no_change_for_safe_string(self):
        assert _escape_applescript("hello world") == "hello world"

    def test_chinese_characters(self):
        assert _escape_applescript("臺灣臺北地方法院") == "臺灣臺北地方法院"


class TestRunOsascript:
    @patch("skills.apple.eventkit_bridge.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok\n")
        ok, output = _run_osascript('display notification "test"')
        assert ok is True
        assert output == "ok"

    @patch("skills.apple.eventkit_bridge.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="error msg")
        ok, output = _run_osascript('invalid script')
        assert ok is False
        assert "error msg" in output

    @patch("skills.apple.eventkit_bridge.subprocess.run")
    def test_timeout(self, mock_run):
        from subprocess import TimeoutExpired
        mock_run.side_effect = TimeoutExpired(cmd="osascript", timeout=10)
        ok, output = _run_osascript('long script')
        assert ok is False
        assert "timeout" in output


# ── Calendar operations ──

class TestCalendarOperations:
    @patch("skills.apple.eventkit_bridge._run_osascript")
    def test_ensure_calendar_exists(self, mock_osa):
        mock_osa.return_value = (True, "exists")
        assert ensure_calendar_exists() is True

    @patch("skills.apple.eventkit_bridge._run_osascript")
    def test_create_calendar_event(self, mock_osa):
        mock_osa.return_value = (True, "")
        start = datetime(2026, 5, 1, 9, 30)
        ok = create_calendar_event(
            title="開庭：113勞訴19",
            start=start,
            location="臺北地方法院",
            notes="當事人：黃語玲",
        )
        assert ok is True
        # Verify osascript was called with correct calendar name
        script = mock_osa.call_args[0][0]
        assert DEFAULT_CALENDAR in script
        assert "開庭" in script

    @patch("skills.apple.eventkit_bridge._run_osascript")
    def test_create_all_day_event(self, mock_osa):
        mock_osa.return_value = (True, "")
        start = datetime(2026, 5, 1)
        ok = create_calendar_event(title="全天事件", start=start, all_day=True)
        assert ok is True
        script = mock_osa.call_args[0][0]
        assert "allday event:true" in script

    @patch("skills.apple.eventkit_bridge._run_osascript")
    def test_check_event_exists_true(self, mock_osa):
        mock_osa.return_value = (True, "1")
        result = check_event_exists("開庭：113勞訴19", datetime(2026, 5, 1))
        assert result is True

    @patch("skills.apple.eventkit_bridge._run_osascript")
    def test_check_event_exists_false(self, mock_osa):
        mock_osa.return_value = (True, "0")
        result = check_event_exists("不存在的事件", datetime(2026, 5, 1))
        assert result is False


# ── Reminder operations ──

class TestReminderOperations:
    @patch("skills.apple.eventkit_bridge._run_osascript")
    def test_ensure_reminder_list(self, mock_osa):
        mock_osa.return_value = (True, "created")
        assert ensure_reminder_list_exists() is True

    @patch("skills.apple.eventkit_bridge._run_osascript")
    def test_create_reminder_with_due_date(self, mock_osa):
        mock_osa.return_value = (True, "")
        due = datetime(2026, 5, 10, 17, 0)
        ok = create_reminder(
            title="答辯期限：113勞訴19",
            due_date=due,
            notes="收到起訴狀後 20 天",
            priority=9,
        )
        assert ok is True
        script = mock_osa.call_args[0][0]
        assert DEFAULT_REMINDER_LIST in script
        assert "priority:9" in script

    @patch("skills.apple.eventkit_bridge._run_osascript")
    def test_create_reminder_without_due_date(self, mock_osa):
        mock_osa.return_value = (True, "")
        ok = create_reminder(title="備忘事項", notes="just a note")
        assert ok is True


# ── Trial event workflow ──

class TestCreateTrialEvents:
    @patch("skills.apple.eventkit_bridge.create_reminder")
    @patch("skills.apple.eventkit_bridge.create_calendar_event")
    @patch("skills.apple.eventkit_bridge.check_event_exists", return_value=False)
    @patch("skills.apple.eventkit_bridge.ensure_reminder_list_exists", return_value=True)
    @patch("skills.apple.eventkit_bridge.ensure_calendar_exists", return_value=True)
    def test_creates_all_items(self, mock_ensure_cal, mock_ensure_rem,
                                mock_exists, mock_cal_event, mock_reminder):
        mock_cal_event.return_value = True
        mock_reminder.return_value = True

        trial_date = datetime.now() + timedelta(days=14)
        results = create_trial_events(
            case_number="113勞訴19",
            trial_date=trial_date,
            court="臺北地方法院",
            client="黃語玲",
        )

        assert "開庭事件" in results
        assert "開庭前3天提醒" in results
        assert "開庭前1天提醒" in results
        assert mock_cal_event.call_count == 1
        assert mock_reminder.call_count == 2

    @patch("skills.apple.eventkit_bridge.check_event_exists", return_value=True)
    def test_skips_duplicate_event(self, mock_exists):
        results = create_trial_events(
            case_number="113勞訴19",
            trial_date=datetime.now() + timedelta(days=14),
        )
        assert "已存在" in results[0]


# ── Command parsing ──

class TestParseTrialCommand:
    def test_full_format(self):
        result = parse_trial_command("!開庭 113勞訴19 2026-05-01 09:30")
        assert result is not None
        assert result["case_number"] == "113勞訴19"
        assert result["trial_date"] == datetime(2026, 5, 1, 9, 30)

    def test_without_time(self):
        result = parse_trial_command("!開庭 113勞訴19 2026-05-01")
        assert result is not None
        assert result["trial_date"].hour == 9
        assert result["trial_date"].minute == 30

    def test_full_case_number(self):
        result = parse_trial_command("!開庭 113年度勞訴字第19號 2026-05-01 14:00")
        assert result is not None
        assert "113年度勞訴字第19號" in result["case_number"]

    def test_slash_date(self):
        result = parse_trial_command("!開庭 114訴88 2026/06/15 10:00")
        assert result is not None
        assert result["trial_date"].month == 6

    def test_invalid_format(self):
        assert parse_trial_command("隨便打字") is None

    def test_missing_date(self):
        assert parse_trial_command("!開庭 113勞訴19") is None

    def test_fullwidth_exclamation(self):
        # ！（全形）should also be handled by command dispatch
        result = parse_trial_command("!開庭 113勞訴19 2026-05-01")
        assert result is not None


# ── Case deadline reminder ──

class TestCaseDeadlineReminder:
    @patch("skills.apple.eventkit_bridge.create_reminder")
    def test_creates_deadline_reminder(self, mock_reminder):
        mock_reminder.return_value = True
        ok = create_case_deadline_reminder(
            case_number="113勞訴19",
            deadline_type="答辯期限",
            deadline_date=datetime(2026, 5, 20, 17, 0),
            client="黃語玲",
        )
        assert ok is True
        args = mock_reminder.call_args
        assert "答辯期限" in args[1]["title"]
        assert args[1]["priority"] == 9
