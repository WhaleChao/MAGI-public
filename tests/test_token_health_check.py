import json
from datetime import datetime, timedelta, timezone

from scripts.ops import token_health_check as thc


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
