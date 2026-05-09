# -*- coding: utf-8 -*-
from __future__ import annotations

import re

from api.pipelines import specialized_commands


class _FakeOrch:
    def _strip_intent_prefixes(self, text, patterns):
        out = str(text or "").strip()
        for pattern in patterns:
            out = re.sub(pattern, "", out, flags=re.IGNORECASE).strip()
        return out

    def _translate_text_complete(self, text, source_lang="auto", target_lang="繁體中文", heavy=False):
        return {"success": True, "text": f"{target_lang}::{text}", "provider": "fake"}


def test_inline_translation_strips_target_language_prefix(monkeypatch):
    captured = {}

    def _fake_translate_text(text, target_lang, source_lang, mode):
        captured.update(
            {
                "text": text,
                "target_lang": target_lang,
                "source_lang": source_lang,
                "mode": mode,
            }
        )
        return {"success": True, "text": "法院因證據不足而駁回該聲請。", "provider": "fake"}

    monkeypatch.setattr("skills.bridge.tri_sage_collab.translate_text", _fake_translate_text)

    reply = specialized_commands.run_inline_translation_command(
        _FakeOrch(),
        "tester",
        "請幫我翻譯成繁體中文：The court denied the motion because the evidence was insufficient.",
    )

    assert "法院因證據不足" in reply
    assert captured["text"] == "The court denied the motion because the evidence was insufficient."
    assert captured["target_lang"] == "繁體中文"


def test_inline_translation_capability_question_returns_guide(monkeypatch):
    def _unexpected_translate_text(*args, **kwargs):
        raise AssertionError("capability question should not execute translation")

    monkeypatch.setattr("skills.bridge.tri_sage_collab.translate_text", _unexpected_translate_text)

    reply = specialized_commands.run_inline_translation_command(_FakeOrch(), "tester", "你會翻譯嗎？")

    assert "我可以幫您翻譯" in reply


def test_inline_translation_respects_english_target_prefix(monkeypatch):
    captured = {}

    def _fake_translate_text(text, target_lang, source_lang, mode):
        captured["text"] = text
        captured["target_lang"] = target_lang
        return {"success": True, "text": "Hello.", "provider": "fake"}

    monkeypatch.setattr("skills.bridge.tri_sage_collab.translate_text", _fake_translate_text)

    specialized_commands.run_inline_translation_command(_FakeOrch(), "tester", "翻譯成英文：你好。")

    assert captured["text"] == "你好。"
    assert captured["target_lang"] == "英文"


def test_inline_summary_strips_polite_prefix_and_colon():
    class _SummaryOrch(_FakeOrch):
        def _detect_summary_length(self, message):
            return "medium"

        def _summarize_text_resilient(self, text, summary_length="medium"):
            assert text == "第一，系統需要每日更新新聞。第二，摘要必須輸出繁體中文。"
            return {
                "success": True,
                "text": "- 系統需要每日更新新聞。\n- 摘要必須輸出繁體中文。",
                "provider": "fake",
            }

    reply = specialized_commands.run_inline_summary_command(
        _SummaryOrch(),
        "請幫我摘要：第一，系統需要每日更新新聞。第二，摘要必須輸出繁體中文。",
    )

    assert "每日更新新聞" in reply
    assert "繁體中文" in reply


def test_inline_summary_capability_question_returns_guide():
    class _SummaryOrch(_FakeOrch):
        def _detect_summary_length(self, message):
            raise AssertionError("capability question should not execute summary")

    reply = specialized_commands.run_inline_summary_command(_SummaryOrch(), "你會摘要嗎？")

    assert "我可以幫您做摘要" in reply


def test_inline_summary_falls_back_when_model_requests_content():
    class _SummaryOrch(_FakeOrch):
        def _detect_summary_length(self, message):
            return "medium"

        def _summarize_text_resilient(self, text, summary_length="medium"):
            return {
                "success": True,
                "text": "請提供需要我進行分析的原始檔案內容。",
                "provider": "fake",
            }

    reply = specialized_commands.run_inline_summary_command(
        _SummaryOrch(),
        "請幫我摘要：第一，系統需要每日更新新聞。第二，摘要必須輸出繁體中文。",
    )

    assert "extractive_inline" in reply
    assert "- 系統需要每日更新新聞" in reply
    assert "- 摘要必須輸出繁體中文" in reply
