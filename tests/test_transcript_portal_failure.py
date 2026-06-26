import importlib.util
from pathlib import Path
from types import SimpleNamespace


ACTION_PATH = Path(__file__).resolve().parents[1] / "skills" / "transcript-downloader" / "action.py"
SPEC = importlib.util.spec_from_file_location("transcript_downloader_action_for_failure_test", ACTION_PATH)
action = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(action)


def test_portal_failure_reports_not_authorized_instead_of_generic_sso():
    downloader = SimpleNamespace(
        last_login_error_code="",
        last_login_error_detail=(
            "ezlawyer_not_authorized: 已登入電子筆錄服務網，但目前帳號/入口未授權存取調閱頁"
        ),
        _last_download_error="",
    )

    code, msg, reason = action._portal_failure_from_downloader(downloader)

    assert code == "ezlawyer_not_authorized"
    assert reason == "not_authorized"
    assert "SSO login failed" not in msg
    assert "未授權" in msg


def test_portal_failure_keeps_generic_login_failure_when_no_detail():
    downloader = SimpleNamespace(
        last_login_error_code="",
        last_login_error_detail="",
        _last_download_error="",
    )

    code, msg, reason = action._portal_failure_from_downloader(downloader)

    assert code == "login_failed"
    assert reason == "login_failed"
    assert msg == "SSO login failed"


def test_failed_case_attempt_does_not_complete_sync_cycle():
    case = SimpleNamespace(
        case_number="2026-0001",
        court_name="臺灣花蓮地方法院",
        court_case_number="115年度訴字第1號",
        case_type="刑事",
        client_name="測試人",
        folder_path="",
    )
    state = {"version": 1, "cycle": 1, "cycle_started_at": "", "last_cycle_completed_at": "", "cases": {}}

    action._prepare_sync_cycle(state)
    action._record_case_attempt(state, case, status="search_failed", success=False, error="SSO login failed")
    action._update_cycle_completion(state, [case])

    assert state["cycle_scanned_cases"] == 0
    assert state["last_cycle_completed_at"] == ""


def test_successful_case_attempt_completes_sync_cycle():
    case = SimpleNamespace(
        case_number="2026-0002",
        court_name="臺灣花蓮地方法院",
        court_case_number="115年度訴字第2號",
        case_type="刑事",
        client_name="測試人",
        folder_path="",
    )
    state = {"version": 1, "cycle": 1, "cycle_started_at": "", "last_cycle_completed_at": "", "cases": {}}

    action._prepare_sync_cycle(state)
    action._record_case_attempt(state, case, status="no_new_files", success=True, files=[])
    action._update_cycle_completion(state, [case])

    assert state["last_cycle_completed_at"]


def test_download_all_payload_failure_is_not_success(monkeypatch):
    class _Downloader:
        def __init__(self, *args, **kwargs):
            pass

        def download_all(self):
            return {"success": False, "error": "SSO login failed"}

        def close(self):
            pass

    monkeypatch.setattr(action, "_ensure_local_cases_schema", lambda: None)
    monkeypatch.setattr(action, "_load_config", lambda: {})
    monkeypatch.setattr(action, "_get_credentials", lambda cfg: {"username": "u", "password": "p", "download_folder": "/tmp"})
    monkeypatch.setattr(action, "_get_db_manager", lambda cfg: None)
    monkeypatch.setattr(action, "_ensure_imports", lambda: SimpleNamespace(CourtRecordDownloader=_Downloader))
    monkeypatch.setattr(action, "_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(action, "_mark_notify_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(action, "_eventlog", lambda *args, **kwargs: None)
    monkeypatch.setattr(action, "_safe_flow_step_status", lambda *args, **kwargs: None)

    result = action.cmd_download_all(notify=False)

    assert result["success"] is False
    assert result["error"] == "SSO login failed"
