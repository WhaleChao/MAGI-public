"""Tests for InferenceGateway — routing decisions and fallback chains."""

import pytest
from unittest.mock import patch, MagicMock


def test_classify_intent_explicit_override():
    """Explicit task_type should override keyword detection."""
    from skills.bridge.inference_gateway import classify_intent
    assert classify_intent("翻譯這段文字", explicit_task_type="summary") == "summary"


def test_classify_intent_keyword_match():
    """Keywords should trigger correct task type."""
    from skills.bridge.inference_gateway import classify_intent
    assert classify_intent("請幫我翻譯這篇文章") == "translate"
    assert classify_intent("請摘要這份判決") == "summary"
    assert classify_intent("校正為繁體中文") == "tc_review"


def test_classify_intent_vision_with_image():
    """Image path should trigger vision task type."""
    from skills.bridge.inference_gateway import classify_intent
    assert classify_intent("這是什麼", image_path="/tmp/test.jpg") == "vision"


def test_classify_intent_captcha_with_image():
    """Captcha keywords with image should trigger captcha."""
    from skills.bridge.inference_gateway import classify_intent
    assert classify_intent("讀取驗證碼", image_path="/tmp/cap.png") == "captcha"


def test_classify_intent_long_text_summary():
    """Long text without keywords should default to summary."""
    from skills.bridge.inference_gateway import classify_intent
    long_text = "這是一段很長的文字。" * 300
    assert classify_intent(long_text) == "summary"


def test_classify_intent_default_general():
    """Short generic text should return general."""
    from skills.bridge.inference_gateway import classify_intent
    assert classify_intent("你好") == "general"


def test_gateway_chat_returns_error_on_empty_prompt():
    """Empty prompt should return error immediately."""
    from skills.bridge.inference_gateway import InferenceGateway
    gw = InferenceGateway()
    result = gw.chat("")
    assert result["success"] is False
    assert "missing_prompt" in result.get("error", "")


def test_gateway_can_disable_synthetic_fallback():
    """Callers can opt out of timeout placeholder text and get a hard failure instead."""
    from skills.bridge.inference_gateway import InferenceGateway

    gw = InferenceGateway()
    with patch.object(gw, "_omlx_chat", return_value={"success": False, "error": "omlx_failed"}), \
         patch.object(gw, "_can_try_remote_melchior", return_value=(False, "offline")), \
         patch.object(gw, "_can_try_remote_balthasar", return_value=(False, "offline")), \
         patch.object(gw, "_local_chat", return_value={"success": False, "error": "local_timeout"}):
        result = gw.chat("請摘要這份文件", task_type="summary", timeout=30, allow_synthetic_fallback=False)

    assert result["success"] is False
    assert result["route"] == "failed_all"
    assert "本機模型逾時" not in result.get("response", "")


def test_tc_review_uses_local_without_remote_probes():
    """tc_review should go straight to local oMLX TAIDE instead of probing remotes first."""
    from skills.bridge.inference_gateway import InferenceGateway

    gw = InferenceGateway()
    with patch.object(gw, "_remote_chat_melchior", side_effect=AssertionError("remote melchior should not be called")), \
         patch.object(gw, "_remote_chat_balthasar", side_effect=AssertionError("remote balthasar should not be called")), \
         patch.object(gw, "_can_try_remote_melchior", side_effect=AssertionError("remote melchior probe should not be called")), \
         patch.object(gw, "_can_try_remote_balthasar", side_effect=AssertionError("remote balthasar probe should not be called")), \
         patch.object(gw, "_omlx_chat", return_value={"success": True, "response": "正確", "route": "omlx", "model": "TAIDE-12b-Chat-mlx-4bit"}):
        result = gw.chat("請檢查這段譯文是否忠實", task_type="tc_review", timeout=12)

    assert result["success"] is True
    assert result["route"] == "omlx"
    assert result["response"] == "正確"
