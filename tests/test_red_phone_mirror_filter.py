from __future__ import annotations

import json
import sys
import types

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


def test_discord_mirror_blocks_laf_general_audit_report(monkeypatch):
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

    assert red_phone._mirror_to_discord(msg, topic_key="laf_general", source="laf_nightly_audit") is False
    assert sent == {}


def test_discord_mirror_allows_laf_progress_reminder(monkeypatch):
    sent = {}

    def fake_send(message, severity, *, topic_key="", source=""):
        sent["message"] = message
        sent["severity"] = severity
        sent["topic_key"] = topic_key
        sent["source"] = source
        return True

    monkeypatch.setenv("MAGI_DC_MIRROR_ENABLED", "1")
    monkeypatch.setattr(red_phone, "_send_discord_bot_message", fake_send)

    msg = "📣 法扶進度回報提醒\n⚠️ 進行中逾 18 個月，需確認進度回報：1 件"

    assert red_phone._mirror_to_discord(msg, topic_key="laf_progress", source="laf_progress_reminder") is True
    assert sent["topic_key"] == "laf_progress"


def test_system_sources_infer_non_business_topics():
    assert red_phone._infer_topic_key("法扶 二階段 健康檢查", "business_module_live_check", "warning") == "check"
    assert red_phone._infer_topic_key("摘要 訓練完成", "weekend_resummary", "info") == "nightly"
    assert red_phone._canonical_topic_key("self_repair") == "alert"
    assert red_phone._canonical_topic_key("quiet_cron") == "check"


def test_laf_reports_and_actions_infer_separate_topics(monkeypatch):
    assert red_phone._infer_topic_key("📋 法扶夜間巡檢報告\n📊 法扶案件總數：125", "laf_nightly_audit", "warning") == "laf_general"
    assert red_phone._infer_topic_key("📥 新法扶派案已建立\n案號: 1150505-W-002", "laf_monitor", "info") == "laf_dispatch"
    assert red_phone._infer_topic_key("❌ 開辦預填失敗 — 1150421-E-016", "laf_orchestrator", "warning") == "laf_go_live"
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


def test_unknown_business_topic_does_not_use_telegram_general_thread(monkeypatch):
    monkeypatch.setattr(red_phone, "_load_topic_map", lambda: {"general": 999})

    topic, thread_id = red_phone._resolve_thread_id(
        "未知法扶業務通知",
        "unit",
        "warning",
        topic_key="laf_unknown",
    )

    assert topic == "laf_unknown"
    assert thread_id is None


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


def test_outbox_deduplicates_same_pending_message(tmp_path, monkeypatch):
    outbox_path = tmp_path / "outbox.json"
    monkeypatch.setattr(red_phone, "RED_PHONE_OUTBOX_FILE", str(outbox_path))
    message = "📧 法扶派案通知\n分會: 花蓮\n當事人: 王惠薰\n法扶案號: 1150529-E-005"

    first_id = red_phone._enqueue_outbox(message, severity="info", source="laf_notifier", topic_key="laf")
    second_id = red_phone._enqueue_outbox(message, severity="info", source="laf_notifier", topic_key="laf")

    data = __import__("json").loads(outbox_path.read_text("utf-8"))
    assert second_id == first_id
    assert len(data) == 1
    assert data[0]["topic_key"] == "laf_dispatch"
    assert data[0]["fingerprint"]


def test_outbox_flush_drops_stale_info_without_resending(tmp_path, monkeypatch):
    outbox_path = tmp_path / "outbox.json"
    delivery_path = tmp_path / "delivery.jsonl"
    monkeypatch.setattr(red_phone, "RED_PHONE_OUTBOX_FILE", str(outbox_path))
    monkeypatch.setattr(red_phone, "RED_PHONE_DELIVERY_LOG", str(delivery_path))
    monkeypatch.setattr(red_phone, "RED_PHONE_OUTBOX_INFO_MAX_AGE_SEC", 60.0)

    old_entry = {
        "id": "old_laf_dispatch",
        "created_at": "2026-06-23T10:20:25",
        "updated_at": "2026-06-23T10:20:25",
        "severity": "info",
        "source": "laf_notifier",
        "topic_key": "laf_dispatch",
        "message": "📧 法扶派案通知\n當事人: 王惠薰\n法扶案號: 1150529-E-005",
        "attempts": 0,
        "next_retry_at": 0,
        "last_error": "temporary",
    }
    outbox_path.write_text(__import__("json").dumps([old_entry], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(red_phone.time, "time", lambda: __import__("datetime").datetime(2026, 6, 23, 23, 24, 0).timestamp())
    calls = []
    monkeypatch.setattr(red_phone, "send_telegram_push_with_status", lambda *a, **k: calls.append((a, k)) or {"telegram": True})

    result = red_phone._flush_outbox(max_items=8)

    assert result["checked"] == 0
    assert result["recovered"] == 0
    assert result["remaining"] == 0
    assert calls == []
    assert __import__("json").loads(outbox_path.read_text("utf-8")) == []


def test_alert_admin_dedup_does_not_report_fake_delivery(tmp_path, monkeypatch):
    delivery_path = tmp_path / "delivery.jsonl"
    outbox_path = tmp_path / "outbox.json"
    monkeypatch.setattr(red_phone, "RED_PHONE_DELIVERY_LOG", str(delivery_path))
    monkeypatch.setattr(red_phone, "RED_PHONE_OUTBOX_FILE", str(outbox_path))
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(
            is_done=lambda category, key: True,
            mark_done=lambda *args, **kwargs: None,
        ),
    )

    result = red_phone.alert_admin("同一則警報", severity="info", source="unit", topic_key="check")

    assert result["deduplicated"] is True
    assert result["telegram"] is False
    assert result["line"] is False
    assert result["discord"] is False
    assert result["delivered"] is False
    entries = [json.loads(line) for line in delivery_path.read_text(encoding="utf-8").splitlines()]
    assert entries[-1]["event"] == "deduplicated"


def test_send_telegram_push_dedup_returns_false(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "skills.ops.dedup_db",
        types.SimpleNamespace(
            is_done=lambda category, key: True,
            mark_done=lambda *args, **kwargs: None,
        ),
    )

    assert red_phone.send_telegram_push("同一則警報") is False


def test_discord_silent_route_is_not_delivery_success(monkeypatch):
    from api import discord_channel_router as router

    monkeypatch.setattr(red_phone, "DISCORD_BOT_TOKEN", "token")
    monkeypatch.setattr(red_phone, "_get_discord_channel_id_fallback", lambda: "123")
    monkeypatch.setattr(router, "resolve_discord_channel", lambda *args, **kwargs: ("laf_unknown", "__SILENT__"))

    assert red_phone._send_discord_bot_message("未知法扶業務通知", "warning", topic_key="laf_unknown") is False
