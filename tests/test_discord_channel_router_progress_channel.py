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
        "333",
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
        "555",
    )


def test_laf_progress_confirmation_still_routes_to_progress_channel():
    msg = "未結案件進度回報：請確認送出，confirm_token=ABC123"

    assert router._infer_sub_topic(msg, "laf", "laf_progress_helper") == "laf_progress"


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

    monkeypatch.setattr(router, "_load_channel_map", lambda: {"laf_condition": "111", "general": "999"})

    assert router.resolve_discord_channel(
        msg,
        topic_key="laf_condition",
        source="business_module_live_check",
        fallback_channel_id="999",
    ) == ("check", "__SILENT__")
