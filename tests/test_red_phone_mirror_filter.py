from __future__ import annotations

from skills.ops import red_phone


def test_discord_mirror_keeps_zero_count_warning(monkeypatch):
    sent = {}

    def fake_send(message, severity, *, topic_key="", source=""):
        sent["message"] = message
        sent["severity"] = severity
        sent["topic_key"] = topic_key
        return True

    monkeypatch.setenv("MAGI_DC_MIRROR_ENABLED", "1")
    monkeypatch.setattr(red_phone, "_send_discord_bot_message", fake_send)

    msg = "\n".join(
        [
            "📮 閱卷通知檢查完成",
            "- 可下載通知：0 封（待下載佇列 0 件）",
            "- ⚠️ 入口列表探測失敗：navigate_failed / popup_timeout",
        ]
    )

    assert red_phone._mirror_to_discord(msg, topic_key="filereview", source="test") is True
    assert sent["message"] == msg
