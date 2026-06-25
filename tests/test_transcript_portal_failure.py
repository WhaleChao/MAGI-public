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
