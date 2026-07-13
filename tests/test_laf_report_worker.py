import json

from scripts.ops import laf_report_worker as worker
from skills.ops import red_phone


def test_parse_sentinel_result():
    body = {"ok": True, "action": "closing"}
    text = "noise\n===MAGI_RESULT_JSON_START===\n" + json.dumps(body) + "\n===MAGI_RESULT_JSON_END===\nmore"
    assert worker._parse_result(text) == body


def test_format_closing_success_contains_counts_and_draft_policy():
    data = {
        "ok": True,
        "identity": {
            "client_name": "林文忠",
            "laf_case_number": "1141121-E-005",
            "case_number": "2025-0119",
        },
        "counts": {
            "meeting_count": 1,
            "contact_count": 2,
            "inq_count": 0,
            "court_count": 1,
            "review_count": 0,
            "document_count": 2,
            "court_name": "臺灣花蓮地方法院",
            "court_case_year": "115",
            "court_case_code": "調偵",
            "court_case_no": "118",
            "closing_doc_type": "不起訴處分書",
        },
        "upload_bundle": {"pdf_files": ["a.pdf", "b.pdf"]},
    }
    msg = worker._format_success(data, {"action_label": "結案回報"}, "closing")
    assert "林文忠｜1141121-E-005｜2025-0119" in msg
    assert "開會 1／聯繫 2／律見 0／開庭 1／閱卷 0／書狀 2" in msg
    assert "臺灣花蓮地方法院115年度調偵字第118號" in msg
    assert "目前僅暫存，不會代為送出" in msg


def test_format_missing_docs_failure_is_actionable():
    msg = worker._format_failure(
        {
            "ok": False,
            "error": "missing_required_docs",
            "missing": ["結案依據文件"],
        },
        {"action_label": "結案回報"},
        "closing",
    )
    assert "缺少文件：結案依據文件" in msg
    assert "放入對應案件資料夾" in msg


def test_notify_sends_discord_once_and_disables_telegram_mirror(monkeypatch):
    discord_calls = []
    telegram_calls = []

    monkeypatch.setattr(
        red_phone,
        "_send_discord_bot_message",
        lambda *args, **kwargs: discord_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        red_phone,
        "send_telegram_push_with_status",
        lambda *args, **kwargs: telegram_calls.append((args, kwargs)) or {"telegram": True},
    )

    result = worker._notify("法扶結案已完成存檔", topic_key="laf_closing")

    assert result["discord_text"] is True
    assert result["telegram_text"] is True
    assert len(discord_calls) == 1
    assert len(telegram_calls) == 1
    assert telegram_calls[0][1]["mirror_to_discord"] is False
