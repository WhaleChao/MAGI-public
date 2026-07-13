from datetime import datetime

from skills.ops import red_phone


def test_alert_timestamp_uses_taiwan_timezone(monkeypatch):
    fixed = datetime(2026, 7, 13, 14, 20, 52, tzinfo=red_phone._TAIPEI_TIMEZONE)
    monkeypatch.setattr(red_phone, "_taipei_now", lambda: fixed)

    assert red_phone._alert_timestamp() == "2026-07-13 14:20:52（台灣時間）"
