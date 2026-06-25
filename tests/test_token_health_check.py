import json
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


def test_run_after_token_refresh_blocks_when_refresh_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        run_after_token_refresh.token_health_check,
        "build_report",
        lambda **kwargs: {"ok": False, "failures": [{"name": "google_calendar", "status": "auth_required"}]},
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
