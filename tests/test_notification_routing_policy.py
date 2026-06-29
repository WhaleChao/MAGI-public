from __future__ import annotations

from api import discord_channel_router as router
from skills.ops import red_phone


def test_zero_file_review_completion_is_not_mirrored_to_discord(monkeypatch):
    calls = []

    def fake_send(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setenv("MAGI_DC_MIRROR_ENABLED", "1")
    monkeypatch.setattr(red_phone, "_send_discord_bot_message", fake_send)

    msg = "\n".join(
        [
            "💰 繳費單檢查完成",
            "- 繳費相關信件：0 封（已通知 0 封）",
            "- 入口列表待繳費：0 件",
        ]
    )

    assert red_phone._mirror_to_discord(
        msg,
        topic_key="filereview_payment",
        source="file_review_orchestrator",
    ) is False
    assert calls == []


def test_zero_downloadable_probe_with_skipped_rows_is_not_mirrored(monkeypatch):
    calls = []
    monkeypatch.setenv("MAGI_DC_MIRROR_ENABLED", "1")
    monkeypatch.setattr(red_phone, "_send_discord_bot_message", lambda *a, **k: calls.append((a, k)) or True)

    msg = (
        "📮 閱卷可下載判定：法院端狀態掃描完成（入口列表）："
        "法院端可下載 0 件（已歸檔/已下載略過 6 件），"
        "近期需到院閱卷 0 件，待繳費 0 件，"
        "同案合併後共 0 案（原始 12 列）；Gmail 通知 0 封（可下載型 0 封）"
    )

    assert red_phone._mirror_to_discord(msg, topic_key="filereview", source="file_review_orchestrator") is False
    assert calls == []


def test_file_review_specific_topics_do_not_fall_back_to_telegram_general(monkeypatch):
    monkeypatch.setattr(red_phone, "_load_topic_map", lambda: {"general": 999})

    topic, thread_id = red_phone._resolve_thread_id(
        "💰 繳費單通知\n當事人: 凡江\n案號: 115年度原交易字第21號",
        "file_review_orchestrator",
        "info",
        topic_key="filereview",
    )
    assert topic == "filereview_payment"
    assert thread_id is None

    topic, thread_id = red_phone._resolve_thread_id(
        "📥 卷宗下載完成（2 個檔案）\n凡江｜115年度原交易字第21號",
        "file_review_orchestrator",
        "info",
        topic_key="filereview",
    )
    assert topic == "filereview_download"
    assert thread_id is None


def test_file_review_specific_topics_can_fall_back_to_parent_topic(monkeypatch):
    monkeypatch.setattr(red_phone, "_load_topic_map", lambda: {"filereview": 222, "general": 999})

    topic, thread_id = red_phone._resolve_thread_id(
        "📥 卷宗下載完成（2 個檔案）",
        "file_review_orchestrator",
        "info",
        topic_key="filereview",
    )

    assert topic == "filereview_download"
    assert thread_id == 222


def test_download_signal_with_zero_payment_column_routes_to_download_channel(monkeypatch):
    monkeypatch.setattr(
        router,
        "_load_channel_map",
        lambda: {
            "filereview_payment": "111",
            "filereview_download": "222",
            "general": "999",
        },
    )

    msg = (
        "法院端狀態掃描完成（入口列表）：法院端可下載 2 件，"
        "近期需到院閱卷 0 件，待繳費 0 件，同案合併後共 2 案"
    )

    assert router._infer_sub_topic(msg, "filereview", "file_review_orchestrator") == "filereview_download"
    assert router.resolve_discord_channel(
        msg,
        topic_key="filereview",
        source="file_review_orchestrator",
        fallback_channel_id="999",
    ) == ("filereview_download", "222")


def test_router_silences_zero_completion_even_with_general_fallback(monkeypatch):
    monkeypatch.setattr(router, "_load_channel_map", lambda: {"filereview_payment": "111", "general": "999"})

    msg = "💰 繳費單檢查完成\n- 繳費相關信件：0 封（已通知 0 封）\n- 入口列表待繳費：0 件"

    assert router.resolve_discord_channel(
        msg,
        topic_key="filereview_payment",
        source="file_review_orchestrator",
        fallback_channel_id="999",
    ) == ("filereview_payment", "__SILENT__")


def test_laf_dispatch_dedup_classification_is_stable_for_parent_topic():
    msg = "📧 法扶派案通知\n分會: 花蓮\n當事人: 王惠薰\n法扶案號: 1150529-E-005"

    parent = red_phone.classify_notification_event(
        msg,
        source="laf_notifier",
        severity="info",
        topic_key="laf",
    )
    specific = red_phone.classify_notification_event(
        msg,
        source="laf_notifier",
        severity="info",
        topic_key="laf_dispatch",
    )

    assert parent["topic_key"] == "laf_dispatch"
    assert parent["source_class"] == "laf"
    assert specific["topic_key"] == "laf_dispatch"
    assert parent["dedup_key"] == specific["dedup_key"]
