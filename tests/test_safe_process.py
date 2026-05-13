# -*- coding: utf-8 -*-
"""Tests for api.platform.safe_process (R2)."""

from __future__ import annotations

import os
import threading
import time
import pytest

from api.platforms import safe_process as sp


@pytest.fixture(autouse=True)
def _reset_sem():
    sp.reset_for_test()
    yield
    sp.reset_for_test()


# --- argv 白名單 --------------------------------------------------------

def test_argv_head_whitelisted_python3():
    r = sp.run(["python3", "-c", "print('ok')"], timeout_sec=10)
    assert r.returncode == 0 and "ok" in r.stdout


def test_argv_head_rejected_bash():
    with pytest.raises(PermissionError):
        sp.run(["bash", "-c", "echo x"])


def test_argv_head_rejected_sh():
    with pytest.raises(PermissionError):
        sp.run(["/bin/sh", "-c", "echo x"])


def test_argv_empty_rejected():
    with pytest.raises(ValueError):
        sp.run([])


def test_argv_non_string_rejected():
    with pytest.raises(TypeError):
        sp.run(["python3", 123])


# --- shell metachar denylist -------------------------------------------

def test_argv_semicolon_rejected():
    # 測試非 code 引數中的 shell injection（git msg 含 ;）
    with pytest.raises(PermissionError):
        sp.run(["git", "commit", "-m", "msg; rm -rf /"])


def test_argv_pipe_rejected():
    with pytest.raises(PermissionError):
        sp.run(["git", "commit", "-m", "msg | cat /etc/passwd"])


def test_argv_backtick_rejected():
    with pytest.raises(PermissionError):
        sp.run(["git", "commit", "-m", "`whoami`"])


# --- env 白名單 ---------------------------------------------------------

def test_env_default_prefix_filters_secrets(monkeypatch):
    monkeypatch.setenv("MAGI_X", "visible")
    monkeypatch.setenv("SECRET_TOKEN", "HIDDEN")
    r = sp.run(
        ["python3", "-c", "import os; print(os.environ.get('MAGI_X','')); print(os.environ.get('SECRET_TOKEN','NOPE'))"],
        timeout_sec=10,
    )
    assert "visible" in r.stdout
    assert "HIDDEN" not in r.stdout


def test_env_custom_prefix_extends(monkeypatch):
    monkeypatch.setenv("CUSTOM_OK", "yes")
    r = sp.run(
        ["python3", "-c", "import os; print(os.environ.get('CUSTOM_OK',''))"],
        env_whitelist_prefixes=("MAGI_", "CUSTOM_"),
        timeout_sec=10,
    )
    assert "yes" in r.stdout


def test_env_extra_respects_default_whitelist():
    r = sp.run(
        [
            "python3",
            "-c",
            "import os; print(os.environ.get('MAGI_EXTRA','')); print(os.environ.get('SECRET_EXTRA','NOPE'))",
        ],
        env_extra={"MAGI_EXTRA": "visible", "SECRET_EXTRA": "hidden"},
        timeout_sec=10,
    )
    assert "visible" in r.stdout
    assert "hidden" not in r.stdout


# --- timeout / kill -----------------------------------------------------

def test_timeout_triggers_sigterm():
    r = sp.run(["python3", "-c", "import time; time.sleep(30)"], timeout_sec=2.0)
    assert r.timed_out is True
    assert r.returncode != 0


def test_sigkill_after_grace():
    # 子進程 ignore SIGTERM → 必須被 SIGKILL
    code = (
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )
    r = sp.run(["python3", "-c", code], timeout_sec=1.0)
    assert r.timed_out is True and r.killed is True


# --- stdout / stderr cap -----------------------------------------------

def test_stdout_truncated_at_1mb():
    code = "print('x' * (2 * 1024 * 1024))"
    r = sp.run(["python3", "-c", code], timeout_sec=15)
    assert "truncated" in r.stdout
    assert len(r.stdout.encode("utf-8")) <= 1_048_576 + 200


# --- launchctl ----------------------------------------------------------

def test_launchctl_label_regex_accepts_valid():
    # 只驗證 regex，不真的跑 launchctl
    assert sp._LAUNCHCTL_LABEL_RE.match("com.magi.daemon")
    assert sp._LAUNCHCTL_LABEL_RE.match("com.magi.omlx-phi4")


def test_launchctl_label_regex_rejects_invalid():
    with pytest.raises(PermissionError):
        sp.launchctl_op("bootout", "com.other.service")
    with pytest.raises(PermissionError):
        sp.launchctl_op("bootout", "com.magi.DAEMON")  # 大寫不准
    with pytest.raises(PermissionError):
        sp.launchctl_op("bootout", "com.magi.;rm")


def test_launchctl_op_whitelist():
    with pytest.raises(PermissionError):
        sp.launchctl_op("unload", "com.magi.daemon")   # 舊動詞不准


# --- parse_cron_command -------------------------------------------------

def test_parse_cron_simple():
    assert sp.parse_cron_command("python3 script.py --flag x") == [
        "python3", "script.py", "--flag", "x"
    ]


def test_parse_cron_rejects_pipe():
    with pytest.raises(PermissionError):
        sp.parse_cron_command("python3 a.py | cat")


def test_parse_cron_rejects_dollar_paren():
    with pytest.raises(PermissionError):
        sp.parse_cron_command("python3 a.py $(whoami)")


# --- OCR runtime whitelist (Phase A) ------------------------------------

def test_tesseract_in_whitelist():
    """tesseract must be whitelisted so tesseract_provider can use SafeProcess."""
    assert "tesseract" in sp._ARGV0_WHITELIST


def test_pdftoppm_in_whitelist():
    """pdftoppm must be whitelisted for PDF → image conversion (Phase C)."""
    assert "pdftoppm" in sp._ARGV0_WHITELIST


def test_tesseract_argv_accepted_without_run():
    """_validate_argv should accept tesseract without raising PermissionError."""
    # Does not actually run tesseract — just validates argv construction
    sp._validate_argv(["tesseract", "/tmp/test.png", "stdout", "-l", "chi_tra+eng", "--psm", "3"])


def test_pdftoppm_argv_accepted_without_run():
    """_validate_argv should accept pdftoppm without raising PermissionError."""
    sp._validate_argv(["pdftoppm", "-r", "300", "/tmp/test.pdf", "/tmp/test_out"])
