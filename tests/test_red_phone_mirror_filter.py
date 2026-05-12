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


def test_discord_mirror_blocks_system_health_even_when_message_mentions_laf(monkeypatch):
    calls = []

    def fake_send(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setenv("MAGI_DC_MIRROR_ENABLED", "1")
    monkeypatch.setattr(red_phone, "_send_discord_bot_message", fake_send)

    msg = "📋 業務三模組 LIVE/健康檢查\n✅ laf_portal_live: 案件狀態暫存 0 / 二階段 0"

    assert red_phone._mirror_to_discord(msg, source="business_module_live_check") is False
    assert calls == []


def test_discord_mirror_blocks_system_health_even_with_business_topic(monkeypatch):
    calls = []

    def fake_send(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setenv("MAGI_DC_MIRROR_ENABLED", "1")
    monkeypatch.setattr(red_phone, "_send_discord_bot_message", fake_send)

    msg = "📋 業務三模組 LIVE/健康檢查\n✅ laf_portal_live: 0 / 二階段 0"

    assert red_phone._mirror_to_discord(msg, topic_key="laf_condition", source="business_module_live_check") is False
    assert calls == []


def test_discord_mirror_allows_laf_general_audit_report(monkeypatch):
    sent = {}

    def fake_send(message, severity, *, topic_key="", source=""):
        sent["message"] = message
        sent["severity"] = severity
        sent["topic_key"] = topic_key
        sent["source"] = source
        return True

    monkeypatch.setenv("MAGI_DC_MIRROR_ENABLED", "1")
    monkeypatch.setattr(red_phone, "_send_discord_bot_message", fake_send)

    msg = "📋 法扶夜間巡檢報告\n⚠️ 進行中逾 18 個月，需確認進度回報：13 件"

    assert red_phone._mirror_to_discord(msg, topic_key="laf_general", source="laf_nightly_audit") is True
    assert sent["topic_key"] == "laf_general"


def test_system_sources_infer_non_business_topics():
    assert red_phone._infer_topic_key("法扶 二階段 健康檢查", "business_module_live_check", "warning") == "check"
    assert red_phone._infer_topic_key("摘要 訓練完成", "weekend_resummary", "info") == "nightly"
    assert red_phone._canonical_topic_key("self_repair") == "alert"
    assert red_phone._canonical_topic_key("quiet_cron") == "check"


def test_laf_reports_and_actions_infer_separate_topics(monkeypatch):
    assert red_phone._infer_topic_key("📋 法扶夜間巡檢報告\n📊 法扶案件總數：125", "laf_nightly_audit", "warning") == "laf_general"
    assert red_phone._infer_topic_key("📥 新法扶派案已建立\n案號: 1150505-W-002", "laf_monitor", "info") == "laf_dispatch"
    assert red_phone._infer_topic_key("❌ go_live 暫存失敗 — 1150421-E-016", "laf_orchestrator", "warning") == "laf_go_live"
    assert red_phone._infer_topic_key("法扶二階段回報待確認：附條件審查需補資料", "laf_orchestrator", "warning") == "laf_condition"
    assert red_phone._infer_topic_key("未結案件進度回報：請確認送出 confirm_token=ABC123", "laf_orchestrator", "warning") == "laf_progress"


def test_laf_general_telegram_fallback_does_not_use_laf_business_topic(monkeypatch):
    monkeypatch.setattr(red_phone, "_load_topic_map", lambda: {"laf": 111, "general": 999})

    topic, thread_id = red_phone._resolve_thread_id(
        "📋 法扶夜間巡檢報告\n📊 法扶案件總數：125",
        "laf_nightly_audit",
        "warning",
        topic_key="laf_general",
    )

    assert topic == "laf_general"
    assert thread_id == 999


def test_outbox_preserves_topic_key(tmp_path, monkeypatch):
    outbox_path = tmp_path / "outbox.json"
    monkeypatch.setattr(red_phone, "RED_PHONE_OUTBOX_FILE", str(outbox_path))

    entry_id = red_phone._enqueue_outbox(
        "法扶 二階段 健康檢查",
        severity="warning",
        source="business_module_live_check",
        topic_key="check",
    )

    data = __import__("json").loads(outbox_path.read_text("utf-8"))
    assert data[0]["id"] == entry_id
    assert data[0]["topic_key"] == "check"
