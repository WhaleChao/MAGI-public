import json
import subprocess
import sys
import time
import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from scripts.ops import token_health_check as thc
from scripts.ops import run_after_token_refresh


def _write_token(path, *, expiry, scopes=None, refresh_token="refresh-token"):
    path.write_text(
        json.dumps(
            {
                "token": "access-token-secret",
                "refresh_token": refresh_token,
                "client_id": "client-id",
                "client_secret": "client-secret",
                "expiry": expiry.astimezone(timezone.utc).isoformat(),
                "scopes": scopes or ["scope-a", "scope-b"],
            }
        ),
        encoding="utf-8",
    )


def test_google_token_ok_report_does_not_include_secret_values(tmp_path):
    token = tmp_path / "token.json"
    _write_token(token, expiry=datetime.now(timezone.utc) + timedelta(days=30))
    spec = thc.GoogleTokenSpec(name="unit", token_path=token, scopes=["scope-a"])

    result = thc.check_google_token(spec, refresh=False, threshold_seconds=7 * 24 * 3600)

    assert result["ok"] is True
    assert result["status"] == "ok"
    serialized = json.dumps(result)
    assert "access-token-secret" not in serialized
    assert "refresh-token" not in serialized
    assert "client-secret" not in serialized


def test_google_token_expiring_soon_is_actionable_without_refresh(tmp_path):
    token = tmp_path / "token.json"
    _write_token(token, expiry=datetime.now(timezone.utc) + timedelta(hours=2))
    spec = thc.GoogleTokenSpec(name="unit", token_path=token, scopes=["scope-a"])

    result = thc.check_google_token(spec, refresh=False, threshold_seconds=7 * 24 * 3600)

    assert result["ok"] is False
    assert result["status"] == "expiring_soon"


def test_google_token_expired_is_actionable_and_fails(tmp_path):
    token = tmp_path / "token.json"
    _write_token(token, expiry=datetime.now(timezone.utc) - timedelta(hours=1))
    spec = thc.GoogleTokenSpec(name="unit", token_path=token, scopes=["scope-a"])

    result = thc.check_google_token(spec, refresh=False, threshold_seconds=7 * 24 * 3600)

    assert result["ok"] is False
    assert result["status"] == "expired"


def test_google_token_missing_scope_fails(tmp_path):
    token = tmp_path / "token.json"
    _write_token(token, expiry=datetime.now(timezone.utc) + timedelta(days=30), scopes=["scope-a"])
    spec = thc.GoogleTokenSpec(name="unit", token_path=token, scopes=["scope-a", "scope-c"])

    result = thc.check_google_token(spec, refresh=False)

    assert result["ok"] is False
    assert result["status"] == "missing_scope"
    assert result["scopes_ok"] is False
    assert result["missing_scopes"] == ["scope-c"]
    assert "Re-authorize" in result["next_action"]


def test_google_token_missing_refresh_token_is_actionable_without_network(tmp_path):
    token = tmp_path / "token.json"
    _write_token(token, expiry=datetime.now(timezone.utc) + timedelta(days=30), refresh_token="")
    spec = thc.GoogleTokenSpec(name="unit", token_path=token, scopes=["scope-a"])

    result = thc.check_google_token(spec, refresh=True)

    assert result["ok"] is False
    assert result["status"] == "auth_required"
    assert result["refresh_token_present"] is False
    assert result["auth_required_reason"] == "missing_refresh_token"
    assert "refresh_token" in result["message"]
    assert "offline access" in result["next_action"]


def test_google_token_account_mismatch_is_actionable(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(
        json.dumps(
            {
                "token": "access-token-secret",
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "expiry": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
                "scopes": ["scope-a"],
                "account_email": "wrong@example.com",
            }
        ),
        encoding="utf-8",
    )
    spec = thc.GoogleTokenSpec(name="unit", token_path=token, scopes=["scope-a"], account_hint="right@example.com")

    result = thc.check_google_token(spec, refresh=False)

    assert result["ok"] is False
    assert result["status"] == "account_mismatch"
    assert result["account_hint_ok"] is False
    assert result["account_mismatch"] is True
    assert result["account_from_token"] == "wrong@example.com"
    assert "right@example.com" in result["next_action"]


def test_token_report_failures_keep_oauth_diagnostics(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    _write_token(token, expiry=datetime.now(timezone.utc) + timedelta(days=30), scopes=["scope-a"])
    spec = thc.GoogleTokenSpec(name="unit", token_path=token, scopes=["scope-a", "scope-c"])
    monkeypatch.setattr(thc, "_discover_google_tokens", lambda: [spec])
    monkeypatch.setattr(thc, "_discover_api_keys", lambda: [])

    report = thc.build_report(refresh=False, threshold_days=7.0)

    failure = report["failures"][0]
    assert failure["name"] == "unit"
    assert failure["refresh_token_present"] is True
    assert failure["scopes_ok"] is False
    assert failure["missing_scopes"] == ["scope-c"]
    assert "next_action" in failure


def test_optional_missing_google_token_is_skipped(tmp_path):
    spec = thc.GoogleTokenSpec(name="unit", token_path=tmp_path / "missing.json", scopes=["scope-a"], required=False)

    result = thc.check_google_token(spec)

    assert result["ok"] is True
    assert result["status"] == "skipped"


def test_api_key_required_env_reports_missing(monkeypatch):
    monkeypatch.delenv("SOME_API_KEY", raising=False)
    monkeypatch.setenv("MAGI_REQUIRE_SOME_API_KEY", "1")
    spec = thc.ApiKeySpec(
        name="some",
        env_names=("SOME_API_KEY",),
        required_env="MAGI_REQUIRE_SOME_API_KEY",
    )

    result = thc.check_api_key(spec)

    assert result["ok"] is False
    assert result["status"] == "missing_key"


def test_run_after_token_refresh_blocks_when_refresh_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        run_after_token_refresh.token_health_check,
        "build_report",
        lambda **kwargs: {
            "ok": False,
            "failures": [
                {
                    "name": "google_calendar",
                    "status": "auth_required",
                    "refresh_token_present": False,
                    "scopes_ok": True,
                    "next_action": "Re-authorize with account primary.",
                }
            ],
        },
    )
    writes = []
    monkeypatch.setattr(
        run_after_token_refresh.token_health_check,
        "_atomic_write_text",
        lambda *args, **kwargs: writes.append(args),
    )
    monkeypatch.setattr(run_after_token_refresh.os, "execvpe", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not exec")))

    rc = run_after_token_refresh.main(["--", "python", "job.py"])

    assert rc == 1
    assert writes
    stderr = capsys.readouterr().err
    assert "refresh_token_present=False" in stderr
    assert "next_action=Re-authorize" in stderr


def test_run_after_token_refresh_execs_with_env_prefix(monkeypatch):
    monkeypatch.setattr(
        run_after_token_refresh.token_health_check,
        "build_report",
        lambda **kwargs: {"ok": True, "failures": []},
    )
    monkeypatch.setattr(run_after_token_refresh.token_health_check, "_atomic_write_text", lambda *args, **kwargs: None)
    called = {}

    def fake_execvpe(program, command, env):
        called["program"] = program
        called["command"] = command
        called["env"] = env
        raise SystemExit(0)

    monkeypatch.setattr(run_after_token_refresh.os, "execvpe", fake_execvpe)

    try:
        run_after_token_refresh.main(["MAGI_TEST_FLAG=1", "--", "python", "job.py"])
    except SystemExit as exc:
        assert exc.code == 0

    assert called["program"] == "python"
    assert called["command"] == ["python", "job.py"]
    assert called["env"]["MAGI_TEST_FLAG"] == "1"

    called.clear()
    try:
        run_after_token_refresh.main(["--", "MAGI_TEST_FLAG=2", "python", "job.py"])
    except SystemExit as exc:
        assert exc.code == 0

    assert called["program"] == "python"
    assert called["command"] == ["python", "job.py"]
    assert called["env"]["MAGI_TEST_FLAG"] == "2"


def test_google_token_file_lock_times_out_when_held(tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import pathlib,time\n"
                "from scripts.ops.token_health_check import google_token_file_lock\n"
                f"p=pathlib.Path({str(token)!r})\n"
                "with google_token_file_lock(p, timeout_sec=1):\n"
                "    time.sleep(1.5)\n"
            ),
        ],
        cwd=str(thc.MAGI_ROOT),
    )
    time.sleep(0.2)
    try:
        with pytest.raises(TimeoutError):
            with thc.google_token_file_lock(token, timeout_sec=0.01):
                pass
    finally:
        holder.terminate()
        holder.wait(timeout=5)
