from __future__ import annotations

import asyncio
from types import SimpleNamespace

from api import discord_channel_router as router


class _FakeGuild:
    def __init__(self, text_channels=None, categories=None):
        self.text_channels = list(text_channels or [])
        self.categories = list(categories or [])
        self.created_channels = []

    async def create_category(self, name: str):
        cat = SimpleNamespace(name=name)
        self.categories.append(cat)
        return cat

    async def create_text_channel(self, *, name: str, category, topic: str):
        ch = SimpleNamespace(name=name, id=9001, category=category, topic=topic)
        self.text_channels.append(ch)
        self.created_channels.append(ch)
        return ch


def test_progress_channel_definition_uses_new_name_and_keeps_legacy_alias():
    progress_def = next(ch for ch in router.DEFAULT_CHANNELS if ch["key"] == "laf_progress")
    assert progress_def["name"] == "法扶-進度回報"
    assert "🔄 進度回報" in progress_def.get("aliases", [])


def test_auto_setup_reuses_legacy_progress_channel_name(monkeypatch):
    legacy_channel = SimpleNamespace(name="🔄 進度回報", id=123456)
    guild = _FakeGuild(text_channels=[legacy_channel])

    saved = {}

    def _fake_save_channel_map(channel_map):
        saved.update(channel_map)
        return "/tmp/discord_channel_map.json"

    monkeypatch.setattr(router, "save_channel_map", _fake_save_channel_map)

    result = asyncio.run(router.auto_setup_channels(guild))

    assert result["laf_progress"] == "123456"
    assert not any(ch.name == "法扶-進度回報" for ch in guild.created_channels)


def test_laf_nightly_audit_report_routes_to_laf_general_before_progress_keyword(monkeypatch):
    msg = "\n".join(
        [
            "📋 法扶夜間巡檢報告",
            "日期：2026-05-12",
            "📊 法扶案件總數：125",
            "✅ 自動補填法扶案號：82 件",
            "⚠️ 進行中逾 18 個月，需確認進度回報：13 件",
        ]
    )

    assert router._infer_sub_topic(msg, "laf", "laf_nightly_audit") == "laf_general"

    monkeypatch.setattr(
        router,
        "_load_channel_map",
        lambda: {
            "laf_dispatch": "111",
            "laf_progress": "222",
            "laf_general": "333",
            "laf": "444",
            "general": "555",
        },
    )

    assert router.resolve_discord_channel(msg, topic_key="laf", source="laf_nightly_audit") == (
        "laf_general",
        "__SILENT__",
    )


def test_laf_general_discord_fallback_does_not_use_laf_business_channel(monkeypatch):
    msg = "📋 法扶夜間巡檢報告\n📊 法扶案件總數：125"

    monkeypatch.setattr(
        router,
        "_load_channel_map",
        lambda: {
            "laf": "444",
            "general": "555",
        },
    )

    assert router.resolve_discord_channel(msg, topic_key="laf_general", source="laf_nightly_audit") == (
        "laf_general",
        "__SILENT__",
    )


def test_laf_progress_confirmation_still_routes_to_progress_channel():
    msg = "未結案件進度回報：請確認送出，confirm_token=ABC123"

    assert router._infer_sub_topic(msg, "laf", "laf_progress_helper") == "laf_progress"


def test_laf_progress_reminder_routes_to_progress_channel(monkeypatch):
    msg = "📣 法扶進度回報提醒\n⚠️ 進行中逾 18 個月，需確認進度回報：1 件"

    monkeypatch.setattr(
        router,
        "_load_channel_map",
        lambda: {
            "laf_progress": "222",
            "laf_general": "333",
            "general": "555",
        },
    )

    assert router.resolve_discord_channel(msg, topic_key="laf_progress", source="laf_progress_reminder") == (
        "laf_progress",
        "222",
    )


def test_laf_routing_matrix_keeps_business_notifications_separated():
    samples = [
        ("📥 新法扶派案已建立\n案號: 1150505-W-002", "laf_dispatch"),
        ("[INFO] ❌ 開辦預填失敗 — 1150421-E-016\n原因：portal go_live prefill failed", "laf_go_live"),
        ("法扶二階段回報待確認：附條件審查需補資料", "laf_condition"),
        ("法扶費用通知：酬金領款單已下載", "laf_fee"),
        ("法扶疑義回報：資力不合標準待處理", "laf_inquiry"),
        ("法扶結案通知：結案審查通知書已下載", "laf_closing"),
        ("未結案件進度回報：請確認送出，confirm_token=ABC123", "laf_progress"),
        ("📋 法扶夜間巡檢報告\n📊 法扶案件總數：125\n⚠️ 進行中逾 18 個月，需確認進度回報：13 件", "laf_general"),
    ]

    for msg, expected in samples:
        assert router._infer_sub_topic(msg, "laf", "routing_matrix") == expected


def test_system_health_source_overrides_accidental_business_topic(monkeypatch):
    msg = "📋 業務三模組 LIVE/健康檢查\n✅ laf_portal_live: 案件狀態暫存 0 / 二階段 0"

    assert router._infer_sub_topic(msg, "laf_condition", "business_module_live_check") == "check"

    monkeypatch.setattr(router, "_load_notification_preferences", lambda: {"live_check": "system_only", "system_health": "system_only"})
    monkeypatch.setattr(router, "_load_channel_map", lambda: {"laf_condition": "111", "general": "999"})

    assert router.resolve_discord_channel(
        msg,
        topic_key="laf_condition",
        source="business_module_live_check",
        fallback_channel_id="999",
    ) == ("check", "__SILENT__")

    monkeypatch.setattr(router, "_load_channel_map", lambda: {"check": "222", "laf_condition": "111", "general": "999"})

    assert router.resolve_discord_channel(
        msg,
        topic_key="laf_condition",
        source="business_module_live_check",
        fallback_channel_id="999",
    ) == ("check", "222")


def test_system_only_notification_pref_requires_explicit_system_channel(monkeypatch):
    msg = "☀️ 白天整理完成：處理 200、摘要 80"

    monkeypatch.setattr(router, "_load_notification_preferences", lambda: {"nightly_report": "system_only"})
    monkeypatch.setattr(router, "_load_channel_map", lambda: {"general": "999"})

    assert router.resolve_discord_channel(
        msg,
        topic_key="nightly",
        source="job_judicial_api_noon",
        fallback_channel_id="999",
    ) == ("nightly", "__SILENT__")

    monkeypatch.setattr(router, "_load_channel_map", lambda: {"nightly": "777", "general": "999"})

    assert router.resolve_discord_channel(
        msg,
        topic_key="nightly",
        source="job_judicial_api_noon",
        fallback_channel_id="999",
    ) == ("nightly", "777")


def test_judicial_api_and_resummary_never_fall_back_to_business_or_general(monkeypatch):
    monkeypatch.setattr(router, "_load_notification_preferences", lambda: {"nightly_report": "system_only"})
    monkeypatch.setattr(
        router,
        "_load_channel_map",
        lambda: {
            "judgment": "444",
            "general": "999",
        },
    )

    for topic_key, source in [
        ("judicial_api", "judgment_collector"),
        ("judgment_resummary", "weekend_resummary"),
    ]:
        assert router.resolve_discord_channel(
            "☀️ 白天整理完成：處理 200、摘要 80。尚有 raw backlog 68999 份待消化。",
            topic_key=topic_key,
            source=source,
            fallback_channel_id="999",
        ) == ("nightly", "__SILENT__")

    monkeypatch.setattr(
        router,
        "_load_channel_map",
        lambda: {
            "nightly": "777",
            "judgment": "444",
            "general": "999",
        },
    )

    assert router.resolve_discord_channel(
        "⚠️ 司法院 API 晨間整理仍有 backlog",
        topic_key="judicial_api",
        source="judgment_collector",
        fallback_channel_id="999",
    ) == ("nightly", "777")


def test_quiet_cron_and_self_repair_use_system_policies(monkeypatch):
    monkeypatch.setattr(router, "_load_notification_preferences", lambda: {"system_health": "system_only"})
    monkeypatch.setattr(router, "_load_channel_map", lambda: {"general": "999"})

    assert router.resolve_discord_channel(
        "信箱檢查完成，沒有新資料",
        topic_key="quiet_cron",
        source="file_review_orchestrator",
        fallback_channel_id="999",
    ) == ("check", "__SILENT__")

    monkeypatch.setattr(router, "_load_channel_map", lambda: {"alert": "888", "general": "999"})

    assert router.resolve_discord_channel(
        "MAGI 自我修復週報",
        topic_key="self_repair",
        source="self_repair_weekly",
        fallback_channel_id="999",
    ) == ("alert", "888")


def test_laf_nightly_general_report_stays_silent_even_with_explicit_channel(monkeypatch):
    msg = "📋 法扶夜間巡檢報告\n📊 法扶案件總數：125"

    monkeypatch.setattr(router, "_load_notification_preferences", lambda: {"laf_general": "system_only"})
    monkeypatch.setattr(router, "_load_channel_map", lambda: {"general": "999", "laf_dispatch": "111"})

    assert router.resolve_discord_channel(
        msg,
        topic_key="laf",
        source="laf_nightly_audit",
        fallback_channel_id="999",
    ) == ("laf_general", "__SILENT__")

    monkeypatch.setattr(router, "_load_channel_map", lambda: {"laf_general": "333", "general": "999"})

    assert router.resolve_discord_channel(
        msg,
        topic_key="laf",
        source="laf_nightly_audit",
        fallback_channel_id="999",
    ) == ("laf_general", "__SILENT__")


def test_business_notification_preference_can_silence_non_system_topics(monkeypatch):
    msg = "📥 卷宗下載完成（2 個檔案）"

    monkeypatch.setattr(router, "_load_notification_preferences", lambda: {"business": "silent"})
    monkeypatch.setattr(router, "_load_channel_map", lambda: {"filereview_download": "111", "general": "999"})

    assert router.resolve_discord_channel(
        msg,
        topic_key="filereview",
        source="file_review_orchestrator",
        fallback_channel_id="999",
    ) == ("filereview_download", "__SILENT__")
