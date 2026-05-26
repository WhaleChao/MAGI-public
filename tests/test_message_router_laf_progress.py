from __future__ import annotations

import sys
import types

from api.pipelines import message_router


class _DummyOrch:
    _TOPIC_HANDLERS = {}

    def _handle_command(self, user_id, message, role=None, platform=None):
        return {
            "user_id": user_id,
            "message": message,
            "role": role,
            "platform": platform,
        }


def test_laf_progress_channel_redirects_non_progress_laf_commands():
    orch = _DummyOrch()

    out = message_router.topic_fast_path(
        orch,
        "laf_progress",
        "user-1",
        "張偉銘 結案回報",
        "user",
        "discord",
    )

    assert "法扶-結案" in out
    assert "法扶-進度回報" in out


def test_laf_progress_channel_accepts_reported_without_angle_brackets(monkeypatch):
    calls = []

    def fake_mark(target, actor=None, note=None):
        calls.append((target, actor, note))
        return {
            "ok": True,
            "case": {"client_name": "謝依穎", "laf_case_number": target},
            "cooldown_until": "2026-07-16",
            "calendar": {"todo_id": "todo-1"},
        }

    monkeypatch.setitem(sys.modules, "laf_nightly_audit", types.SimpleNamespace(mark_progress_reported=fake_mark))

    out = message_router.topic_fast_path(
        _DummyOrch(),
        "laf_progress",
        "user-1",
        "1131122-E-017 謝依穎已回報",
        "user",
        "discord",
    )

    assert calls[0][0] == "1131122-E-017"
    assert "進度回報提醒冷卻" in out
    assert "行事曆" in out


def test_laf_progress_target_extracts_name_without_angle_brackets():
    assert message_router.extract_laf_progress_reported_target("謝依穎已回報") == "謝依穎"
    assert message_router.extract_laf_progress_reported_target("進度已回報 謝依穎") == "謝依穎"


def test_laf_progress_channel_does_not_fall_through_to_chat():
    out = message_router.topic_fast_path(
        _DummyOrch(),
        "laf_progress",
        "user-1",
        "謝依穎",
        "user",
        "discord",
    )

    assert "法扶進度回報" in out
    assert "Gemma" not in out


def test_transcript_channel_does_not_fall_through_to_chat():
    out = message_router.topic_fast_path(
        _DummyOrch(),
        "transcript",
        "user-1",
        "這件下載一下",
        "user",
        "discord",
    )

    assert "筆錄同步" in out
    assert "Gemma" not in out


def test_document_business_channels_do_not_fall_through_to_chat():
    samples = [
        ("translation", "這份怎麼弄", "翻譯"),
        ("summary", "這份怎麼弄", "摘要"),
        ("verbatim", "這份怎麼弄", "逐字稿"),
        ("filing", "這份怎麼弄", "PDF"),
        ("judgment", "這份怎麼弄", "裁判"),
        ("research_interpretation", "這份怎麼弄", "研究"),
    ]

    for topic, message, expected in samples:
        out = message_router.topic_fast_path(
            _DummyOrch(),
            topic,
            "user-1",
            message,
            "user",
            "discord",
        )
        assert expected in out
        assert "Gemma" not in out
        assert "您好" not in out


def test_document_business_channels_allow_explicit_commands_to_continue():
    cases = [
        ("translation", "翻譯 Hello"),
        ("summary", "摘要 這是一段文字"),
        ("judgment", "查判決 通譯"),
        ("research_interpretation", "研究摘要 通譯"),
        ("filing", "PDF 建立待辦"),
    ]

    for topic, message in cases:
        assert (
            message_router.topic_fast_path(
                _DummyOrch(),
                topic,
                "user-1",
                message,
                "user",
                "discord",
            )
            is None
        )
