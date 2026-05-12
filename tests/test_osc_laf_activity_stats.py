from __future__ import annotations

from pathlib import Path

from api.blueprints import osc_cases as mod


def test_laf_review_stats_excludes_payment_only_date_folders(tmp_path: Path):
    review = tmp_path / "06_閱卷資料"
    payment_only = review / "20260501"
    actual_review = review / "20260502"
    payment_only.mkdir(parents=True)
    actual_review.mkdir(parents=True)
    (payment_only / "20260501 規費繳款單.pdf").write_bytes(b"payment")
    (actual_review / "卷證_P1-20_OCR.pdf").write_bytes(b"ocr")

    result = mod._laf_collect_review_dates_from_folder(str(tmp_path))

    assert result["count"] == 1
    assert result["dates"] == ["2026-05-02"]
    assert result["items"][0]["files"][0]["file_name"] == "卷證_P1-20_OCR.pdf"
    assert result["skipped_payment_only"] == ["20260501"]


def test_laf_activity_stats_uses_osc_exclusions_and_time_dedup(monkeypatch):
    monkeypatch.setattr(mod, "_osc_exec", lambda *args, **kwargs: ({"cnt": 1}, {}))
    case = {"client_name": "張偉銘", "case_reason": "詐欺"}
    todos = [
        {"todo_type": "開庭", "todo_date": "2026-05-01", "todo_time": "09:30", "description": "張偉銘 詐欺 開庭"},
        {"todo_type": "開庭", "todo_date": "2026-05-01", "todo_time": "09:30", "description": "張偉銘 詐欺 準備程序"},
        {"todo_type": "開庭", "todo_date": "2026-05-08", "todo_time": "09:30", "description": "張偉銘 詐欺 聲請改期"},
        {"todo_type": "會議", "todo_date": "2026-05-02", "todo_time": "10:00", "description": "張偉銘 U會議"},
        {"todo_type": "電話聯繫", "todo_date": "2026-05-03", "todo_time": "11:00", "description": "張偉銘 電聯"},
    ]
    meetings = [
        {"type": "律見", "datetime": "2026-05-04 14:00", "location": "看守所", "notes": "張偉銘"},
        {"type": "會議", "datetime": "2026-05-05 15:00", "location": "事務所", "notes": "張偉銘 來所面談"},
    ]
    review = {"count": 2, "dates": ["2026-05-06", "2026-05-02"]}

    stats = mod._laf_build_activity_stats(case, todos, meetings, review)

    assert stats["開庭"]["count"] == 1
    assert stats["會議"]["count"] == 1
    assert stats["律見"]["count"] == 1
    assert stats["電話聯繫"]["count"] == 1
    assert stats["閱卷"]["count"] == 2
    assert stats["閱卷"]["rows"][0]["source"] == "閱卷資料夾"


def test_laf_income_tax_year_pair_switches_in_may():
    assert mod._laf_income_tax_year_pair(mod.date(2026, 4, 30)) == (112, 113)
    assert mod._laf_income_tax_year_pair(mod.date(2026, 5, 1)) == (113, 114)
    assert mod._laf_income_tax_year_pair(mod.date(2027, 5, 1)) == (114, 115)


def test_laf_activity_stats_counts_calendar_meeting_and_excludes_laf_admin(monkeypatch):
    monkeypatch.setattr(mod, "_osc_exec", lambda *args, **kwargs: ({"cnt": 1}, {}))
    case = {"client_name": "陳鏈棠", "case_reason": "更生"}
    todos = [
        {
            "todo_type": "行事曆事件",
            "todo_date": "2026-04-29",
            "todo_time": "17:30:00",
            "description": "陳鏈棠面談＠全家宜蘭縣府店",
            "source_file": "gcal_import",
        },
    ]
    calendar_events = [
        {
            "title": "【法扶開辦末日】2026-0035 陳鏈棠",
            "start_date": "2026-06-09 00:00:00",
        },
        {
            "title": "陳鏈棠面談＠全家宜蘭縣府店",
            "start_date": "2026-04-29 17:30:00",
        },
    ]

    stats = mod._laf_build_activity_stats(case, todos, [], {"dates": []}, calendar_events)

    assert stats["會議"]["count"] == 1
    assert stats["會議"]["rows"][0]["source"] == "Google Calendar"


def test_laf_activity_stats_excludes_judgment_announcement_and_future_events(monkeypatch):
    monkeypatch.setattr(mod, "_osc_exec", lambda *args, **kwargs: ({"cnt": 1}, {}))
    case = {"client_name": "測試人", "case_reason": "民事"}
    todos = [
        {"todo_type": "宣判", "todo_date": "2026-05-01", "todo_time": "10:00", "description": "測試人 宣示判決"},
        {"todo_type": "調查", "todo_date": "2026-05-02", "todo_time": "10:00", "description": "測試人 調查程序"},
        {"todo_type": "電話聯絡", "todo_date": "2026-05-03", "todo_time": "11:00", "description": "測試人 電話聯絡"},
        {"todo_type": "開庭", "todo_date": "2099-01-01", "todo_time": "09:30", "description": "測試人 開庭"},
    ]

    stats = mod._laf_build_activity_stats(case, todos, [], {"dates": []}, [])

    assert stats["開庭"]["count"] == 1
    assert stats["開庭"]["rows"][0]["summary"] == "調查 測試人 調查程序"
    assert stats["電話聯繫"]["count"] == 1
