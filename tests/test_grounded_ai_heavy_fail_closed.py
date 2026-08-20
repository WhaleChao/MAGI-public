from __future__ import annotations

from skills.bridge import grounded_ai
from skills.bridge import nim_heavy


def test_heavy_chat_does_not_silently_fall_back_when_nim_fails(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "1")
    monkeypatch.delenv("MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK", raising=False)
    monkeypatch.setattr(
        nim_heavy,
        "run_nim_chat",
        lambda **_kwargs: {"success": False, "error": "daily_budget_exceeded"},
    )
    monkeypatch.setattr(
        grounded_ai,
        "_classify_query_tier",
        lambda _message: (_ for _ in ()).throw(AssertionError("must not reach local fallback")),
    )

    reply = grounded_ai.chat_casper("@heavy 請分析本案證據")

    assert "NVIDIA 重型服務目前無法完成" in reply
    assert "沒有改用較弱模型" in reply


def test_heavy_chat_does_not_silently_fall_back_when_nim_disabled(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_ENABLE", "0")
    monkeypatch.delenv("MAGI_HEAVY_STRICT_NIM_ALLOW_FALLBACK", raising=False)
    monkeypatch.setattr(
        grounded_ai,
        "_classify_query_tier",
        lambda _message: (_ for _ in ()).throw(AssertionError("must not reach local fallback")),
    )

    reply = grounded_ai.chat_casper("@重型 請整理長篇判決")

    assert "NVIDIA 重型服務目前尚未啟用" in reply
    assert "沒有改用較弱模型" in reply
