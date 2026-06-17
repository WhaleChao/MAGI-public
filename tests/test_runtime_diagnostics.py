from __future__ import annotations

from api.runtime_diagnostics import classify_model_health, classify_runtime_error


def test_classify_runtime_error_categorizes_known_payloads():
    assert classify_runtime_error({"error": "stat timeout after 30s on /Volumes/sync/abc"}) == "nas_path_timeout"
    assert classify_runtime_error("google.oauth error: token expired") == "google_token_expired"
    assert classify_runtime_error("Drive API returned 403 permission denied") == "drive_auth_denied"
    assert classify_runtime_error("File stage failed: could not write to staging dir") == "file_stage_failed"
    assert classify_runtime_error("檔案讀取失敗：[Errno 4] system copy staging failed on /Volumes/homes") == "file_stage_failed"
    assert classify_runtime_error("python ssl malloc crash detected") == "python_ssl_malloc_crash"
    assert classify_runtime_error("model unavailable: offline") == "model_unavailable"
    assert classify_runtime_error("an unrelated temporary failure") == "unknown"


def test_classify_model_health_detects_overload_and_unavailable():
    assert classify_model_health({"status": "overload"}) == "overload"
    assert classify_model_health({"status": "down"}) == "unavailable"
    assert classify_model_health({"available": False}) == "unavailable"
    assert classify_model_health({"ok": False}) == "unavailable"


def test_classify_model_health_unknown_on_nonaligned_payload():
    assert classify_model_health("service degraded") == "unknown"
    assert classify_model_health({"status": "ready"}) == "ok"
