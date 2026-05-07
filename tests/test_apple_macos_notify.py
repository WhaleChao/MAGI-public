# -*- coding: utf-8 -*-
"""Tests for skills/ops/macos_notify.py — macOS notification integration."""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from skills.ops.macos_notify import (
    send_notification,
    _send_via_osascript,
    notify_omlx_error,
    notify_nas_status,
    notify_cron_failure,
    notify_pdf_processed,
    notify_case_deadline,
    GROUP_SYSTEM,
    GROUP_OMLX,
    GROUP_NAS,
)


class TestSendNotification:
    @patch("skills.ops.macos_notify.HAS_TERMINAL_NOTIFIER", False)
    @patch("skills.ops.macos_notify.subprocess.run")
    def test_osascript_fallback(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        ok = send_notification("Title", "Message")
        assert ok is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "osascript"

    @patch("skills.ops.macos_notify.HAS_TERMINAL_NOTIFIER", True)
    @patch("skills.ops.macos_notify._TERMINAL_NOTIFIER", "/usr/local/bin/terminal-notifier")
    @patch("skills.ops.macos_notify.subprocess.run")
    def test_terminal_notifier_preferred(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        ok = send_notification("Title", "Message")
        assert ok is True
        cmd = mock_run.call_args[0][0]
        assert "terminal-notifier" in cmd[0]

    @patch("skills.ops.macos_notify.HAS_TERMINAL_NOTIFIER", False)
    @patch("skills.ops.macos_notify.subprocess.run")
    def test_osascript_failure(self, mock_run):
        mock_run.side_effect = subprocess.SubprocessError("test error")
        ok = _send_via_osascript("Title", "Message")
        assert ok is False

    @patch("skills.ops.macos_notify.HAS_TERMINAL_NOTIFIER", False)
    @patch("skills.ops.macos_notify.subprocess.run")
    def test_escapes_double_quotes(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        ok = _send_via_osascript('Title "with" quotes', 'Message "here"')
        assert ok is True
        script = mock_run.call_args[0][0][-1]
        assert '\\"' in script or 'with' in script


class TestConvenienceFunctions:
    @patch("skills.ops.macos_notify.send_notification")
    def test_notify_omlx_error(self, mock_send):
        mock_send.return_value = True
        ok = notify_omlx_error("Connection refused")
        assert ok is True
        mock_send.assert_called_once()
        args = mock_send.call_args
        assert "oMLX" in args[1].get("title", "") or "oMLX" in args[0][0]

    @patch("skills.ops.macos_notify.send_notification")
    def test_notify_nas_connected(self, mock_send):
        mock_send.return_value = True
        ok = notify_nas_status(connected=True, share_name="homes")
        assert ok is True
        args = mock_send.call_args
        assert "重連" in str(args) or "掛載" in str(args)

    @patch("skills.ops.macos_notify.send_notification")
    def test_notify_nas_disconnected(self, mock_send):
        mock_send.return_value = True
        ok = notify_nas_status(connected=False, share_name="homes")
        assert ok is True
        args = mock_send.call_args
        assert "斷線" in str(args)

    @patch("skills.ops.macos_notify.send_notification")
    def test_notify_cron_failure(self, mock_send):
        mock_send.return_value = True
        ok = notify_cron_failure("pdf-namer-nightly", "timeout after 30s")
        assert ok is True

    @patch("skills.ops.macos_notify.send_notification")
    def test_notify_pdf_processed(self, mock_send):
        mock_send.return_value = True
        ok = notify_pdf_processed("scan001.pdf", "113年度勞訴19號裁定")
        assert ok is True

    @patch("skills.ops.macos_notify.send_notification")
    def test_notify_case_deadline_urgent(self, mock_send):
        mock_send.return_value = True
        ok = notify_case_deadline("113勞訴19", "答辯期限", days_left=2)
        assert ok is True
        args = mock_send.call_args
        assert "Basso" in str(args)  # urgent sound

    @patch("skills.ops.macos_notify.send_notification")
    def test_notify_case_deadline_normal(self, mock_send):
        mock_send.return_value = True
        ok = notify_case_deadline("113勞訴19", "書狀提出", days_left=10)
        assert ok is True
        args = mock_send.call_args
        assert "default" in str(args)  # normal sound
